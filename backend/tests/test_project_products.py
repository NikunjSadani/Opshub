"""Project-Product tags — tag/untag/list + picker curation + RBAC, over real wiring.

Exercises the money-free curation surface: tagging a shared Product to a Project
(idempotent), the management list (ALL tags incl. inactive), and the extended
`/products` picker (empty `q` + `project_id` => curated tagged-only default; a
non-blank `q` => full catalogue). RBAC split: reads need sales_orders >= View,
writes need `product.tag` (OPERATE). Uses an in-memory sqlite with FKs enforced, so
the `uq_project_product` unique constraint and the FK cascades are genuinely tested.
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from decimal import Decimal

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
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.routes import router as sales_orders_router
from app.platform.auth import current_user
from app.platform.models import Level, Role, RoleModulePermission, User
from app.platform.rbac import SALES_ORDERS
from app.platform.roles_builtin import ensure_builtin_roles


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:  # enforce FKs like Postgres
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    # A sales_orders OPERATE role — the exact level `product.tag` requires (Viewer is
    # VIEW-only, Administrator is MANAGE; this pins the OPERATE boundary under test).
    operator_role = Role(name="SO Operator", description="Sales Orders operate.",
                         is_system=False, created_by="system")
    operator_role.module_permissions = [
        RoleModulePermission(module_key=SALES_ORDERS, level=Level.OPERATE)
    ]
    seed.add(operator_role)
    seed.flush()
    seed.add(User(firebase_uid="admin", email="admin@t.local", name="Admin",
                  role_id=roles["Administrator"].id, active=True))
    seed.add(User(firebase_uid="viewer", email="viewer@t.local", name="Viewer",
                  role_id=roles["Viewer"].id, active=True))
    seed.add(User(firebase_uid="operator", email="op@t.local", name="Operator",
                  role_id=operator_role.id, active=True))
    # A client + project to tag products to.
    pc = ProjectClient(name="Bajaj", code="BAJ", created_by="system")
    seed.add(pc)
    seed.flush()
    seed.add(Project(client_id=pc.id, seq=1, code="BAJ-001", name="Diwali", created_by="system"))
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
        select(User)
        .options(
            selectinload(User.role).selectinload(Role.module_permissions),
            selectinload(User.role).selectinload(Role.platform_permissions),
        )
        .where(User.firebase_uid == uid)
    ).scalar_one()
    db.close()
    client.app.dependency_overrides[current_user] = lambda: user


def _project_id(client: TestClient) -> int:
    db = client.app.state.TestSession()
    pid = db.execute(select(Project.id).where(Project.code == "BAJ-001")).scalar_one()
    db.close()
    return pid


def _make_product(client: TestClient, name: str, **extra: object) -> int:
    r = client.post("/api/v1/products", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


# --------------------------------------------------------------- tag / list / untag

def test_tag_then_list_and_idempotent(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Mixer")

    r = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert r.status_code == 201, r.text
    assert r.json()["product_id"] == pid

    listed = client.get("/api/v1/project-products", params={"project_id": proj})
    assert listed.status_code == 200
    assert [p["product_id"] for p in listed.json()] == [pid]

    # Re-tagging the same pair is idempotent: 200 (not 201), still exactly one row.
    again = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert again.status_code == 200, again.text
    assert again.json()["product_id"] == pid
    listed2 = client.get("/api/v1/project-products", params={"project_id": proj})
    assert [p["product_id"] for p in listed2.json()] == [pid]


def test_untag_is_idempotent(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Toaster")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})

    r = client.delete(f"/api/v1/project-products/{proj}/{pid}")
    assert r.status_code == 200 and r.json() == {"deleted": True}
    assert client.get("/api/v1/project-products", params={"project_id": proj}).json() == []

    # Untag again -> nothing to remove -> 200 {deleted: false}.
    again = client.delete(f"/api/v1/project-products/{proj}/{pid}")
    assert again.status_code == 200 and again.json() == {"deleted": False}


def test_list_no_tags_is_empty(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    assert client.get("/api/v1/project-products", params={"project_id": proj}).json() == []


# --------------------------------------------------------------- picker curation

def test_picker_curation_empty_q_vs_search(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    tagged = _make_product(client, "Bajaj Mixer")
    _untagged = _make_product(client, "Bajaj Kettle")  # in catalogue, NOT tagged
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": tagged})

    # Empty q + project_id -> curated: ONLY the project's tagged products.
    curated = client.get("/api/v1/products", params={"project_id": proj})
    assert [p["id"] for p in curated.json()] == [tagged]

    # A matching q -> full catalogue (tagged or not): both "Bajaj" products.
    searched = client.get("/api/v1/products", params={"project_id": proj, "q": "bajaj"})
    assert {p["id"] for p in searched.json()} == {tagged, _untagged}

    # A blank (whitespace) q with project_id still curates.
    blank = client.get("/api/v1/products", params={"project_id": proj, "q": "   "})
    assert [p["id"] for p in blank.json()] == [tagged]


def test_picker_untagged_project_falls_back_to_full_catalogue(client: TestClient) -> None:
    # A project with NO tags must NOT open an empty picker — it falls back to the full
    # catalogue (so every existing untagged project behaves like the un-scoped picker; the
    # curation only NARROWS a picker once the project has been tagged).
    _as(client, "admin")
    proj = _project_id(client)
    a = _make_product(client, "Gamma")
    b = _make_product(client, "Delta")
    # project_id + empty q, project has NO tags -> full catalogue, newest-first.
    ids = [p["id"] for p in client.get("/api/v1/products", params={"project_id": proj}).json()]
    assert ids == [b, a]
    # Tag one product -> the picker now narrows to just that product.
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": a})
    curated = [p["id"] for p in client.get("/api/v1/products", params={"project_id": proj}).json()]
    assert curated == [a]


def test_picker_no_project_id_unchanged(client: TestClient) -> None:
    _as(client, "admin")
    a = _make_product(client, "Alpha")
    b = _make_product(client, "Beta")
    # No project_id -> full catalogue, newest-first (existing behavior byte-identical).
    ids = [p["id"] for p in client.get("/api/v1/products").json()]
    assert ids == [b, a]


def test_picker_respects_active_but_management_list_shows_all(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    inactive = _make_product(client, "Retired Mixer")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": inactive})
    client.patch(f"/api/v1/products/{inactive}", json={"active": False})

    # Curated picker with active=true EXCLUDES the inactive tagged product.
    picker = client.get("/api/v1/products", params={"project_id": proj, "active": True})
    assert picker.json() == []
    # Management list shows ALL tags including inactive (FE marks them).
    mgmt = client.get("/api/v1/project-products", params={"project_id": proj})
    assert [p["product_id"] for p in mgmt.json()] == [inactive]
    assert mgmt.json()[0]["active"] is False


# --------------------------------------------------------------- validation

def test_unknown_project_is_404(client: TestClient) -> None:
    _as(client, "admin")
    pid = _make_product(client, "Widget")
    assert client.get("/api/v1/project-products", params={"project_id": 999999}).status_code == 404
    r = client.post("/api/v1/project-products", json={"project_id": 999999, "product_id": pid})
    assert r.status_code == 404, r.text


def test_unknown_product_is_404(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    r = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": 999999})
    assert r.status_code == 404, r.text


# --------------------------------------------------------------- RBAC

def test_viewer_can_read_but_not_write(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Seen")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})

    _as(client, "viewer")
    assert client.get("/api/v1/project-products", params={"project_id": proj}).status_code == 200
    assert client.get("/api/v1/products", params={"project_id": proj}).status_code == 200
    post = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert post.status_code == 403
    assert client.delete(f"/api/v1/project-products/{proj}/{pid}").status_code == 403


def test_operator_can_tag_and_untag(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Op Widget")  # admin creates the product (needs Manage)

    _as(client, "operator")
    tag = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert tag.status_code == 201, tag.text
    untag = client.delete(f"/api/v1/project-products/{proj}/{pid}")
    assert untag.status_code == 200 and untag.json() == {"deleted": True}


# --------------------------------------------------------------- pricing template

# A full template; `sell_price_paise`/`freight_paise` are the ADMIN-ONLY actuals.
_TEMPLATE: dict[str, object] = {
    "description": "Diwali Mixer",
    "uom": "PCS",
    "cost_price_paise": 3000,
    "original_cost_price_paise": 3200,
    "client_sell_price_paise": 5000,
    "vendor_sell_price_paise": 4800,
    "sell_price_paise": 6000,        # ACTUAL — admin-only
    "client_freight_paise": 50,
    "vendor_freight_paise": 40,
    "freight_paise": 99,             # ACTUAL — admin-only
    "packaging_paise": 10,
    "handling_paise": 20,
    "other_paise": 5,
    "tax_rate": "18",
}


def _sole(client: TestClient, proj: int) -> dict[str, object]:
    """The single tag row on `proj` (as the current caller sees it — masked or not)."""
    rows = client.get("/api/v1/project-products", params={"project_id": proj}).json()
    assert len(rows) == 1, rows
    return rows[0]


def test_tag_with_pricing_returns_fields_and_idempotent_no_overwrite(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Diwali Mixer")

    r = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid, **_TEMPLATE},
    )
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["product_id"] == pid
    assert row["name"] == "Diwali Mixer"      # joined product identity
    assert row["active"] is True
    assert row["cost_price_paise"] == 3000
    assert row["client_sell_price_paise"] == 5000
    assert row["sell_price_paise"] == 6000    # admin sees the actual
    assert row["freight_paise"] == 99
    assert row["packaging_paise"] == 10
    assert Decimal(str(row["tax_rate"])) == Decimal("18")

    got = _sole(client, proj)
    assert got["cost_price_paise"] == 3000
    assert got["other_paise"] == 5

    # Re-tag with DIFFERENT pricing -> idempotent 200, ORIGINAL fields untouched.
    again = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid,
              "cost_price_paise": 111, "sell_price_paise": 222},
    )
    assert again.status_code == 200, again.text
    assert again.json()["cost_price_paise"] == 3000   # NOT overwritten by the re-tag
    assert again.json()["sell_price_paise"] == 6000   # NOT overwritten


def test_patch_updates_subset_leaves_rest(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Kettle")
    client.post("/api/v1/project-products",
                json={"project_id": proj, "product_id": pid, **_TEMPLATE})

    r = client.patch(f"/api/v1/project-products/{proj}/{pid}",
                     json={"cost_price_paise": 3500, "tax_rate": "12"})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["cost_price_paise"] == 3500              # changed
    assert Decimal(str(row["tax_rate"])) == Decimal("12")
    assert row["client_sell_price_paise"] == 5000       # untouched
    assert row["packaging_paise"] == 10                 # untouched
    assert row["sell_price_paise"] == 6000              # untouched actual (admin sees)


def test_product_uom_is_product_master_independent_of_template(client: TestClient) -> None:
    """`product_uom` reflects the Product master's own uom and is present even when the
    template sets no `uom` override (the PO pre-fill falls back to it)."""
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Boxed Mixer", uom="BOX")
    # Tag WITHOUT a template uom override.
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})

    row = _sole(client, proj)
    assert row["product_uom"] == "BOX"   # from the Product master
    assert row["uom"] is None            # template override unset

    # Default product uom is "PCS" when not given at product creation.
    pid2 = _make_product(client, "Default Uom")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid2})
    rows = client.get("/api/v1/project-products", params={"project_id": proj}).json()
    got = next(r for r in rows if r["product_id"] == pid2)
    assert got["product_uom"] == "PCS"


def test_patch_non_tagged_is_404(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Never Tagged")
    r = client.patch(f"/api/v1/project-products/{proj}/{pid}",
                     json={"cost_price_paise": 1})
    assert r.status_code == 404, r.text


# --------------------------------------------------------------- actual masking

def test_actuals_masked_for_non_admin_read(client: TestClient) -> None:
    """A non-admin (OPERATE, no platform IAM) reads the two ACTUAL fields as null while
    every visible tier shows; an admin sees the actuals."""
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Masked Mixer")
    client.post("/api/v1/project-products",
                json={"project_id": proj, "product_id": pid, **_TEMPLATE})

    admin_row = _sole(client, proj)
    assert admin_row["sell_price_paise"] == 6000       # admin: actual visible
    assert admin_row["freight_paise"] == 99

    _as(client, "operator")  # OPERATE, but NOT platform IAM -> non-admin
    row = _sole(client, proj)
    assert row["sell_price_paise"] is None             # ACTUAL masked
    assert row["freight_paise"] is None                # ACTUAL freight masked
    assert row["client_sell_price_paise"] == 5000      # visible tier present
    assert row["vendor_sell_price_paise"] == 4800
    assert row["cost_price_paise"] == 3000
    assert row["client_freight_paise"] == 50
    assert row["packaging_paise"] == 10


def test_non_admin_write_of_actuals_is_dropped(client: TestClient) -> None:
    """A non-admin's actual sell/freight is DROPPED on both POST and PATCH (never
    persisted); an admin can set them and a non-admin's later edit can't clear them."""
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "NonAdmin Write")  # admin creates it (product.manage)

    # Non-admin tags WITH actuals -> dropped (response masked + not persisted).
    _as(client, "operator")
    r = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid,
              "cost_price_paise": 3000, "client_sell_price_paise": 5000,
              "sell_price_paise": 9999, "freight_paise": 7777},
    )
    assert r.status_code == 201, r.text
    assert r.json()["sell_price_paise"] is None        # masked in the response

    _as(client, "admin")  # only an admin can see the stored actual
    row = _sole(client, proj)
    assert row["sell_price_paise"] is None              # non-admin write was NOT persisted
    assert row["freight_paise"] is None
    assert row["cost_price_paise"] == 3000              # visible field DID persist
    assert row["client_sell_price_paise"] == 5000

    # Admin sets the real actuals.
    client.patch(f"/api/v1/project-products/{proj}/{pid}",
                 json={"sell_price_paise": 6000, "freight_paise": 99})
    row = _sole(client, proj)
    assert row["sell_price_paise"] == 6000
    assert row["freight_paise"] == 99

    # Non-admin PATCH tries to CHANGE the actuals -> dropped; admin's value survives,
    # while a visible field the non-admin sends DOES update.
    _as(client, "operator")
    resp = client.patch(f"/api/v1/project-products/{proj}/{pid}",
                        json={"sell_price_paise": 1, "freight_paise": 2,
                              "cost_price_paise": 4000})
    assert resp.status_code == 200, resp.text
    _as(client, "admin")
    row = _sole(client, proj)
    assert row["sell_price_paise"] == 6000             # unchanged (non-admin actual dropped)
    assert row["freight_paise"] == 99                  # unchanged
    assert row["cost_price_paise"] == 4000             # visible edit applied


# --------------------------------------------------------------- bounds + RBAC

def test_negative_money_is_422(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Neg Money")
    r = client.post("/api/v1/project-products",
                    json={"project_id": proj, "product_id": pid, "cost_price_paise": -1})
    assert r.status_code == 422, r.text


def test_tax_rate_over_100_is_422(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Bad Tax")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    r = client.patch(f"/api/v1/project-products/{proj}/{pid}", json={"tax_rate": "150"})
    assert r.status_code == 422, r.text


def test_over_scale_tax_is_422_not_silently_rounded(client: TestClient) -> None:
    # tax_rate is Numeric(5,2); an over-scale value must be a clean 422, never a silent DB
    # round (SQLite keeps full precision and would hide a round from the test).
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Scale Tax")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    r = client.patch(f"/api/v1/project-products/{proj}/{pid}", json={"tax_rate": "18.999"})
    assert r.status_code == 422, r.text


def test_money_over_ceiling_is_422_not_500(client: TestClient) -> None:
    # A money value beyond the int8 ceiling degrades to a clean 422, never a DB-overflow 500.
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Huge Money")
    r = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid, "cost_price_paise": 10**16},
    )
    assert r.status_code == 422, r.text


def test_tax_rate_serialized_as_string(client: TestClient) -> None:
    # The FE contract types tax_rate as `string | null` and calls .trim() on it — the wire
    # value must be a JSON STRING, not a bare number.
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Str Tax")
    client.post("/api/v1/project-products",
                json={"project_id": proj, "product_id": pid, "tax_rate": "18"})
    row = client.get("/api/v1/project-products", params={"project_id": proj}).json()[0]
    assert isinstance(row["tax_rate"], str)


def test_viewer_403_on_post_and_patch(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "RBAC Guarded")
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})

    _as(client, "viewer")  # VIEW only -> no product.tag
    post = client.post("/api/v1/project-products",
                       json={"project_id": proj, "product_id": pid, "cost_price_paise": 1})
    assert post.status_code == 403
    patch = client.patch(f"/api/v1/project-products/{proj}/{pid}",
                         json={"cost_price_paise": 1})
    assert patch.status_code == 403


# --------------------------------------------------------------- bulk pricing upload

def _new_product(client: TestClient, name: str, **extra: object) -> dict[str, object]:
    """Create a product and return its full row (id + code + ...)."""
    r = client.post("/api/v1/products", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    """Build an in-memory .xlsx with the given header row + data rows."""
    wb = Workbook()
    ws = wb.active
    ws.title = "pricing"
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client: TestClient, project_id: int, data: bytes) -> object:
    return client.post(
        "/api/v1/project-products/upload",
        data={"project_id": str(project_id)},
        files={"file": ("pricing.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def _row_for(client: TestClient, proj: int, product_id: int) -> dict[str, object]:
    rows = client.get("/api/v1/project-products", params={"project_id": proj}).json()
    return next(r for r in rows if r["product_id"] == product_id)


def test_bulk_prices_by_code_and_by_exact_name(client: TestClient) -> None:
    """Upload prices EXISTING products — one resolved by code, one by exact name — and both
    are tagged + priced (the GET reflects the money)."""
    _as(client, "admin")
    proj = _project_id(client)
    by_code = _new_product(client, "Mixer Grinder", code="SKU-CODE-1")
    by_name = _new_product(client, "Unique Kettle Name")  # auto-minted code

    data = _xlsx(
        ["product_code", "product_name", "cost_price", "client_sell", "tax_rate"],
        [
            ["SKU-CODE-1", "", 250.00, 399.00, 18],   # resolved by code
            ["", "Unique Kettle Name", 120.00, 199.00, 12],  # resolved by exact name
        ],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert set(body["priced"]) == {"SKU-CODE-1", by_name["code"]}

    # GET reflects the persisted money (rupees parsed to paise).
    row_code = _row_for(client, proj, int(by_code["id"]))
    assert row_code["cost_price_paise"] == 25000
    assert row_code["client_sell_price_paise"] == 39900
    assert Decimal(str(row_code["tax_rate"])) == Decimal("18")

    row_name = _row_for(client, proj, int(by_name["id"]))
    assert row_name["cost_price_paise"] == 12000
    assert row_name["client_sell_price_paise"] == 19900


def test_bulk_code_match_is_exact_case_sensitive_no_crash(client: TestClient) -> None:
    """Product codes are case-SENSITIVELY unique, so `CODE` and `code` can coexist. A pricing
    row's product_code must resolve by EXACT match (not a case-folded one that would match
    both and raise MultipleResultsFound -> a 500 that aborts the whole batch). The upload
    prices exactly the exact-case product and leaves the other untouched."""
    _as(client, "admin")
    proj = _project_id(client)
    upper = _new_product(client, "Upper Cased", code="CASECODE")
    _new_product(client, "Lower Cased", code="casecode")  # coexists (case-sensitive unique)

    data = _xlsx(
        ["product_code", "cost_price", "client_sell"],
        [["CASECODE", 200.00, 350.00]],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text  # not a 500 from an ambiguous lookup
    body = r.json()
    assert body["errors"] == []
    assert body["priced"] == ["CASECODE"]

    # Only the exact-case product was priced/tagged; the other-case one is untouched.
    rows = client.get("/api/v1/project-products", params={"project_id": proj}).json()
    assert [int(r["product_id"]) for r in rows] == [int(upper["id"])]  # lower not tagged
    assert rows[0]["cost_price_paise"] == 20000


def test_bulk_unknown_and_ambiguous_are_row_errors(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    # Two DIFFERENT products sharing the same name -> an ambiguous name lookup.
    _new_product(client, "Twin", brand="A")
    _new_product(client, "Twin", brand="B")

    data = _xlsx(
        ["product_code", "product_name", "cost_price"],
        [
            ["NO-SUCH-CODE", "", 100.00],   # unknown product
            ["", "Twin", 100.00],           # ambiguous name
        ],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["priced"] == []
    msgs = {e["row"]: e["message"] for e in body["errors"]}
    assert msgs[2] == "product not found — add it via the Products upload first"
    assert msgs[3] == "ambiguous product name; use a product code"
    # Nothing was tagged.
    assert client.get("/api/v1/project-products", params={"project_id": proj}).json() == []


def test_bulk_non_admin_cannot_set_actuals(client: TestClient) -> None:
    """A NON-admin (OPERATE, no IAM) upload with actual_sell/actual_freight set: the actuals
    are NOT persisted (admin GET still sees null) while the visible tiers ARE."""
    _as(client, "admin")
    proj = _project_id(client)
    prod = _new_product(client, "Margin Widget", code="MARGIN-1")

    _as(client, "operator")  # OPERATE, but NOT platform IAM -> non-admin
    data = _xlsx(
        ["product_code", "cost_price", "client_sell", "actual_sell", "actual_freight"],
        [["MARGIN-1", 300.00, 500.00, 999.00, 777.00]],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    assert r.json()["priced"] == ["MARGIN-1"]

    _as(client, "admin")  # only an admin can see the stored actual
    row = _row_for(client, proj, int(prod["id"]))
    assert row["sell_price_paise"] is None       # actual NOT persisted by a non-admin
    assert row["freight_paise"] is None
    assert row["cost_price_paise"] == 30000       # visible tiers DID persist
    assert row["client_sell_price_paise"] == 50000


def test_bulk_admin_can_set_actuals(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    prod = _new_product(client, "Admin Margin", code="ADM-1")

    data = _xlsx(
        ["product_code", "client_sell", "actual_sell", "actual_freight"],
        [["ADM-1", 500.00, 620.00, 45.00]],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    assert r.json()["priced"] == ["ADM-1"]

    row = _row_for(client, proj, int(prod["id"]))
    assert row["sell_price_paise"] == 62000       # admin CAN set the actual
    assert row["freight_paise"] == 4500
    assert row["client_sell_price_paise"] == 50000


def test_bulk_template_download_masks_actual_columns(client: TestClient) -> None:
    """The admin's template INCLUDES the two actual columns; the non-admin's OMITS them.
    The download needs module VIEW."""
    _as(client, "admin")
    admin_dl = client.get("/api/v1/project-products/bulk-template.xlsx")
    assert admin_dl.status_code == 200
    admin_cols = _header_cols(admin_dl.content)
    assert "actual_sell" in admin_cols
    assert "actual_freight" in admin_cols

    _as(client, "operator")  # non-admin (no IAM)
    op_dl = client.get("/api/v1/project-products/bulk-template.xlsx")
    assert op_dl.status_code == 200
    op_cols = _header_cols(op_dl.content)
    assert "actual_sell" not in op_cols
    assert "actual_freight" not in op_cols
    assert "client_sell" in op_cols  # visible columns still present


def _header_cols(data: bytes) -> list[str]:
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        header = next(ws.iter_rows(values_only=True))
        return [str(c) for c in header if c is not None]
    finally:
        wb.close()


def test_bulk_money_and_tax_bounds_are_per_row_errors(client: TestClient) -> None:
    """An over-scale tax row and an out-of-ceiling money row are per-row errors; the batch is
    not poisoned (a valid row alongside them still prices)."""
    _as(client, "admin")
    proj = _project_id(client)
    ok = _new_product(client, "Good Row", code="OK-1")
    _new_product(client, "Bad Tax", code="BADTAX-1")
    _new_product(client, "Huge Money", code="HUGE-1")

    data = _xlsx(
        ["product_code", "cost_price", "tax_rate"],
        [
            ["OK-1", 100.00, 18],           # valid
            ["BADTAX-1", 100.00, "18.999"], # over-scale tax
            ["HUGE-1", 10**14, 18],         # rupees -> 10**16 paise, over the ceiling
        ],
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["priced"] == ["OK-1"]
    msgs = {e["row"]: e["message"] for e in body["errors"]}
    assert msgs[3] == "tax_rate supports at most 2 decimal places"
    assert "too large" in msgs[4]
    # The valid row DID persist; the bad ones did not tag.
    assert [row["product_id"] for row in
            client.get("/api/v1/project-products", params={"project_id": proj}).json()] \
        == [int(ok["id"])]


def test_bulk_upload_missing_project_is_400(client: TestClient) -> None:
    _as(client, "admin")
    data = _xlsx(["product_code", "cost_price"], [["X", 1.00]])
    r = _upload(client, 999999, data)
    assert r.status_code == 400, r.text


def test_bulk_upload_viewer_403_and_template_needs_view(client: TestClient) -> None:
    _as(client, "admin")
    proj = _project_id(client)
    _new_product(client, "Guarded", code="GUARD-1")
    data = _xlsx(["product_code", "cost_price"], [["GUARD-1", 10.00]])

    _as(client, "viewer")  # VIEW only -> no product.tag
    up = _upload(client, proj, data)
    assert up.status_code == 403
    # Template download only needs module VIEW -> a viewer CAN download it.
    dl = client.get("/api/v1/project-products/bulk-template.xlsx")
    assert dl.status_code == 200


# ------------------------------------------- tax_rate defaults from product gst_rate

def test_tag_defaults_tax_rate_from_product_gst(client: TestClient) -> None:
    # A NEW tag with NO tax_rate inherits the product's gst_rate (product -> template -> PO).
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Gst Mixer", gst_rate=18)

    r = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["tax_rate"])) == Decimal("18")
    assert Decimal(str(_sole(client, proj)["tax_rate"])) == Decimal("18")


def test_tag_explicit_tax_rate_not_overridden_by_gst(client: TestClient) -> None:
    # An explicit tax_rate always wins over the product's gst_rate default.
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Explicit Tax", gst_rate=18)

    r = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid, "tax_rate": "12"},
    )
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["tax_rate"])) == Decimal("12")   # NOT defaulted to 18


def test_tag_explicit_zero_tax_rate_preserved(client: TestClient) -> None:
    # An explicit 0% (nil-rated) is a real choice and must NOT be defaulted away.
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Nil Rated", gst_rate=18)

    r = client.post(
        "/api/v1/project-products",
        json={"project_id": proj, "product_id": pid, "tax_rate": "0"},
    )
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["tax_rate"])) == Decimal("0")   # 0 preserved, NOT -> 18


def test_tag_no_product_gst_leaves_tax_unset(client: TestClient) -> None:
    # No product gst_rate to default from -> the template's tax_rate stays unset (None).
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "No Gst Product")  # gst_rate defaults to None

    r = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert r.status_code == 201, r.text
    assert r.json()["tax_rate"] is None
    assert _sole(client, proj)["tax_rate"] is None


def test_idempotent_retag_keeps_defaulted_tax_rate(client: TestClient) -> None:
    # First tag defaults tax_rate from gst (18); a re-tag is idempotent and never re-defaults
    # or clobbers the stored value.
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Retag Gst", gst_rate=18)
    first = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert first.status_code == 201, first.text
    assert Decimal(str(first.json()["tax_rate"])) == Decimal("18")

    again = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert again.status_code == 200, again.text
    assert Decimal(str(again.json()["tax_rate"])) == Decimal("18")   # unchanged


def test_update_template_omitting_tax_rate_leaves_it_unchanged(client: TestClient) -> None:
    # Editing an existing template must NEVER re-default: a PATCH that omits tax_rate leaves
    # the stored value untouched (partial update).
    _as(client, "admin")
    proj = _project_id(client)
    pid = _make_product(client, "Edit Keeps Tax", gst_rate=18)
    client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert Decimal(str(_sole(client, proj)["tax_rate"])) == Decimal("18")

    r = client.patch(f"/api/v1/project-products/{proj}/{pid}", json={"cost_price_paise": 3500})
    assert r.status_code == 200, r.text
    assert r.json()["cost_price_paise"] == 3500
    assert Decimal(str(r.json()["tax_rate"])) == Decimal("18")   # untouched, not re-defaulted


def test_bulk_new_tag_inherits_product_gst_default(client: TestClient) -> None:
    # The bulk path routes NEW tags through tag_product: a row with a BLANK tax_rate on a new
    # tag inherits the product's gst_rate default.
    _as(client, "admin")
    proj = _project_id(client)
    prod = _new_product(client, "Bulk Gst", code="BULK-GST-1", gst_rate=18)

    data = _xlsx(
        ["product_code", "cost_price", "tax_rate"],
        [["BULK-GST-1", 250.00, ""]],   # blank tax cell -> inherit product gst_rate
    )
    r = _upload(client, proj, data)
    assert r.status_code == 200, r.text
    assert r.json()["priced"] == ["BULK-GST-1"]

    row = _row_for(client, proj, int(prod["id"]))
    assert row["cost_price_paise"] == 25000
    assert Decimal(str(row["tax_rate"])) == Decimal("18")   # inherited default


def test_bulk_blank_tax_on_existing_tag_does_not_redefault(client: TestClient) -> None:
    # An EXISTING tag priced at 5% must NOT jump to the product's 18% when a later bulk row
    # leaves the tax cell blank — the default fires ONLY on tag creation, never on re-price.
    _as(client, "admin")
    proj = _project_id(client)
    prod = _new_product(client, "Existing Priced", code="EXIST-TAX-1", gst_rate=18)

    # 1) Tag + price it at an EXPLICIT 5% (creates the tag with tax 5, not the product's 18).
    r1 = _upload(client, proj, _xlsx(
        ["product_code", "cost_price", "tax_rate"], [["EXIST-TAX-1", 100.00, 5]]))
    assert r1.status_code == 200, r1.text
    assert Decimal(str(_row_for(client, proj, int(prod["id"]))["tax_rate"])) == Decimal("5")

    # 2) Re-price the SAME product with a BLANK tax cell — the stored 5% must stay 5%.
    r2 = _upload(client, proj, _xlsx(
        ["product_code", "cost_price"], [["EXIST-TAX-1", 200.00]]))
    assert r2.status_code == 200, r2.text
    row = _row_for(client, proj, int(prod["id"]))
    assert row["cost_price_paise"] == 20000               # cost updated
    assert Decimal(str(row["tax_rate"])) == Decimal("5")  # tax NOT re-defaulted to 18
