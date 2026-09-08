"""Purchase Orders operator surface over the real app wiring.

In-memory sqlite (StaticPool, FKs ON) with the sales_orders router mounted. Exercises
create-with-lines, the ``(client_id, po_number)`` dup 409, active client/project/product
validation, list/filter, the detail shape, amend (snapshots a version + replaces lines),
short-close (a line and the whole PO), void, the RBAC split (Viewer/Operator/Manager),
and the Excel bulk upload (happy path + dedup skip + bad-row error).
"""
from __future__ import annotations

import io
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module + table is registered on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.projects.models import ClientGstin, Project, ProjectClient
from app.modules.sales_orders.models import Product
from app.modules.sales_orders.routes import router as sales_orders_router
from app.platform.auth import current_user
from app.platform.models import Level, Role, RoleModulePermission, User
from app.platform.roles_builtin import ensure_builtin_roles


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:  # enforce FKs like Postgres
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    operator_role = Role(name="SO Operator", description="", is_system=False)
    operator_role.module_permissions = [
        RoleModulePermission(module_key="sales_orders", level=Level.OPERATE)
    ]
    seed.add(operator_role)
    seed.flush()
    seed.add(User(firebase_uid="admin", email="admin@t.local", name="Admin",
                  role_id=roles["Administrator"].id, active=True))
    seed.add(User(firebase_uid="viewer", email="viewer@t.local", name="Viewer",
                  role_id=roles["Viewer"].id, active=True))
    seed.add(User(firebase_uid="operator", email="op@t.local", name="Operator",
                  role_id=operator_role.id, active=True))
    seed.commit()
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(sales_orders_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.state.TestSession = TestSession
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, uid: str) -> None:
    db = client.app.state.TestSession()
    user = db.execute(
        select(User).options(
            selectinload(User.role).selectinload(Role.module_permissions),
            selectinload(User.role).selectinload(Role.platform_permissions),
        ).where(User.firebase_uid == uid)
    ).scalar_one()
    db.close()
    client.app.dependency_overrides[current_user] = lambda: user


@pytest.fixture
def seeded(client: TestClient) -> dict[str, int]:
    """A client, an ACTIVE project, an ON_HOLD project, a GSTIN, and 3 products (one
    inactive) — the prerequisite rows a PO references."""
    db = client.app.state.TestSession()
    cl = ProjectClient(name="Acme Corp", code="ACM", active=True)
    db.add(cl)
    db.flush()
    gstin = ClientGstin(client_id=cl.id, gstin="27AAPFU0939F1ZV")
    active = Project(client_id=cl.id, seq=1, code="ACM-001", name="Live", status="ACTIVE")
    hold = Project(client_id=cl.id, seq=2, code="ACM-002", name="Held", status="ON_HOLD")
    db.add_all([gstin, active, hold])
    db.flush()
    p1 = Product(name="Widget", code="W1", brand="Acme", model_number="M1",
                 uom="PCS", active=True)
    p2 = Product(name="Gadget", code="G1", uom="BOX", active=True)
    p3 = Product(name="Retired", code="R1", uom="PCS", active=False)
    db.add_all([p1, p2, p3])
    db.flush()
    ids = {
        "client_id": cl.id, "gstin_id": gstin.id,
        "project_id": active.id, "hold_project_id": hold.id,
        "p1": p1.id, "p2": p2.id, "p3_inactive": p3.id,
    }
    db.commit()
    db.close()
    return ids


def _create_body(seeded: dict[str, int], **over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "po_number": "PO-1",
        "client_id": seeded["client_id"],
        "project_id": seeded["project_id"],
        "po_date": "2026-08-20",
        "lines": [
            {"product_id": seeded["p1"], "ordered_qty": "10",
             "cost_price_paise": 3000, "client_sell_price_paise": 5000, "tax_rate": "18"},
        ],
    }
    body.update(over)
    return body


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_BULK_HEADER = [
    "po_number", "product_code", "description", "uom", "ordered_qty",
    "cost_price", "sell_price", "freight", "packaging", "handling", "other", "tax_rate",
]


# ------------------------------------------------------------------- create

def test_create_po_with_lines(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, client_gstin_id=seeded["gstin_id"], notes="urgent",
        expected_procurement_date="2026-09-01"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["po_number"] == "PO-1"
    assert body["status"] == "DRAFT"
    assert body["client_name"] == "Acme Corp"
    assert body["project_code"] == "ACM-001"
    assert body["client_gstin"] == "27AAPFU0939F1ZV"
    assert body["line_count"] == 1
    # The operator is a NON-admin: the client-sell total is visible; the ACTUAL sell total
    # is masked to None. client_sell = 5000 (the required visible figure).
    assert body["total_client_sell_paise"] == 10 * 5000  # ordered_qty * per-unit client sell
    assert body["total_sell_paise"] is None
    line = body["lines"][0]
    assert line["product_name"] == "Widget" and line["brand"] == "Acme"
    assert line["description"] == "Widget"  # snapshot the product name when omitted
    assert line["uom"] == "PCS"  # snapshot the product uom when omitted
    assert line["ordered_qty"] == "10.000" and line["tax_rate"] == "18.00"
    assert line["line_status"] == "OPEN"
    assert line["client_sell_price_paise"] == 5000
    assert line["sell_price_paise"] is None  # ACTUAL masked for a non-admin


def test_duplicate_client_po_number_is_409(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    assert client.post("/api/v1/purchase-orders",
                       json=_create_body(seeded)).status_code == 201
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded))
    assert r.status_code == 409, r.text


