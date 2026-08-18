"""User Management module over HTTP: admin-gated CRUD + lockout guards.

Auth + DB are dependency-overridden (no Firebase / Postgres). The provisioner is
selected via env (default local -> LocalProvisioner), so created users get a
`local:` uid (is_provisioned=False) and no setup_link. Proves: admin-only access,
duplicate-email 409, invalid module_key 400, role/status/module diffs apply +
audit, and both lockout guards (last-active-admin, self-lockout).

Importing app.main populates the module REGISTRY so `projects` is an assignable
module key (idempotent — the import is cached across the suite).
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.main  # noqa: F401 - populates the module REGISTRY (assignable keys)
from app.db import Base, get_db
from app.modules.users import service
from app.modules.users.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

# The default authenticated admin. id=999 is deliberately NOT a DB row, so acting
# on OTHER users never trips the self-lockout guard; role/active satisfy rbac.
ADMIN = User(id=999, firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
NON_ADMIN = User(
    firebase_uid="mod", email="m@x.com", name="M", role=Role.MIS, active=True,
    module_access=[UserModuleAccess(module_key="projects")],
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[User.__table__, UserModuleAccess.__table__, AuditLog.__table__],
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[current_user] = lambda: ADMIN
    app.state.TestSession = TestSession
    yield TestClient(app)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _create(client: TestClient, **body: object) -> dict:
    payload = {"email": "x@x.com", "name": "X", "role": "OPERATIONS", "module_keys": []}
    payload.update(body)
    return client.post("/api/v1/users", json=payload)


def _actions(client: TestClient) -> set[str]:
    db = client.app.state.TestSession()
    rows = {a.action for a in db.execute(select(AuditLog)).scalars()}
    db.close()
    return rows


# ------------------------------------------------------------------- create

def test_admin_creates_user_with_grants(client: TestClient) -> None:
    r = _create(
        client, email="Jane@Example.com", name=" Jane  Doe ", role="MIS",
        module_keys=["projects"],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    user, link = body["user"], body["setup_link"]
    assert user["email"] == "jane@example.com"  # normalized lowercase
    assert user["name"] == "Jane Doe"           # whitespace collapsed
    assert user["role"] == "MIS"
    assert user["active"] is True
    assert user["module_keys"] == ["projects"]
    assert user["is_provisioned"] is False       # LocalProvisioner -> local: uid
    assert link is None                          # no setup link locally
    assert "firebase_uid" not in user            # raw uid never exposed

    # a UserModuleAccess row was persisted + create was audited
    db = client.app.state.TestSession()
    grants = db.execute(select(UserModuleAccess)).scalars().all()
    db.close()
    assert [g.module_key for g in grants] == ["projects"]
    assert "user.created" in _actions(client)


def test_duplicate_email_conflicts(client: TestClient) -> None:
    assert _create(client, email="dup@x.com").status_code == 201
    # case-insensitive duplicate
    r = _create(client, email="DUP@x.com")
    assert r.status_code == 409, r.text


def test_invalid_module_key_rejected(client: TestClient) -> None:
    r = _create(client, module_keys=["projects", "not_a_module"])
    assert r.status_code == 400, r.text


def test_invalid_role_rejected(client: TestClient) -> None:
    assert _create(client, role="SUPERUSER").status_code == 400


def test_invalid_email_rejected(client: TestClient) -> None:
    assert _create(client, email="not-an-email").status_code == 400


# --------------------------------------------------------------------- authz

def test_non_admin_forbidden_on_all(client: TestClient) -> None:
    _as(client, NON_ADMIN)
    assert client.get("/api/v1/users").status_code == 403
    assert _create(client).status_code == 403
    assert client.patch("/api/v1/users/1", json={"name": "Y"}).status_code == 403


# --------------------------------------------------------------------- update

def test_patch_role_status_and_modules(client: TestClient) -> None:
    uid = _create(client, email="u@x.com", role="OPERATIONS", module_keys=[]).json()["user"]["id"]

    r = client.patch(
        f"/api/v1/users/{uid}",
        json={"role": "FINANCE", "active": False, "module_keys": ["projects"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["role"] == "FINANCE"
    assert out["active"] is False
    assert out["module_keys"] == ["projects"]

    acts = _actions(client)
    assert {"user.role_changed", "user.status_changed", "user.module_access_changed"} <= acts


def test_patch_missing_user_404(client: TestClient) -> None:
    assert client.patch("/api/v1/users/123456", json={"name": "Z"}).status_code == 404


def test_module_access_diff_add_and_remove(client: TestClient) -> None:
    uid = _create(
        client, email="d@x.com", module_keys=["projects"],
    ).json()["user"]["id"]
    # swap projects -> document_automation (one add, one remove)
    r = client.patch(f"/api/v1/users/{uid}", json={"module_keys": ["document_automation"]})
    assert r.status_code == 200, r.text
    assert r.json()["module_keys"] == ["document_automation"]

    db = client.app.state.TestSession()
    keys = {g.module_key for g in db.execute(select(UserModuleAccess)).scalars()}
    db.close()
    assert keys == {"document_automation"}  # projects grant was deleted


def test_enable_disable_round_trip(client: TestClient) -> None:
    uid = _create(client, email="rt@x.com").json()["user"]["id"]
    assert client.patch(f"/api/v1/users/{uid}", json={"active": False}).json()["active"] is False
    assert client.patch(f"/api/v1/users/{uid}", json={"active": True}).json()["active"] is True


# ---------------------------------------------------------------- lockout guards

def test_last_active_admin_cannot_be_disabled(client: TestClient) -> None:
    # exactly one admin in the DB; actor (id=999) is NOT that admin -> isolates guard (a)
    admin_id = _create(client, email="solo@x.com", role="ADMIN").json()["user"]["id"]
    r = client.patch(f"/api/v1/users/{admin_id}", json={"active": False})
    assert r.status_code == 409, r.text


def test_last_active_admin_cannot_be_demoted(client: TestClient) -> None:
    admin_id = _create(client, email="solo2@x.com", role="ADMIN").json()["user"]["id"]
    r = client.patch(f"/api/v1/users/{admin_id}", json={"role": "OPERATIONS"})
    assert r.status_code == 409, r.text


def test_second_admin_allows_disabling_the_first(client: TestClient) -> None:
    a = _create(client, email="a1@x.com", role="ADMIN").json()["user"]["id"]
    _create(client, email="a2@x.com", role="ADMIN")  # now two active admins
    # disabling the first is allowed — the second remains
    assert client.patch(f"/api/v1/users/{a}", json={"active": False}).status_code == 200


def test_self_disable_blocked(client: TestClient) -> None:
    # two admins so guard (a) would NOT fire -> isolates the self guard (b)
    me = _create(client, email="me@x.com", role="ADMIN").json()["user"]["id"]
    _create(client, email="other@x.com", role="ADMIN")
    _as(client, User(id=me, firebase_uid="me", email="me@x.com", name="Me",
                     role=Role.ADMIN, active=True))
    r = client.patch(f"/api/v1/users/{me}", json={"active": False})
    assert r.status_code == 409, r.text


def test_self_demote_blocked(client: TestClient) -> None:
    me = _create(client, email="me2@x.com", role="ADMIN").json()["user"]["id"]
    _create(client, email="other2@x.com", role="ADMIN")
    _as(client, User(id=me, firebase_uid="me2", email="me2@x.com", name="Me",
                     role=Role.ADMIN, active=True))
    r = client.patch(f"/api/v1/users/{me}", json={"role": "MIS"})
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------- setup-link

def test_setup_link_endpoint_local_returns_none(client: TestClient) -> None:
    uid = _create(client, email="sl@x.com").json()["user"]["id"]
    r = client.post(f"/api/v1/users/{uid}/setup-link")
    assert r.status_code == 200, r.text
    assert r.json() == {"setup_link": None}
    assert client.post("/api/v1/users/999999/setup-link").status_code == 404


# --------------------------------------- provisioning saga (orphan compensation)

class _SpyProvisioner:
    """Records provision/delete calls; can be told to fail link generation."""

    def __init__(self, *, link: str | None = None, link_raises: bool = False) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._link = link
        self._link_raises = link_raises

    def create_auth_user(self, email: str) -> str:
        uid = f"fb:{email}"
        self.created.append(uid)
        return uid

    def delete_auth_user(self, uid: str) -> None:
        self.deleted.append(uid)

    def password_setup_link(self, email: str) -> str | None:
        if self._link_raises:
            raise RuntimeError("link boom")
        return self._link


def test_create_survives_setup_link_failure(client: TestClient) -> None:
    # A transient link-generation failure must NOT roll back an already-committed
    # user (they can re-issue the link) and must NOT compensate-delete the account.
    db = client.app.state.TestSession()
    spy = _SpyProvisioner(link_raises=True)
    user, link = service.create_user(
        db, email="lf@x.com", name="LF", role="OPERATIONS", module_keys=[],
        actor_uid="adm", provisioner=spy)
    assert link is None
    assert spy.deleted == []                       # user kept -> no compensation
    assert db.get(User, user.id) is not None
    db.close()


def test_create_compensates_orphan_on_persist_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If persistence fails AFTER the auth account is provisioned, the account must be
    # deleted (never leave a Firebase orphan that bricks the email forever).
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("audit boom")

    monkeypatch.setattr(service.audit, "log", _boom)
    db = client.app.state.TestSession()
    spy = _SpyProvisioner()
    with pytest.raises(RuntimeError):
        service.create_user(
            db, email="orphan@x.com", name="O", role="OPERATIONS", module_keys=[],
            actor_uid="adm", provisioner=spy)
    assert spy.created and spy.deleted == spy.created   # provisioned account rolled back
    db.close()
    db2 = client.app.state.TestSession()
    assert db2.execute(
        select(User).where(User.email == "orphan@x.com")
    ).scalar_one_or_none() is None                      # no half-created user persisted
    db2.close()


def test_setup_link_issuance_is_audited(client: TestClient) -> None:
    # (Re)issuing a set-password link is account-takeover-capable → it must be audited,
    # and the link itself must never be written to the trail.
    uid = _create(client, email="audit-sl@x.com").json()["user"]["id"]
    client.post(f"/api/v1/users/{uid}/setup-link")
    assert "user.setup_link_issued" in _actions(client)
    db = client.app.state.TestSession()
    rows = [a for a in db.execute(select(AuditLog)).scalars()
            if a.action == "user.setup_link_issued"]
    db.close()
    assert rows and all("http" not in str(a.detail).lower() for a in rows)  # no link in trail
