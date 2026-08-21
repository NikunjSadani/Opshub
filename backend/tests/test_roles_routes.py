"""Roles CRUD + /me integration — the RBAC v2 admin surface over the real app wiring."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module is registered in the REGISTRY (grantable modules) and
# every table is on Base before create_all.
import app.main  # noqa: F401,E402
import app.modules.numbering.models  # noqa: F401,E402
import app.platform.models  # noqa: F401,E402
from app.db import Base, get_db
from app.modules.me.routes import router as me_router
from app.modules.roles.routes import router as roles_router
from app.modules.users.routes import router as users_router
from app.platform.auth import current_user
from app.platform.models import Role, User
from app.platform.roles_builtin import ensure_builtin_roles


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
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
    app.include_router(roles_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.state.TestSession = TestSession
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, uid: str) -> None:
    db = client.app.state.TestSession()
    # Eager-load the role + its grants so rbac's attribute access needs no live session.
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


def test_admin_lists_roles_incl_protected_administrator(client: TestClient) -> None:
    _as(client, "admin")
    r = client.get("/api/v1/roles")
    assert r.status_code == 200, r.text
    roles = {x["name"]: x for x in r.json()}
    assert roles["Administrator"]["is_system"] is True
    assert roles["Administrator"]["user_count"] == 1
    # A preset carries its module levels.
    assert roles["Challan Operator"]["module_levels"] == {"document_automation": "OPERATE"}


def test_assignable_modules(client: TestClient) -> None:
    _as(client, "admin")
    r = client.get("/api/v1/roles/assignable-modules")
    assert r.status_code == 200
    keys = {m["key"] for m in r.json()}
    assert keys == {"document_automation", "projects", "expense_invoice", "sales_orders",
                    "billing", "finance", "logistics"}


def test_create_edit_role(client: TestClient) -> None:
    _as(client, "admin")
    r = client.post("/api/v1/roles", json={
        "name": "Ops Clerk", "description": "desk work",
        "module_levels": {"document_automation": "OPERATE", "expense_invoice": "VIEW"},
        "platform": [],
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["module_levels"] == {"document_automation": "OPERATE",
                                         "expense_invoice": "VIEW"}
    # edit: bump to MANAGE + grant settings
    r2 = client.patch(f"/api/v1/roles/{rid}", json={
        "name": "Ops Clerk", "description": "desk work",
        "module_levels": {"document_automation": "MANAGE"}, "platform": ["settings"],
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["module_levels"] == {"document_automation": "MANAGE"}
    assert r2.json()["platform"] == ["settings"]


def test_administrator_role_is_protected(client: TestClient) -> None:
    _as(client, "admin")
    admin_role = next(x for x in client.get("/api/v1/roles").json() if x["is_system"])
    rid = admin_role["id"]
    assert client.patch(f"/api/v1/roles/{rid}", json={
        "name": "X", "module_levels": {}, "platform": []}).status_code == 409
    assert client.delete(f"/api/v1/roles/{rid}").status_code == 409


def test_bad_level_400_and_unknown_module_400(client: TestClient) -> None:
    _as(client, "admin")
    assert client.post("/api/v1/roles", json={
        "name": "Bad1", "module_levels": {"document_automation": "SUPERUSER"},
        "platform": []}).status_code == 400
    assert client.post("/api/v1/roles", json={
        "name": "Bad2", "module_levels": {"no_such_module": "VIEW"},
        "platform": []}).status_code == 400


def test_role_in_use_cannot_be_deleted(client: TestClient) -> None:
    _as(client, "admin")
    # 'Viewer' preset is held by the seeded viewer user.
    viewer_role = next(x for x in client.get("/api/v1/roles").json() if x["name"] == "Viewer")
    assert client.delete(f"/api/v1/roles/{viewer_role['id']}").status_code == 409


def test_non_iam_user_forbidden(client: TestClient) -> None:
    _as(client, "viewer")
    assert client.get("/api/v1/roles").status_code == 403
    assert client.post("/api/v1/roles", json={"name": "N", "module_levels": {},
                                              "platform": []}).status_code == 403


def test_role_can_be_cleared_to_zero_grants(client: TestClient) -> None:
    _as(client, "admin")
    rid = client.post("/api/v1/roles", json={
        "name": "Temp", "module_levels": {"document_automation": "MANAGE"},
        "platform": ["iam"]}).json()["id"]
    # Editing down to NO grants must persist as an empty set (no stale rows left behind).
    r = client.patch(f"/api/v1/roles/{rid}", json={
        "name": "Temp", "module_levels": {}, "platform": []})
    assert r.status_code == 200, r.text
    assert r.json()["module_levels"] == {} and r.json()["platform"] == []


def test_role_name_clash_is_case_insensitive(client: TestClient) -> None:
    _as(client, "admin")
    assert client.post("/api/v1/roles", json={
        "name": "Ops", "module_levels": {}, "platform": []}).status_code == 201
    # "ops" collides with "Ops" (case-insensitive) -> 409, not a second role.
    assert client.post("/api/v1/roles", json={
        "name": "ops", "module_levels": {}, "platform": []}).status_code == 409


def test_delete_nonexistent_role_404(client: TestClient) -> None:
    _as(client, "admin")
    assert client.delete("/api/v1/roles/999999").status_code == 404


def test_roleless_user_is_fully_denied(client: TestClient) -> None:
    # A user whose role_id is NULL (e.g. a migrated non-admin) has NO access anywhere.
    db = client.app.state.TestSession()
    db.add(User(firebase_uid="roleless", email="none@t.local", name="None",
                role_id=None, active=True))
    db.commit()
    db.close()
    _as(client, "roleless")
    me = client.get("/api/v1/me").json()
    assert me["role_name"] is None
    assert me["module_levels"] == {} and me["platform"] == []
    assert me["is_administrator"] is False
    assert client.get("/api/v1/roles").status_code == 403  # no iam


def test_me_reflects_effective_permissions(client: TestClient) -> None:
    _as(client, "admin")
    me = client.get("/api/v1/me").json()
    assert me["is_administrator"] is True
    assert me["module_levels"]["document_automation"] == "MANAGE"
    assert set(me["platform"]) == {"iam", "settings"}

    _as(client, "viewer")
    me = client.get("/api/v1/me").json()
    assert me["is_administrator"] is False
    assert me["module_levels"] == {"document_automation": "VIEW", "projects": "VIEW",
                                   "expense_invoice": "VIEW", "sales_orders": "VIEW",
                                   "billing": "VIEW", "finance": "VIEW",
                                   "logistics": "VIEW"}
    assert me["platform"] == []
