"""Product Master — create/dedupe/search/update + RBAC, over the real app wiring.

Exercises the case-insensitive identity dedup (the `uq_product_identity` DB index),
the optional-`code` uniqueness, LIKE-escaped search, the patch surface, and the
RBAC split (reads need the sales_orders module >= View; writes need Manage). Uses
an in-memory sqlite whose schema includes the functional unique index, so the
dedup backstop is genuinely under test.
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
from app.modules.sales_orders.routes import router as sales_orders_router
from app.platform.auth import current_user
from app.platform.models import Level, Role, RoleModulePermission, User
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
    # A non-admin OPERATE role on sales_orders — used to prove the bulk endpoints are gated
    # at MANAGE (OPERATE is still 403), not merely at OPERATE.
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
        select(User)
        .options(
            selectinload(User.role).selectinload(Role.module_permissions),
            selectinload(User.role).selectinload(Role.platform_permissions),
        )
        .where(User.firebase_uid == uid)
    ).scalar_one()
    db.close()
    client.app.dependency_overrides[current_user] = lambda: user


def test_create_returns_normalized_product(client: TestClient) -> None:
    _as(client, "admin")
    r = client.post("/api/v1/products", json={
        "name": "  Mixer Grinder ", "brand": "Bajaj", "model_number": "GX-1",
        "category": "Appliances", "uom": "PCS", "hsn": "8509"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Mixer Grinder"  # stripped
    assert body["brand"] == "Bajaj" and body["model_number"] == "GX-1"
    assert body["active"] is True and body["id"] > 0
    assert body["code"] == f"PRD-{body['id']:06d}"  # code-less create -> auto-minted


def test_duplicate_identity_is_409_case_insensitive(client: TestClient) -> None:
    _as(client, "admin")
    assert client.post("/api/v1/products", json={"name": "Mixer"}).status_code == 201
    # "mixer" collides with "Mixer" (identity is lower(name)/brand/model) -> 409.
    r = client.post("/api/v1/products", json={"name": "mixer"})
    assert r.status_code == 409, r.text
    # Same name but a distinct model_number is a DIFFERENT identity -> allowed.
    assert client.post(
        "/api/v1/products", json={"name": "Mixer", "model_number": "v2"}
    ).status_code == 201


def test_duplicate_code_is_409(client: TestClient) -> None:
    _as(client, "admin")
    assert client.post(
        "/api/v1/products", json={"name": "Widget A", "code": "SKU1"}
    ).status_code == 201
    r = client.post("/api/v1/products", json={"name": "Widget B", "code": "SKU1"})
    assert r.status_code == 409, r.text


def test_list_search_and_filter(client: TestClient) -> None:
    _as(client, "admin")
    client.post("/api/v1/products", json={"name": "Steel Bolt", "brand": "Acme",
                                          "category": "Hardware"})
    client.post("/api/v1/products", json={"name": "Copper Wire", "brand": "Bolt Co",
                                          "category": "Electrical"})
    client.post("/api/v1/products", json={"name": "Plastic Clip", "category": "Hardware"})

    # substring `q` matches across name/brand (both "Bolt" rows).
    names = {p["name"] for p in client.get("/api/v1/products", params={"q": "bolt"}).json()}
    assert names == {"Steel Bolt", "Copper Wire"}
    # category filter is case-insensitive exact.
    cats = {p["name"] for p in client.get("/api/v1/products",
                                          params={"category": "hardware"}).json()}
    assert cats == {"Steel Bolt", "Plastic Clip"}


def test_active_filter_and_get_one(client: TestClient) -> None:
    _as(client, "admin")
    pid = client.post("/api/v1/products", json={"name": "Retired Item"}).json()["id"]
    client.patch(f"/api/v1/products/{pid}", json={"active": False})
    assert client.get("/api/v1/products", params={"active": True}).json() == []
    inactive = client.get("/api/v1/products", params={"active": False}).json()
    assert len(inactive) == 1 and inactive[0]["id"] == pid
    one = client.get(f"/api/v1/products/{pid}")
    assert one.status_code == 200 and one.json()["active"] is False
    assert client.get("/api/v1/products/999999").status_code == 404


def test_update_fields_and_clear_optional(client: TestClient) -> None:
    _as(client, "admin")
    pid = client.post("/api/v1/products", json={"name": "Old", "brand": "X",
                                                "hsn": "1000"}).json()["id"]
    r = client.patch(f"/api/v1/products/{pid}", json={"name": "New Name", "brand": None})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New Name"
    assert r.json()["brand"] is None  # explicit null cleared it
    assert r.json()["hsn"] == "1000"  # omitted -> untouched


def test_update_into_duplicate_identity_is_409(client: TestClient) -> None:
    _as(client, "admin")
    client.post("/api/v1/products", json={"name": "Alpha"})
    pid = client.post("/api/v1/products", json={"name": "Beta"}).json()["id"]
    # Renaming Beta -> "alpha" collides with Alpha's identity -> 409.
    assert client.patch(f"/api/v1/products/{pid}", json={"name": "alpha"}).status_code == 409


def test_viewer_can_read_but_not_write(client: TestClient) -> None:
    _as(client, "admin")
    pid = client.post("/api/v1/products", json={"name": "Seen"}).json()["id"]
    _as(client, "viewer")
    assert client.get("/api/v1/products").status_code == 200  # VIEW allowed
    assert client.get(f"/api/v1/products/{pid}").status_code == 200
    assert client.post("/api/v1/products", json={"name": "Nope"}).status_code == 403
    assert client.patch(f"/api/v1/products/{pid}", json={"name": "Nope"}).status_code == 403


# ------------------------------------------------- system-generated product codes

def test_create_without_code_auto_mints_prd_id(client: TestClient) -> None:
    _as(client, "admin")
    body = client.post("/api/v1/products", json={"name": "Auto One"}).json()
    assert body["code"] == f"PRD-{body['id']:06d}"  # e.g. PRD-000001


def test_create_with_user_code_is_kept(client: TestClient) -> None:
    _as(client, "admin")
    body = client.post("/api/v1/products", json={"name": "User Coded", "code": "ABC-1"}).json()
    assert body["code"] == "ABC-1"  # user code kept verbatim, never overwritten


def test_create_with_reserved_code_is_422(client: TestClient) -> None:
    _as(client, "admin")
    r = client.post("/api/v1/products", json={"name": "Reserved", "code": "PRD-000001"})
    assert r.status_code == 422, r.text
    # case-insensitive: lowercase prefix is still the reserved shape.
    r2 = client.post("/api/v1/products", json={"name": "Reserved 2", "code": "prd-123456"})
    assert r2.status_code == 422, r2.text


def test_update_into_reserved_code_is_422(client: TestClient) -> None:
    _as(client, "admin")
    pid = client.post("/api/v1/products", json={"name": "Patch Me", "code": "OK-1"}).json()["id"]
    r = client.patch(f"/api/v1/products/{pid}", json={"code": "PRD-000009"})
    assert r.status_code == 422, r.text


def test_create_with_prd_prefix_but_not_reserved_shape_is_allowed(client: TestClient) -> None:
    _as(client, "admin")
    # Only `^PRD-\d{6,}$` is reserved; PRD-XY (letters) and PRD-12345 (5 digits) are user codes.
    a = client.post("/api/v1/products", json={"name": "Prefix A", "code": "PRD-XY"})
    assert a.status_code == 201, a.text
    assert a.json()["code"] == "PRD-XY"
    b = client.post("/api/v1/products", json={"name": "Prefix B", "code": "PRD-12345"})
    assert b.status_code == 201, b.text
    assert b.json()["code"] == "PRD-12345"


def test_create_with_seven_digit_reserved_code_is_422(client: TestClient) -> None:
    # The auto namespace widens past 6 digits for ids >= 1,000,000 (PRD-1000000), so a
    # 7+ digit code in the auto shape is reserved too — otherwise a user could hand-enter
    # a code that a future auto-mint would collide with.
    _as(client, "admin")
    r = client.post("/api/v1/products", json={"name": "Seven Digit", "code": "PRD-1000000"})
    assert r.status_code == 422, r.text


def test_two_codeless_products_get_distinct_auto_codes_and_identity_dedup_holds(
    client: TestClient,
) -> None:
    _as(client, "admin")
    a = client.post("/api/v1/products", json={"name": "Distinct A"}).json()
    b = client.post("/api/v1/products", json={"name": "Distinct B"}).json()
    assert a["code"] != b["code"]
    assert a["code"] == f"PRD-{a['id']:06d}" and b["code"] == f"PRD-{b['id']:06d}"
    # Identity dedup still enforced independently of the code.
    dup = client.post("/api/v1/products", json={"name": "distinct a"})
    assert dup.status_code == 409, dup.text


def test_migration_backfills_codeless_products() -> None:
    """Round-trip the backfill migration on sqlite: a pre-existing code-less row is
    minted `PRD-{id:06d}` on upgrade and cleared again on downgrade, while a user code
    (and a user code already sitting in another row's target slot) is preserved."""
    import importlib.util
    from pathlib import Path

    from sqlalchemy import create_engine, text

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mig_path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "d4e6f8a1b3c5_backfill_product_auto_codes.py"
    )
    spec = importlib.util.spec_from_file_location("_backfill_product_auto_codes", mig_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite://", poolclass=StaticPool, future=True)
    with engine.begin() as conn:
        # Minimal `product` table (only the columns the migration touches).
        conn.execute(text(
            "CREATE TABLE product (id INTEGER PRIMARY KEY, code VARCHAR(40) UNIQUE)"
        ))
        conn.execute(text("INSERT INTO product (id, code) VALUES (1, NULL)"))   # -> PRD-000001
        conn.execute(text("INSERT INTO product (id, code) VALUES (2, 'SKU-2')"))  # user code kept
        # Row 3 is code-less but its target PRD-000003 is already held by row 4 (a
        # pre-existing user code in the auto shape) -> row 3 must be SKIPPED (left NULL).
        conn.execute(text("INSERT INTO product (id, code) VALUES (4, 'PRD-000003')"))
        conn.execute(text("INSERT INTO product (id, code) VALUES (3, NULL)"))

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

    with engine.begin() as conn:
        codes = dict(conn.execute(text("SELECT id, code FROM product ORDER BY id")).all())
    assert codes[1] == "PRD-000001"      # code-less -> minted
    assert codes[2] == "SKU-2"           # user code untouched
    assert codes[3] is None              # skipped: target already taken by row 4
    assert codes[4] == "PRD-000003"      # pre-existing user code untouched

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()

    with engine.begin() as conn:
        codes = dict(conn.execute(text("SELECT id, code FROM product ORDER BY id")).all())
    # Downgrade clears auto-shaped codes (row 1 and the pre-existing PRD-000003), keeps user code.
    assert codes[1] is None
    assert codes[2] == "SKU-2"
    assert codes[3] is None
    assert codes[4] is None
    engine.dispose()


# --------------------------------------------------------- bulk upsert (Excel)

_BULK_HEADER = ["name", "code", "brand", "model_number", "category", "uom", "hsn"]


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


def _upload(client: TestClient, data: bytes) -> object:
    return client.post(
        "/api/v1/products/upload",
        files={"file": ("products.xlsx", data, "application/xlsx")},
    )


def test_bulk_template_download(client: TestClient) -> None:
    """The template endpoint returns an .xlsx (correct content-type + attachment name) whose
    first sheet's header row is EXACTLY the parser's expected columns, in order."""
    _as(client, "admin")
    r = client.get("/api/v1/products/bulk-template.xlsx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert 'filename="products-bulk-template.xlsx"' in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    assert list(rows[0]) == _BULK_HEADER  # header matches the parser's exact columns, in order
    assert len(rows) == 2                 # header + one illustrative example row
    assert rows[1][0]                     # the example row carries a name


def test_bulk_template_requires_manage(client: TestClient) -> None:
    """The template is MANAGE-gated — a VIEW-only and an OPERATE user are both 403."""
    _as(client, "viewer")
    assert client.get("/api/v1/products/bulk-template.xlsx").status_code == 403
    _as(client, "operator")
    assert client.get("/api/v1/products/bulk-template.xlsx").status_code == 403


def test_bulk_upload_creates_new_product(client: TestClient) -> None:
    _as(client, "admin")
    data = _xlsx(_BULK_HEADER, [
        ["Blender Pro", "", "Acme", "BX-9", "Appliances", "PCS", "8509"],
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["updated"] == [] and out["errors"] == []
    assert len(out["created"]) == 1
    code = out["created"][0]
    assert code.startswith("PRD-")  # code-less row -> auto-minted
    # The product really exists with the row's fields.
    found = client.get("/api/v1/products", params={"q": "Blender"}).json()
    assert len(found) == 1
    assert found[0]["name"] == "Blender Pro" and found[0]["brand"] == "Acme"
    assert found[0]["code"] == code


def test_bulk_upload_updates_by_code(client: TestClient) -> None:
    _as(client, "admin")
    pid = client.post("/api/v1/products", json={
        "name": "Old Name", "code": "SKU-9", "brand": "OldBrand"}).json()["id"]
    data = _xlsx(_BULK_HEADER, [
        ["New Name", "SKU-9", "NewBrand", "", "Hardware", "BOX", ""],
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == [] and out["errors"] == []
    assert out["updated"] == ["SKU-9"]  # matched the existing product by its code
    body = client.get(f"/api/v1/products/{pid}").json()
    assert body["name"] == "New Name"       # descriptive fields updated
    assert body["brand"] == "NewBrand"
    assert body["uom"] == "BOX"
    assert body["code"] == "SKU-9"          # code unchanged


def test_bulk_upload_updates_by_identity(client: TestClient) -> None:
    _as(client, "admin")
    created = client.post("/api/v1/products", json={
        "name": "Ident Prod", "brand": "Zeta", "model_number": "M9",
        "category": "OldCat"}).json()
    auto_code = created["code"]
    # No code column value -> match by case-insensitive identity (name, brand, model_number).
    data = _xlsx(_BULK_HEADER, [
        ["ident prod", "", "ZETA", "m9", "NewCat", "", ""],
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == [] and out["errors"] == []
    assert out["updated"] == [auto_code]  # identity matched despite case differences
    body = client.get(f"/api/v1/products/{created['id']}").json()
    assert body["category"] == "NewCat"   # the provided field was updated
    assert body["brand"] == "ZETA"        # identity re-supplied (still the same product)


def test_bulk_upload_reserved_code_row_errors(client: TestClient) -> None:
    _as(client, "admin")
    data = _xlsx(_BULK_HEADER, [
        ["Reserved One", "PRD-999999", "Acme", "", "", "", ""],
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == [] and out["updated"] == []
    assert len(out["errors"]) == 1
    assert out["errors"][0]["row"] == 2
    assert "reserved" in out["errors"][0]["message"].lower()


def test_bulk_upload_blank_name_row_errors(client: TestClient) -> None:
    _as(client, "admin")
    # Not an all-blank row (code present) so it is processed — but a blank name is a row error.
    data = _xlsx(_BULK_HEADER, [
        ["", "X-1", "Acme", "", "", "", ""],
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == [] and out["updated"] == []
    assert len(out["errors"]) == 1
    assert out["errors"][0]["row"] == 2
    assert "name is required" in out["errors"][0]["message"].lower()


def test_bulk_upload_good_and_bad_row_isolated(client: TestClient) -> None:
    """A good + bad row mix: the good one processes, the bad one is a row error — the SAVEPOINT
    per row means the bad row never poisons the batch."""
    _as(client, "admin")
    data = _xlsx(_BULK_HEADER, [
        ["", "BAD-1", "Acme", "", "", "", ""],           # row 2: blank name -> error
        ["Good Widget", "", "Acme", "GW-1", "", "", ""],  # row 3: creates fine
    ])
    r = _upload(client, data)
    assert r.status_code == 201, r.text
    out = r.json()
    assert len(out["created"]) == 1 and out["updated"] == []
    assert len(out["errors"]) == 1 and out["errors"][0]["row"] == 2
    # The good row's product genuinely persisted (batch not rolled back).
    assert len(client.get("/api/v1/products", params={"q": "Good Widget"}).json()) == 1


def test_bulk_upload_requires_manage(client: TestClient) -> None:
    """The upload is MANAGE-gated — an OPERATE and a VIEW user are both 403 (nothing written)."""
    data = _xlsx(_BULK_HEADER, [["Nope", "", "", "", "", "", ""]])
    _as(client, "operator")
    assert _upload(client, data).status_code == 403
    _as(client, "viewer")
    assert _upload(client, data).status_code == 403
    _as(client, "admin")
    assert client.get("/api/v1/products").json() == []  # nothing was created by the 403s