def test_inactive_project_400_and_missing_404(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    r_hold = client.post("/api/v1/purchase-orders",
                         json=_create_body(seeded, project_id=seeded["hold_project_id"]))
    assert r_hold.status_code == 400, r_hold.text
    r_missing = client.post("/api/v1/purchase-orders",
                            json=_create_body(seeded, project_id=999999))
    assert r_missing.status_code == 404, r_missing.text


def test_inactive_product_400(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p3_inactive"], "ordered_qty": "1",
         "cost_price_paise": 100, "client_sell_price_paise": 200}]))
    assert r.status_code == 400, r.text


# --------------------------------------------------------------- list / detail

def test_list_and_filters(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    client.post("/api/v1/purchase-orders", json=_create_body(seeded, po_number="PO-A"))
    client.post("/api/v1/purchase-orders", json=_create_body(seeded, po_number="PO-B"))
    _as(client, "viewer")
    allrows = client.get("/api/v1/purchase-orders").json()
    assert {p["po_number"] for p in allrows} == {"PO-A", "PO-B"}
    # filter by po_number substring
    one = client.get("/api/v1/purchase-orders", params={"q": "PO-A"}).json()
    assert len(one) == 1 and one[0]["po_number"] == "PO-A"
    # filter by client + status
    by_client = client.get("/api/v1/purchase-orders",
                           params={"client_id": seeded["client_id"], "status": "DRAFT"}).json()
    assert len(by_client) == 2


def test_detail_shape_and_404(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    _as(client, "viewer")
    d = client.get(f"/api/v1/purchase-orders/{pid}")
    assert d.status_code == 200, d.text
    body = d.json()
    for key in ("client_gstin", "notes", "soft_copy_file", "amendments_count", "lines"):
        assert key in body
    assert body["amendments_count"] == 0
    assert body["soft_copy_file"] is None
    assert client.get("/api/v1/purchase-orders/999999").status_code == 404


# -------------------------------------------------------------------- amend

def test_amend_snapshots_version_and_replaces_lines(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    r = client.patch(f"/api/v1/purchase-orders/{pid}", json={
        "notes": "revised",
        "lines": [
            {"product_id": seeded["p2"], "ordered_qty": "4",
             "cost_price_paise": 1000, "client_sell_price_paise": 2500},
        ],
        "summary": "swapped the line",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amendments_count"] == 1  # a version was snapshotted
    assert body["notes"] == "revised"
    assert body["line_count"] == 1
    assert body["lines"][0]["product_name"] == "Gadget"  # line replaced
    assert body["total_client_sell_paise"] == 4 * 2500  # operator: client-sell total visible


def test_non_admin_amend_preserves_admin_actual(
    client: TestClient, seeded: dict[str, int]
) -> None:
    # An IAM admin records an ACTUAL sell that diverges from the client-quoted sell.
    _as(client, "admin")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p1"], "ordered_qty": "10", "cost_price_paise": 3000,
         "client_sell_price_paise": 5000, "sell_price_paise": 6000, "tax_rate": "18"},
    ])).json()["id"]
    assert client.get(f"/api/v1/purchase-orders/{pid}").json()["lines"][0][
        "sell_price_paise"] == 6000  # admin sees the actual

    # A NON-admin operator amends (full line replacement) editing qty, WITHOUT the actual.
    _as(client, "operator")
    r = client.patch(f"/api/v1/purchase-orders/{pid}", json={"lines": [
        {"product_id": seeded["p1"], "ordered_qty": "20", "cost_price_paise": 3000,
         "client_sell_price_paise": 5000, "tax_rate": "18"},
    ]})
    assert r.status_code == 200, r.text

    # The admin's ACTUAL must be PRESERVED (carried forward by same product+position),
    # NOT silently reset to the client figure (5000).
    _as(client, "admin")
    line = client.get(f"/api/v1/purchase-orders/{pid}").json()["lines"][0]
    assert line["sell_price_paise"] == 6000, "admin actual must survive a non-admin edit"
    assert line["client_sell_price_paise"] == 5000

    # A genuinely NEW product line from a non-admin has no old actual to carry -> client.
    _as(client, "operator")
    assert client.patch(f"/api/v1/purchase-orders/{pid}", json={"lines": [
        {"product_id": seeded["p2"], "ordered_qty": "3", "cost_price_paise": 1000,
         "client_sell_price_paise": 2500, "tax_rate": "18"},
    ]}).status_code == 200
    _as(client, "admin")
    assert client.get(f"/api/v1/purchase-orders/{pid}").json()["lines"][0][
        "sell_price_paise"] == 2500  # new product -> defaults to client


def test_amend_blocked_on_cancelled_is_422(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    _as(client, "admin")
    assert client.post(f"/api/v1/purchase-orders/{pid}/void",
                       json={"reason": "cancelled by client"}).status_code == 200
    _as(client, "operator")
    r = client.patch(f"/api/v1/purchase-orders/{pid}", json={"notes": "too late"})
    assert r.status_code == 422, r.text


# --------------------------------------------------------------- short-close

def test_short_close_one_line(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p1"], "ordered_qty": "10",
         "cost_price_paise": 100, "client_sell_price_paise": 200},
        {"product_id": seeded["p2"], "ordered_qty": "5",
         "cost_price_paise": 100, "client_sell_price_paise": 200}])).json()
    pid, line_id = detail["id"], detail["lines"][0]["id"]
    _as(client, "admin")  # short-close is MANAGE
    r = client.post(f"/api/v1/purchase-orders/{pid}/short-close",
                    json={"line_id": line_id, "reason": "vendor out of stock"})
    assert r.status_code == 200, r.text
    body = r.json()
    lines = {ln["id"]: ln for ln in body["lines"]}
    assert lines[line_id]["line_status"] == "SHORT_CLOSED"
    assert lines[line_id]["short_closed_qty"] == "10.000"
    assert lines[line_id]["short_close_reason"] == "vendor out of stock"
    assert body["status"] == "DRAFT"  # a single-line short-close does NOT close the PO


