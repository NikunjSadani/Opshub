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
from openpyxl import Workbook
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
             "cost_price_paise": 3000, "sell_price_paise": 5000, "tax_rate": "18"},
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
    assert body["total_sell_paise"] == 10 * 5000  # ordered_qty * per-unit sell
    line = body["lines"][0]
    assert line["product_name"] == "Widget" and line["brand"] == "Acme"
    assert line["description"] == "Widget"  # snapshot the product name when omitted
    assert line["uom"] == "PCS"  # snapshot the product uom when omitted
    assert line["ordered_qty"] == "10.000" and line["tax_rate"] == "18.00"
    assert line["line_status"] == "OPEN"


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
         "cost_price_paise": 100, "sell_price_paise": 200}]))
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
             "cost_price_paise": 1000, "sell_price_paise": 2500},
        ],
        "summary": "swapped the line",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amendments_count"] == 1  # a version was snapshotted
    assert body["notes"] == "revised"
    assert body["line_count"] == 1
    assert body["lines"][0]["product_name"] == "Gadget"  # line replaced
    assert body["total_sell_paise"] == 4 * 2500


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
         "cost_price_paise": 100, "sell_price_paise": 200},
        {"product_id": seeded["p2"], "ordered_qty": "5",
         "cost_price_paise": 100, "sell_price_paise": 200}])).json()
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
    assert listing["BULK-1"]["total_sell_paise"] == 3 * 2550 + 2 * 900


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
