"""User Management module over HTTP: admin-gated CRUD + lockout guards (RBAC v2).

Auth + DB are dependency-overridden (no Firebase / Postgres). Users are assigned ONE
role (an ORM entity); the built-in roles are seeded via `ensure_builtin_roles`. The
provisioner is selected via env (default local -> LocalProvisioner), so created users
get a `local:` uid (is_provisioned=False) and no setup_link. Proves: admin-only access
(IAM), duplicate-email 409, nonexistent role_id 400, role/status diffs apply + audit,
and both lockout guards (last-active-admin holder, self-lockout).

Importing app.main populates the module REGISTRY + registers every table on Base.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.main  # noqa: F401 - populates the module REGISTRY + registers all tables
from app.db import Base, get_db
from app.modules.users import service
from app.modules.users.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Level, User
from app.platform.roles_builtin import ensure_builtin_roles
from tests.rbac_util import make_role, make_user

# The default authenticated admin. Holds the protected Administrator role (grants IAM).
# id=999 is deliberately NOT a DB row, so acting on OTHER users never trips self-lockout.
ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))
ADMIN.id = 999
# A non-admin (no IAM permission) — every users route must 403 for them.
NON_ADMIN = make_user("mod", role=make_role(module_levels={"projects": Level.OPERATE}))


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    seed.commit()
    role_ids = {name: r.id for name, r in roles.items()}
    seed.close()

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
    app.state.roles = role_ids
    yield TestClient(app)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _create(client: TestClient, *, role_id: int | None = None, **body: object) -> object:
    payload: dict[str, object] = {"email": "x@x.com", "name": "X"}
    payload["role_id"] = role_id if role_id is not None else client.app.state.roles["Finance"]
    payload.update(body)
    return client.post("/api/v1/users", json=payload)


def _actions(client: TestClient) -> set[str]:
    db = client.app.state.TestSession()
    rows = {a.action for a in db.execute(select(AuditLog)).scalars()}
    db.close()
    return rows


# ------------------------------------------------------------------- create

def test_admin_creates_user_with_role(client: TestClient) -> None:
    fin = client.app.state.roles["Finance"]
    r = _create(client, email="Jane@Example.com", name=" Jane  Doe ", role_id=fin)
    assert r.status_code == 201, r.text
    body = r.json()
    user, link = body["user"], body["setup_link"]
    assert user["email"] == "jane@example.com"  # normalized lowercase
    assert user["name"] == "Jane Doe"           # whitespace collapsed
    assert user["role_id"] == fin
    assert user["role_name"] == "Finance"
    assert user["active"] is True
    assert user["is_provisioned"] is False       # LocalProvisioner -> local: uid
    assert link is None                          # no setup link locally
    assert "firebase_uid" not in user            # raw uid never exposed
    assert "user.created" in _actions(client)


def test_duplicate_email_conflicts(client: TestClient) -> None:
    assert _create(client, email="dup@x.com").status_code == 201
    # case-insensitive duplicate
    r = _create(client, email="DUP@x.com")
    assert r.status_code == 409, r.text


def test_nonexistent_role_id_rejected(client: TestClient) -> None:
    r = _create(client, role_id=999999)
    assert r.status_code == 400, r.text


def test_invalid_email_rejected(client: TestClient) -> None:
    assert _create(client, email="not-an-email").status_code == 400


# --------------------------------------------------------------------- authz

def test_non_admin_forbidden_on_all(client: TestClient) -> None:
    _as(client, NON_ADMIN)
    assert client.get("/api/v1/users").status_code == 403
    assert _create(client).status_code == 403
    assert client.patch("/api/v1/users/1", json={"name": "Y"}).status_code == 403


# --------------------------------------------------------------------- update

def test_patch_role_and_status(client: TestClient) -> None:
    fin = client.app.state.roles["Finance"]
    chal = client.app.state.roles["Challan Operator"]
    uid = _create(client, email="u@x.com", role_id=fin).json()["user"]["id"]

    r = client.patch(f"/api/v1/users/{uid}", json={"role_id": chal, "active": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["role_id"] == chal
    assert out["role_name"] == "Challan Operator"
    assert out["active"] is False

    acts = _actions(client)
    assert {"user.role_changed", "user.status_changed"} <= acts


def test_patch_name_is_audited(client: TestClient) -> None:
    uid = _create(client, email="n@x.com", name="Old Name").json()["user"]["id"]
    r = client.patch(f"/api/v1/users/{uid}", json={"name": "New Name"})
    assert r.status_code == 200 and r.json()["name"] == "New Name"
    assert "user.updated" in _actions(client)


def test_patch_missing_user_404(client: TestClient) -> None:
    assert client.patch("/api/v1/users/123456", json={"name": "Z"}).status_code == 404


def test_enable_disable_round_trip(client: TestClient) -> None:
    uid = _create(client, email="rt@x.com").json()["user"]["id"]
    assert client.patch(f"/api/v1/users/{uid}", json={"active": False}).json()["active"] is False
    assert client.patch(f"/api/v1/users/{uid}", json={"active": True}).json()["active"] is True


# ---------------------------------------------------------------- lockout guards

def test_last_active_admin_cannot_be_disabled(client: TestClient) -> None:
    # exactly one Administrator-holder in the DB; actor (id=999) is NOT that admin -> guard (a)
    admin_role = client.app.state.roles["Administrator"]
    admin_id = _create(client, email="solo@x.com", role_id=admin_role).json()["user"]["id"]
    r = client.patch(f"/api/v1/users/{admin_id}", json={"active": False})
    assert r.status_code == 409, r.text


def test_last_active_admin_cannot_be_demoted(client: TestClient) -> None:
    admin_role = client.app.state.roles["Administrator"]
    fin = client.app.state.roles["Finance"]
    admin_id = _create(client, email="solo2@x.com", role_id=admin_role).json()["user"]["id"]
    r = client.patch(f"/api/v1/users/{admin_id}", json={"role_id": fin})
    assert r.status_code == 409, r.text


def test_second_admin_allows_disabling_the_first(client: TestClient) -> None:
    admin_role = client.app.state.roles["Administrator"]
    a = _create(client, email="a1@x.com", role_id=admin_role).json()["user"]["id"]
    _create(client, email="a2@x.com", role_id=admin_role)  # now two active admins
    # disabling the first is allowed — the second remains
    assert client.patch(f"/api/v1/users/{a}", json={"active": False}).status_code == 200


def test_self_disable_blocked(client: TestClient) -> None:
    # two admins so guard (a) would NOT fire -> isolates the self guard (b)
    admin_role = client.app.state.roles["Administrator"]
    me = _create(client, email="me@x.com", role_id=admin_role).json()["user"]["id"]
    _create(client, email="other@x.com", role_id=admin_role)
    acting = make_user("me", role=make_role("Administrator", is_system=True))
    acting.id = me
    _as(client, acting)
    r = client.patch(f"/api/v1/users/{me}", json={"active": False})
    assert r.status_code == 409, r.text


def test_self_demote_blocked(client: TestClient) -> None:
    admin_role = client.app.state.roles["Administrator"]
    fin = client.app.state.roles["Finance"]
    me = _create(client, email="me2@x.com", role_id=admin_role).json()["user"]["id"]
    _create(client, email="other2@x.com", role_id=admin_role)
    acting = make_user("me2", role=make_role("Administrator", is_system=True))
    acting.id = me
    _as(client, acting)
    r = client.patch(f"/api/v1/users/{me}", json={"role_id": fin})
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
    fin = client.app.state.roles["Finance"]
    db = client.app.state.TestSession()
    spy = _SpyProvisioner(link_raises=True)
    user, link = service.create_user(
        db, email="lf@x.com", name="LF", role_id=fin, actor_uid="adm", provisioner=spy)
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
    fin = client.app.state.roles["Finance"]
    db = client.app.state.TestSession()
    spy = _SpyProvisioner()
    with pytest.raises(RuntimeError):
        service.create_user(
            db, email="orphan@x.com", name="O", role_id=fin, actor_uid="adm", provisioner=spy)
    assert spy.created and spy.deleted == spy.created   # provisioned account rolled back
    db.close()
    db2 = client.app.state.TestSession()
    assert db2.execute(
        select(User).where(User.email == "orphan@x.com")
    ).scalar_one_or_none() is None                      # no half-created user persisted
    db2.close()


class _StubSender:
    """Records send() calls; can be told to raise (to prove best-effort delivery)."""

    def __init__(self, *, raises: bool = False) -> None:
        self.sent: list[dict[str, str | None]] = []
        self._raises = raises

    def send(
        self, to: str, subject: str, html_body: str, text_body: str | None = None
    ) -> None:
        if self._raises:
            raise RuntimeError("smtp boom")
        self.sent.append(
            {"to": to, "subject": subject, "html": html_body, "text": text_body}
        )


def test_invite_emails_setup_link_once(client: TestClient) -> None:
    # When a setup link is produced and a sender is supplied, the invite is emailed
    # EXACTLY once and the email carries the setup link.
    fin = client.app.state.roles["Finance"]
    db = client.app.state.TestSession()
    spy = _SpyProvisioner(link="https://setup.example/abc123")
    sender = _StubSender()
    user, link = service.create_user(
        db, email="inv@x.com", name="Invitee", role_id=fin, actor_uid="adm",
        provisioner=spy, email_sender=sender,
    )
    assert link == "https://setup.example/abc123"
    assert len(sender.sent) == 1
    assert "https://setup.example/abc123" in str(sender.sent[0]["html"])
    assert db.get(User, user.id) is not None
    db.close()


def test_invite_survives_email_send_failure(client: TestClient) -> None:
    # A raising sender must NOT break create: the user is durable and the link is
    # STILL returned for manual copy (email is best-effort, not a hard dependency).
    fin = client.app.state.roles["Finance"]
    db = client.app.state.TestSession()
    spy = _SpyProvisioner(link="https://setup.example/xyz789")
    sender = _StubSender(raises=True)
    user, link = service.create_user(
        db, email="inv2@x.com", name="Invitee2", role_id=fin, actor_uid="adm",
        provisioner=spy, email_sender=sender,
    )
    assert link == "https://setup.example/xyz789"       # link still returned
    assert db.get(User, user.id) is not None             # user still durable
    db.close()


def test_invite_no_email_when_no_link(client: TestClient) -> None:
    # No setup link (LocalProvisioner-style) -> nothing to email; sender untouched.
    fin = client.app.state.roles["Finance"]
    db = client.app.state.TestSession()
    spy = _SpyProvisioner(link=None)
    sender = _StubSender()
    _user, link = service.create_user(
        db, email="inv3@x.com", name="Invitee3", role_id=fin, actor_uid="adm",
        provisioner=spy, email_sender=sender,
    )
    assert link is None
    assert sender.sent == []
    db.close()


def test_reissue_setup_link_emails_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-issuing a setup link ALSO emails it (parity with create) — the staffer gets the
    # link by mail, not just on the admin's screen. Best-effort + inert until configured.
    from app.modules.users import routes as user_routes

    uid = _create(client, email="re@x.com", name="Reissue").json()["user"]["id"]
    spy = _SpyProvisioner(link="https://setup.example/reissue1")
    stub = _StubSender()
    monkeypatch.setattr(user_routes, "get_provisioner", lambda _s: spy)
    monkeypatch.setattr(user_routes, "get_email_sender", lambda _s: stub)

    r = client.post(f"/api/v1/users/{uid}/setup-link")
    assert r.status_code == 200, r.text
    assert r.json()["setup_link"] == "https://setup.example/reissue1"
    assert len(stub.sent) == 1
    assert stub.sent[0]["to"] == "re@x.com"
    assert "https://setup.example/reissue1" in str(stub.sent[0]["html"])


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


# ------------------------------------------ FirebaseProvisioner (SDK mocked out)

def test_firebase_create_auth_user_passes_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The account MUST be created with a (throwaway, random) password so it gains a
    # `password` provider — otherwise generate_password_reset_link produces a link
    # that fails as "expired or already used" and every invited staff member is
    # locked out. The user still sets their own password via that link.
    import firebase_admin.auth as fb_auth

    from app.modules.users import provisioner as prov
    from app.platform import auth as platform_auth

    monkeypatch.setattr(platform_auth, "_ensure_firebase", lambda: None)

    calls: dict[str, object] = {}

    class _Record:
        uid = "fb-uid-abc"

    def _fake_create_user(**kwargs: object) -> _Record:
        calls.update(kwargs)
        return _Record()

    monkeypatch.setattr(fb_auth, "create_user", _fake_create_user)

    uid = prov.FirebaseProvisioner().create_auth_user("new@example.com")

    assert uid == "fb-uid-abc"
    assert calls["email"] == "new@example.com"
    password = calls.get("password")
    assert isinstance(password, str) and len(password) >= 16  # non-empty, high-entropy


def test_firebase_create_auth_user_maps_duplicate_to_provision_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Existing already-exists handling must survive the password change.
    import firebase_admin.auth as fb_auth

    from app.modules.users import provisioner as prov
    from app.platform import auth as platform_auth

    monkeypatch.setattr(platform_auth, "_ensure_firebase", lambda: None)

    class EmailAlreadyExistsError(Exception):
        pass

    def _boom(**_kwargs: object) -> object:
        raise EmailAlreadyExistsError("dup")

    monkeypatch.setattr(fb_auth, "create_user", _boom)

    with pytest.raises(prov.ProvisionError):
        prov.FirebaseProvisioner().create_auth_user("dup@example.com")