def test_short_close_whole_po_closes_it(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    _as(client, "admin")
    r = client.post(f"/api/v1/purchase-orders/{pid}/short-close",
                    json={"reason": "project wrapped up"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CLOSED"
    assert all(ln["line_status"] == "SHORT_CLOSED" for ln in body["lines"])


# --------------------------------------------------------------------- void

def test_void_sets_cancelled(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    _as(client, "admin")
    r = client.post(f"/api/v1/purchase-orders/{pid}/void", json={"reason": "duplicate PO"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CANCELLED"


# ------------------------------------------------------------------- confirm

def test_confirm_draft_moves_to_confirmed(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    r = client.post(f"/api/v1/purchase-orders/{pid}/confirm")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"


def test_confirm_non_draft_is_422(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    assert client.post(f"/api/v1/purchase-orders/{pid}/confirm").status_code == 200
    # A second confirm on an already-CONFIRMED PO is a blocked transition -> 422.
    r = client.post(f"/api/v1/purchase-orders/{pid}/confirm")
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------- RBAC

def test_rbac_matrix(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    # Viewer cannot create (needs OPERATE).
    _as(client, "viewer")
    assert client.post("/api/v1/purchase-orders",
                       json=_create_body(seeded, po_number="X")).status_code == 403
    # Viewer cannot confirm (needs OPERATE).
    assert client.post(f"/api/v1/purchase-orders/{pid}/confirm").status_code == 403
    # Operator cannot short-close or void (both need MANAGE).
    _as(client, "operator")
    assert client.post(f"/api/v1/purchase-orders/{pid}/short-close",
                       json={"reason": "no"}).status_code == 403
    assert client.post(f"/api/v1/purchase-orders/{pid}/void",
                       json={"reason": "no"}).status_code == 403


# ------------------------------------------------------------- Excel upload

def test_bulk_upload_happy_path(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    data = _xlsx(_BULK_HEADER, [
        ["BULK-1", "W1", "", "", "3", "10.00", "25.50", "1.00", "", "", "", "18"],
        ["BULK-1", "G1", "custom desc", "BOX", "2", "5.00", "9.00", "", "", "", "", ""],
        ["BULK-2", "W1", "", "", "1", "10.00", "25.00", "", "", "", "", "12"],
    ])
    r = client.post("/api/v1/purchase-orders/upload",
                    files={"file": ("pos.xlsx", data, "application/xlsx")},
                    data={"client_id": seeded["client_id"],
                          "project_id": seeded["project_id"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert set(out["created"]) == {"BULK-1", "BULK-2"}
    assert out["skipped"] == [] and out["errors"] == []
    # BULK-1 has two lines; money parsed rupees -> paise (25.50 -> 2550).
    listing = {p["po_number"]: p for p in client.get("/api/v1/purchase-orders").json()}
    assert listing["BULK-1"]["line_count"] == 2
    # bulk maps the sell column to client_sell; the operator sees the client-sell total.
    assert listing["BULK-1"]["total_client_sell_paise"] == 3 * 2550 + 2 * 900
    assert listing["BULK-1"]["total_sell_paise"] is None  # ACTUAL masked for the operator


def test_bulk_template_download(client: TestClient, seeded: dict[str, int]) -> None:
    """The template endpoint returns an .xlsx (correct content-type + attachment name) whose
    first sheet's header row is EXACTLY the parser's expected columns, in order."""
    _as(client, "operator")
    r = client.get("/api/v1/purchase-orders/bulk-template.xlsx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="po-bulk-template.xlsx"' in r.headers["content-disposition"]

    wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert list(rows[0]) == _BULK_HEADER  # header matches the parser's exact columns, in order
    assert len(rows) == 2  # header + one illustrative example row
    assert rows[1][0]  # the example row carries a po_number


def test_bulk_template_requires_operate(client: TestClient, seeded: dict[str, int]) -> None:
    """The template is gated like the upload — a VIEW-only user is 403."""
    _as(client, "viewer")
    assert client.get("/api/v1/purchase-orders/bulk-template.xlsx").status_code == 403


def test_bulk_template_example_row_is_valid_upload(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """Round-trip proof: the downloaded template's example row, fed straight back through the
    bulk-upload endpoint, creates a PO — so the shipped template is guaranteed-valid input."""
    _as(client, "operator")
    tmpl = client.get("/api/v1/purchase-orders/bulk-template.xlsx")
    assert tmpl.status_code == 200, tmpl.text
    wb = load_workbook(io.BytesIO(tmpl.content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    header, example = (list(row) for row in list(ws.iter_rows(values_only=True))[:2])
    wb.close()

    # Seed a product whose code matches the template's example product_code so it resolves.
    code_idx = header.index("product_code")
    example_code = str(example[code_idx])
    db = client.app.state.TestSession()
    db.add(Product(name="Template Sample", code=example_code, uom="PCS", active=True))
    db.commit()
    db.close()

    # Re-serialise the two template rows into a fresh upload workbook and post it back.
    data = _xlsx(header, [example])
    r = client.post("/api/v1/purchase-orders/upload",
                    files={"file": ("pos.xlsx", data, "application/xlsx")},
                    data={"client_id": seeded["client_id"],
                          "project_id": seeded["project_id"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == [str(example[0])]  # the example po_number was created
    assert out["skipped"] == [] and out["errors"] == []


def test_bulk_upload_dedup_skip(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    client.post("/api/v1/purchase-orders", json=_create_body(seeded, po_number="DUP-1"))
    data = _xlsx(_BULK_HEADER, [
        ["DUP-1", "W1", "", "", "1", "1.00", "2.00", "", "", "", "", ""],
        ["NEW-1", "W1", "", "", "1", "1.00", "2.00", "", "", "", "", ""],
    ])
    r = client.post("/api/v1/purchase-orders/upload",
                    files={"file": ("pos.xlsx", data, "application/xlsx")},
                    data={"client_id": seeded["client_id"],
                          "project_id": seeded["project_id"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == ["NEW-1"]
    assert len(out["skipped"]) == 1 and out["skipped"][0]["po_number"] == "DUP-1"


def test_bulk_upload_bad_row_error(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    data = _xlsx(_BULK_HEADER, [
        ["ERR-1", "NOPE", "", "", "1", "1.00", "2.00", "", "", "", "", ""],  # unknown code
        ["ERR-2", "W1", "", "", "-3", "1.00", "2.00", "", "", "", "", ""],   # bad qty
        ["OK-1", "W1", "", "", "1", "1.00", "2.00", "", "", "", "", ""],
    ])
    r = client.post("/api/v1/purchase-orders/upload",
                    files={"file": ("pos.xlsx", data, "application/xlsx")},
                    data={"client_id": seeded["client_id"],
                          "project_id": seeded["project_id"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == ["OK-1"]
    # Two bad rows -> two errors; each poisons its own PO -> two skips.
    assert len(out["errors"]) == 2
    assert {s["po_number"] for s in out["skipped"]} == {"ERR-1", "ERR-2"}


# ------------------------------------------- audit-fix regressions (Wave 1 audit)

def _other_client_project(client: TestClient) -> tuple[int, int]:
    """A SECOND client with its own ACTIVE project (for cross-client linkage tests)."""
    db = client.app.state.TestSession()
    other = ProjectClient(name="Other Co", code="OTH", active=True)
    db.add(other)
    db.flush()
    proj = Project(client_id=other.id, seq=1, code="OTH-001", name="Other", status="ACTIVE")
    db.add(proj)
    db.flush()
    ids = (other.id, proj.id)
    db.commit()
    db.close()
    return ids


def test_create_rejects_project_of_another_client(
    client: TestClient, seeded: dict[str, int]
) -> None:
    # H1: a PO must not reference a project owned by a DIFFERENT client.
    _as(client, "operator")
    _other_id, other_project = _other_client_project(client)
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded, project_id=other_project))
    assert r.status_code == 400, r.text
    assert "does not belong to this client" in r.json()["detail"]


def test_amend_rejects_project_of_another_client(
    client: TestClient, seeded: dict[str, int]
) -> None:
    # H1 (amend path): the same linkage guard applies when re-pointing the project.
    _as(client, "operator")
    _other_id, other_project = _other_client_project(client)
    created = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()
    r = client.patch(
        f"/api/v1/purchase-orders/{created['id']}", json={"project_id": other_project}
    )
    assert r.status_code in (400, 422), r.text
    assert "does not belong to this client" in r.json()["detail"]


def test_create_rejects_inactive_gstin(client: TestClient, seeded: dict[str, int]) -> None:
    # M1: a soft-deleted (inactive) client GSTIN can't be attached to a new PO.
    db = client.app.state.TestSession()
    row = db.get(ClientGstin, seeded["gstin_id"])
    assert row is not None
    row.active = False
    db.commit()
    db.close()
    _as(client, "operator")
    r = client.post(
        "/api/v1/purchase-orders", json=_create_body(seeded, client_gstin_id=seeded["gstin_id"])
    )
    assert r.status_code == 400, r.text
    assert "inactive" in r.json()["detail"]


def test_bulk_upload_honours_po_date_column(
    client: TestClient, seeded: dict[str, int]
) -> None:
    # M2: an optional po_date column dates each PO (else today); a bad date poisons its group.
    _as(client, "operator")
    header = [*_BULK_HEADER, "po_date"]
    data = _xlsx(header, [
        ["DATED-1", "W1", "", "", "1", "1.00", "2.00", "", "", "", "", "", "2026-01-15"],
        ["BADDATE", "W1", "", "", "1", "1.00", "2.00", "", "", "", "", "", "nonsense"],
    ])
    r = client.post("/api/v1/purchase-orders/upload",
                    files={"file": ("pos.xlsx", data, "application/xlsx")},
                    data={"client_id": seeded["client_id"], "project_id": seeded["project_id"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == ["DATED-1"]
    assert {s["po_number"] for s in out["skipped"]} == {"BADDATE"}
    listing = {p["po_number"]: p for p in client.get("/api/v1/purchase-orders").json()}
    assert listing["DATED-1"]["po_date"] == "2026-01-15"


# ------------------------------------------- pricing tiers + actual/RBAC masking

def _tier_line(seeded: dict[str, int], **over: object) -> dict[str, object]:
    line: dict[str, object] = {
        "product_id": seeded["p1"], "ordered_qty": "10",
        "cost_price_paise": 3000, "original_cost_price_paise": 3200,
        "client_sell_price_paise": 5000, "vendor_sell_price_paise": 4800,
        "client_freight_paise": 50,
    }
    line.update(over)
    return line


def test_create_with_all_tiers_admin_sets_and_sees_actuals(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """An ADMIN may set the ACTUAL sell/freight (diverging from the client figure) and sees
    every tier back, including the actuals and the actual sell total."""
    _as(client, "admin")
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        _tier_line(seeded, sell_price_paise=6000, freight_paise=99)]))
    assert r.status_code == 201, r.text
    body = r.json()
    line = body["lines"][0]
    assert line["cost_price_paise"] == 3000
    assert line["original_cost_price_paise"] == 3200
    assert line["client_sell_price_paise"] == 5000
    assert line["vendor_sell_price_paise"] == 4800
    assert line["client_freight_paise"] == 50
    assert line["sell_price_paise"] == 6000   # ACTUAL — admin set it, diverges from client
    assert line["freight_paise"] == 99        # ACTUAL freight — admin set it
    assert body["total_sell_paise"] == 10 * 6000        # actual aggregate visible to admin
    assert body["total_client_sell_paise"] == 10 * 5000  # client aggregate


def test_actuals_masked_for_non_admin(client: TestClient, seeded: dict[str, int]) -> None:
    """An admin creates a PO with a diverging actual; a non-admin OPERATE user reads it and
    gets None for the actual sell/freight (line + summary), while every client tier shows."""
    _as(client, "admin")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        _tier_line(seeded, sell_price_paise=6000, freight_paise=99)])).json()["id"]
    _as(client, "operator")
    body = client.get(f"/api/v1/purchase-orders/{pid}").json()
    line = body["lines"][0]
    assert line["sell_price_paise"] is None   # ACTUAL masked
    assert line["freight_paise"] is None      # ACTUAL freight masked
    assert line["client_sell_price_paise"] == 5000  # client tier visible
    assert line["vendor_sell_price_paise"] == 4800
    assert line["client_freight_paise"] == 50
    assert body["total_sell_paise"] is None            # actual aggregate masked
    assert body["total_client_sell_paise"] == 10 * 5000
    # And in the list serializer too.
    row = {p["id"]: p for p in client.get("/api/v1/purchase-orders").json()}[pid]
    assert row["total_sell_paise"] is None
    assert row["total_client_sell_paise"] == 10 * 5000


def test_non_admin_cannot_set_actual_defaults_to_client(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """A NON-admin who sends an actual sell/freight diverging from the client figure has it
    IGNORED — the stored actual defaults to the client value (verified by reading as admin)."""
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        _tier_line(seeded, sell_price_paise=9999, freight_paise=7777)])).json()["id"]
    _as(client, "admin")  # only an admin can see the stored actual
    line = client.get(f"/api/v1/purchase-orders/{pid}").json()["lines"][0]
    assert line["sell_price_paise"] == 5000   # defaulted to client_sell, NOT 9999
    assert line["freight_paise"] == 50        # defaulted to client_freight, NOT 7777


def test_client_sell_price_is_required(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p1"], "ordered_qty": "1", "cost_price_paise": 100}]))
    assert r.status_code == 422, r.text  # pydantic: client_sell_price_paise missing


# ------------------------------------------------------- optional po_number

def test_create_without_po_number_succeeds(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(seeded, po_number=None))
    assert r.status_code == 201, r.text
    assert r.json()["po_number"] is None


def test_two_null_number_pos_same_client_both_ok(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """The partial unique index excludes NULLs — two numberless POs for one client persist."""
    _as(client, "operator")
    assert client.post("/api/v1/purchase-orders",
                       json=_create_body(seeded, po_number=None)).status_code == 201
    assert client.post("/api/v1/purchase-orders",
                       json=_create_body(seeded, po_number=None)).status_code == 201


def test_add_po_number_later_via_amend_and_dup_409(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """A numberless PO can get its number via amend; a number already used in-client 409s."""
    _as(client, "operator")
    # An existing PO holds the number "AMD-1".
    assert client.post("/api/v1/purchase-orders",
                       json=_create_body(seeded, po_number="AMD-1")).status_code == 201
    # A numberless PO; amend it to a FREE number -> OK.
    null_pid = client.post("/api/v1/purchase-orders",
                           json=_create_body(seeded, po_number=None)).json()["id"]
    ok = client.patch(f"/api/v1/purchase-orders/{null_pid}", json={"po_number": "AMD-2"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["po_number"] == "AMD-2"
    # Amend it again to a number already taken in this client -> 409.
    dup = client.patch(f"/api/v1/purchase-orders/{null_pid}", json={"po_number": "AMD-1"})
    assert dup.status_code == 409, dup.text


# ------------------------------------------------------------- agency fee

def test_agency_fee_percent_happy(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="5.5"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agency_fee_type"] == "PERCENT"
    # On the wire the percent is a JSON number (FE consumes it as `number`), not a string.
    assert body["agency_fee_percent"] == 5.5
    assert body["agency_fee_amount_paise"] is None
    # Agency fee is REVENUE: 5.5% of the client-sell order value (10*5000 = 50000 paise) =
    # 2750 paise, and the full client-facing revenue = 50000 + 2750. Visible to the operator
    # (non-admin) — it is not a sensitive/actual figure.
    assert body["total_client_sell_paise"] == 50000
    assert body["agency_fee_computed_paise"] == 2750
    assert body["total_with_agency_paise"] == 52750


def test_agency_fee_fixed_happy(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    r = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="FIXED", agency_fee_amount_paise=250000))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["agency_fee_type"] == "FIXED"
    assert body["agency_fee_amount_paise"] == 250000
    assert body["agency_fee_percent"] is None
    # FIXED fee is the flat amount; full revenue = client-sell total + the fixed fee.
    assert body["agency_fee_computed_paise"] == 250000
    assert body["total_with_agency_paise"] == 50000 + 250000


def test_agency_fee_none_default(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    body = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()
    assert body["agency_fee_type"] == "NONE"
    assert body["agency_fee_percent"] is None and body["agency_fee_amount_paise"] is None
    # No fee -> computed 0, and the with-agency total equals the plain client-sell total.
    assert body["agency_fee_computed_paise"] == 0
    assert body["total_with_agency_paise"] == body["total_client_sell_paise"] == 50000


def test_agency_fee_rejections(client: TestClient, seeded: dict[str, int]) -> None:
    _as(client, "operator")
    # PERCENT with amount ALSO set -> 400 (service rule).
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="5",
        agency_fee_amount_paise=1000)).status_code == 400
    # PERCENT with NO percent -> 400.
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT")).status_code == 400
    # FIXED with NO amount -> 400.
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="FIXED")).status_code == 400
    # FIXED with percent set -> 400.
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="FIXED", agency_fee_amount_paise=1000,
        agency_fee_percent="5")).status_code == 400
    # NONE with a percent -> 400.
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="NONE", agency_fee_percent="5")).status_code == 400
    # percent out of 0..100 -> 422 at pydantic (Field le=100).
    assert client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="150")).status_code == 422


def test_agency_fee_amend(client: TestClient, seeded: dict[str, int]) -> None:
    """Amend can set and later clear the agency fee (validated as a trio)."""
    _as(client, "operator")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded)).json()["id"]
    set_pct = client.patch(f"/api/v1/purchase-orders/{pid}", json={
        "agency_fee_type": "PERCENT", "agency_fee_percent": "3.25"})
    assert set_pct.status_code == 200, set_pct.text
    assert set_pct.json()["agency_fee_percent"] == 3.25
    # Changing type to PERCENT without a percent is rejected (422 -> blocked amend).
    bad = client.patch(f"/api/v1/purchase-orders/{pid}", json={
        "agency_fee_type": "FIXED"})
    assert bad.status_code == 422, bad.text
    # Clear back to NONE.
    clear = client.patch(f"/api/v1/purchase-orders/{pid}", json={"agency_fee_type": "NONE"})
    assert clear.status_code == 200, clear.text
    assert clear.json()["agency_fee_type"] == "NONE"
    assert clear.json()["agency_fee_percent"] is None


# --------------------------------------------- totals net of short-close / void

def test_void_zeroes_client_total_and_agency(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """A VOIDED (CANCELLED) PO carries NO revenue: the client-sell total, the client freight,
    the agency fee, the total revenue AND the admin actual total all collapse to 0
    (audit HIGH-1)."""
    _as(client, "admin")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="5",
        lines=[_tier_line(seeded, sell_price_paise=6000)])).json()
    pid = detail["id"]
    # Before: 10*5000 = 50000 goods; client freight 50 (flat, per line); 5% agency on the
    # ENTIRE billing (50000 + 50 = 50050) = 2502.5 -> 2503 HALF-UP; total revenue = 52553.
    assert detail["total_client_sell_paise"] == 50000
    assert detail["total_client_freight_paise"] == 50
    assert detail["total_client_extras_paise"] == 0
    assert detail["agency_fee_computed_paise"] == 2503
    assert detail["total_with_agency_paise"] == 52553
    assert detail["total_sell_paise"] == 10 * 6000  # admin actual aggregate
    body = client.post(f"/api/v1/purchase-orders/{pid}/void",
                       json={"reason": "duplicate PO"}).json()
    assert body["status"] == "CANCELLED"
    assert body["total_client_sell_paise"] == 0
    assert body["total_client_freight_paise"] == 0
    assert body["agency_fee_computed_paise"] == 0
    assert body["total_with_agency_paise"] == 0
    assert body["total_sell_paise"] == 0  # actual aggregate also zeroed


def test_short_close_nets_out_of_client_total_and_agency(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """A short-close retires the un-invoiced quantity, so that line drops out of the client
    total AND the PERCENT agency base — the fee is charged only on what stays invoiceable
    (audit HIGH-2)."""
    _as(client, "admin")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="10", lines=[
            {"product_id": seeded["p1"], "ordered_qty": "10",
             "cost_price_paise": 100, "client_sell_price_paise": 5000},
            {"product_id": seeded["p2"], "ordered_qty": "4",
             "cost_price_paise": 100, "client_sell_price_paise": 2500}])).json()
    pid = detail["id"]
    line1_id = detail["lines"][0]["id"]
    # Before: (10*5000)+(4*2500) = 60000 client; 10% agency = 6000; with-agency 66000.
    assert detail["total_client_sell_paise"] == 60000
    assert detail["agency_fee_computed_paise"] == 6000
    # Short-close line 1 (unbilled DRAFT → retires all 10 units → nets to 0).
    body = client.post(f"/api/v1/purchase-orders/{pid}/short-close",
                       json={"line_id": line1_id, "reason": "vendor shortfall"}).json()
    assert body["lines"][0]["line_status"] == "SHORT_CLOSED"
    # Only line 2 remains invoiceable: 4*2500 = 10000; 10% agency = 1000; with-agency 11000.
    assert body["total_client_sell_paise"] == 10000
    assert body["agency_fee_computed_paise"] == 1000
    assert body["total_with_agency_paise"] == 11000


def test_client_freight_is_revenue_and_nets_on_full_short_close(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """Client freight is REVENUE we bill the client: it counts toward the total revenue AND
    the agency-fee base (agency is charged on the ENTIRE client billing = goods + freight). A
    FULLY short-closed line (nothing ships) drops its flat freight too."""
    _as(client, "admin")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="10", lines=[
            {"product_id": seeded["p1"], "ordered_qty": "10", "cost_price_paise": 100,
             "client_sell_price_paise": 5000, "client_freight_paise": 300},
            {"product_id": seeded["p2"], "ordered_qty": "4", "cost_price_paise": 100,
             "client_sell_price_paise": 2500, "client_freight_paise": 200}])).json()
    pid = detail["id"]
    line1_id = detail["lines"][0]["id"]
    # goods 60000; freight 300+200 = 500; agency 10% of the ENTIRE billing (60500) = 6050;
    # total revenue = 60000 + 500 + 6050 = 66550.
    assert detail["total_client_sell_paise"] == 60000
    assert detail["total_client_freight_paise"] == 500
    assert detail["agency_fee_computed_paise"] == 6050
    assert detail["total_with_agency_paise"] == 66550
    # Fully short-close line 1 → its goods AND its flat freight both drop out.
    body = client.post(f"/api/v1/purchase-orders/{pid}/short-close",
                       json={"line_id": line1_id, "reason": "vendor shortfall"}).json()
    # Only line 2 ships: goods 10000; freight 200; agency 10% of (10000+200) = 1020;
    # total revenue = 10000 + 200 + 1020 = 11220.
    assert body["total_client_sell_paise"] == 10000
    assert body["total_client_freight_paise"] == 200
    assert body["agency_fee_computed_paise"] == 1020
    assert body["total_with_agency_paise"] == 11220


def test_packaging_handling_other_count_as_billing_and_agency_base(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """Packaging / handling / other are CLIENT charges: they count toward the total revenue
    AND the agency-fee base (the agency % is on the ENTIRE client billing = goods + freight +
    packaging + handling + other)."""
    _as(client, "admin")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(
        seeded, agency_fee_type="PERCENT", agency_fee_percent="10", lines=[
            {"product_id": seeded["p1"], "ordered_qty": "10", "cost_price_paise": 100,
             "client_sell_price_paise": 5000, "client_freight_paise": 300,
             "packaging_paise": 100, "handling_paise": 200, "other_paise": 50}])).json()
    # goods 50000; freight 300; extras 100+200+50 = 350; billing = 50000+300+350 = 50650;
    # agency 10% of 50650 = 5065; total revenue = 50650 + 5065 = 55715.
    assert detail["total_client_sell_paise"] == 50000
    assert detail["total_client_freight_paise"] == 300
    assert detail["total_client_extras_paise"] == 350
    assert detail["agency_fee_computed_paise"] == 5065
    assert detail["total_with_agency_paise"] == 55715


# --------------------------------------- amend carries admin actuals (by product)

def test_amend_reorder_preserves_admin_actuals(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """A non-admin amend that REORDERS the lines must keep each admin-set actual — the carry
    matches by PRODUCT, not list position, so a reorder no longer wipes the margin
    (audit MED-3)."""
    _as(client, "admin")
    detail = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p1"], "ordered_qty": "10",
         "cost_price_paise": 100, "client_sell_price_paise": 5000, "sell_price_paise": 6000},
        {"product_id": seeded["p2"], "ordered_qty": "5",
         "cost_price_paise": 100, "client_sell_price_paise": 2500,
         "sell_price_paise": 3000}])).json()
    pid = detail["id"]
    _as(client, "operator")  # non-admin amends, sending the two lines REVERSED, no actuals
    r = client.patch(f"/api/v1/purchase-orders/{pid}", json={"lines": [
        {"product_id": seeded["p2"], "ordered_qty": "6",
         "cost_price_paise": 100, "client_sell_price_paise": 2500},
        {"product_id": seeded["p1"], "ordered_qty": "11",
         "cost_price_paise": 100, "client_sell_price_paise": 5000}]})
    assert r.status_code == 200, r.text
    _as(client, "admin")  # only an admin can read the stored actuals back
    lines = {ln["product_id"]: ln for ln in
             client.get(f"/api/v1/purchase-orders/{pid}").json()["lines"]}
    assert lines[seeded["p1"]]["sell_price_paise"] == 6000  # preserved despite reorder
    assert lines[seeded["p2"]]["sell_price_paise"] == 3000  # preserved despite reorder


def test_admin_amend_omitting_actual_preserves_margin(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """An ADMIN amend that edits a quantity WITHOUT re-sending the actual must keep the prior
    actual, not silently reset it to the client figure (audit MED-4)."""
    _as(client, "admin")
    pid = client.post("/api/v1/purchase-orders", json=_create_body(seeded, lines=[
        {"product_id": seeded["p1"], "ordered_qty": "10",
         "cost_price_paise": 100, "client_sell_price_paise": 5000,
         "sell_price_paise": 6000}])).json()["id"]
    # Admin fixes only the quantity; omits sell_price_paise.
    r = client.patch(f"/api/v1/purchase-orders/{pid}", json={"lines": [
        {"product_id": seeded["p1"], "ordered_qty": "20",
         "cost_price_paise": 100, "client_sell_price_paise": 5000}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lines"][0]["ordered_qty"] == "20.000"
    assert body["lines"][0]["sell_price_paise"] == 6000  # preserved, NOT reset to 5000
