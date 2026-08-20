"""Product Master — create/dedupe/search/update + RBAC, over the real app wiring.

Exercises the case-insensitive identity dedup (the `uq_product_identity` DB index),
the optional-`code` uniqueness, LIKE-escaped search, the patch surface, and the
RBAC split (reads need the sales_orders module >= View; writes need Manage). Uses
an in-memory sqlite whose schema includes the functional unique index, so the
dedup backstop is genuinely under test.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module + table is registered on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.sales_orders.routes import router as sales_orders_router
from app.platform.auth import current_user
from app.platform.models import Role, User
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
    seed.add(User(firebase_uid="admin", email="admin@t.local", name="Admin",
                  role_id=roles["Administrator"].id, active=True))
    seed.add(User(firebase_uid="viewer", email="viewer@t.local", name="Viewer",
                  role_id=roles["Viewer"].id, active=True))
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
    assert body["code"] is None  # empty optional omitted -> null


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
