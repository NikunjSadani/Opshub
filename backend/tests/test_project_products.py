"""Project-Product tags — tag/untag/list + picker curation + RBAC, over real wiring.

Exercises the money-free curation surface: tagging a shared Product to a Project
(idempotent), the management list (ALL tags incl. inactive), and the extended
`/products` picker (empty `q` + `project_id` => curated tagged-only default; a
non-blank `q` => full catalogue). RBAC split: reads need sales_orders >= View,
writes need `product.tag` (OPERATE). Uses an in-memory sqlite with FKs enforced, so
the `uq_project_product` unique constraint and the FK cascades are genuinely tested.
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
    assert r.json()["id"] == pid

    listed = client.get("/api/v1/project-products", params={"project_id": proj})
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [pid]

    # Re-tagging the same pair is idempotent: 200 (not 201), still exactly one row.
    again = client.post("/api/v1/project-products", json={"project_id": proj, "product_id": pid})
    assert again.status_code == 200, again.text
    assert again.json()["id"] == pid
    listed2 = client.get("/api/v1/project-products", params={"project_id": proj})
    assert [p["id"] for p in listed2.json()] == [pid]


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
    assert [p["id"] for p in mgmt.json()] == [inactive]
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
