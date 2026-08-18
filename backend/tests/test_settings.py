"""Settings module tests — admin-gated CRUD + audit, on an in-memory sqlite DB.

Overrides `get_db` (test session) and `current_user` (admin vs non-admin) so no
Firebase/Postgres is required.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.modules.settings.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, Setting, User

# A real, declared, PUBLIC setting (see settings/registry.py). Non-secret operational
# config the UI may read; the only key the challan engine actually consumes today.
EWAY = "eway_threshold"

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)

ADMIN = User(id=1, firebase_uid="admin-uid", email="admin@x.com", role=Role.ADMIN, active=True)
OPS = User(id=2, firebase_uid="ops-uid", email="ops@x.com", role=Role.OPERATIONS, active=True)
INACTIVE_ADMIN = User(
    id=3, firebase_uid="ex-admin", email="ex@x.com", role=Role.ADMIN, active=False)


def _override_get_db() -> Iterator[Session]:
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _fresh_schema() -> Iterator[None]:
    Base.metadata.create_all(_engine)
    yield
    Base.metadata.drop_all(_engine)


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_db] = _override_get_db
    return application


def _as(app: FastAPI, user: User) -> TestClient:
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_admin_put_declared_key_creates_then_reads_back(app: FastAPI) -> None:
    client = _as(app, ADMIN)
    r = client.put(f"/api/v1/settings/{EWAY}", json={"value": 5000000})
    assert r.status_code == 200
    assert r.json()["value"] == 5000000

    r2 = client.get(f"/api/v1/settings/{EWAY}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["key"] == EWAY and body["value"] == 5000000
    assert "updated_at" in body


def test_admin_put_updates_existing(app: FastAPI) -> None:
    client = _as(app, ADMIN)
    client.put(f"/api/v1/settings/{EWAY}", json={"value": 100000})
    r = client.put(f"/api/v1/settings/{EWAY}", json={"value": 200000})
    assert r.status_code == 200 and r.json()["value"] == 200000
    assert client.get(f"/api/v1/settings/{EWAY}").json()["value"] == 200000


def test_put_unknown_key_rejected_422(app: FastAPI) -> None:
    # The governance guarantee: a non-declared key (e.g. a smuggled secret) can't be
    # written to this store at all — even by an admin.
    r = _as(app, ADMIN).put("/api/v1/settings/smtp_password", json={"value": "hunter2"})
    assert r.status_code == 422
    with _TestSession() as db:
        assert db.get(Setting, "smtp_password") is None  # nothing persisted


def test_public_setting_readable_by_non_admin(app: FastAPI) -> None:
    _as(app, ADMIN).put(f"/api/v1/settings/{EWAY}", json={"value": 5000000})
    r = _as(app, OPS).get(f"/api/v1/settings/{EWAY}")
    assert r.status_code == 200 and r.json()["value"] == 5000000


def test_legacy_unknown_key_is_admin_only(app: FastAPI) -> None:
    # A row whose key isn't declared (a legacy/stray value) FAILS CLOSED: invisible to a
    # non-admin (single 404 + absent from the list), readable only by an admin.
    with _TestSession() as db:
        db.add(Setting(key="legacy_stray", value="x"))
        db.commit()
    ops = _as(app, OPS)
    assert ops.get("/api/v1/settings/legacy_stray").status_code == 404
    assert "legacy_stray" not in {row["key"] for row in ops.get("/api/v1/settings").json()}
    admin = _as(app, ADMIN)
    assert admin.get("/api/v1/settings/legacy_stray").status_code == 200
    assert "legacy_stray" in {row["key"] for row in admin.get("/api/v1/settings").json()}


def test_inactive_admin_is_not_treated_as_admin(app: FastAPI) -> None:
    # Defense-in-depth: an inactive account is never admin, so it can't read a hidden
    # key even if a stale token reached the route (the upstream auth gate 403s first).
    with _TestSession() as db:
        db.add(Setting(key="legacy_stray", value="x"))
        db.commit()
    assert _as(app, INACTIVE_ADMIN).get("/api/v1/settings/legacy_stray").status_code == 404


def test_get_missing_is_404(app: FastAPI) -> None:
    # A declared-but-unset key is a plain not-found for an admin.
    assert _as(app, ADMIN).get(f"/api/v1/settings/{EWAY}").status_code == 404


def test_non_admin_put_forbidden(app: FastAPI) -> None:
    r = _as(app, OPS).put(f"/api/v1/settings/{EWAY}", json={"value": 5})
    assert r.status_code == 403
    with _TestSession() as db:
        assert db.get(Setting, EWAY) is None  # nothing written


def test_admin_put_writes_audit_row(app: FastAPI) -> None:
    _as(app, ADMIN).put(f"/api/v1/settings/{EWAY}", json={"value": 5000000})
    with _TestSession() as db:
        rows = list(
            db.execute(select(AuditLog).where(AuditLog.action == "settings.edit")).scalars()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.entity == "setting"
    assert row.entity_id == EWAY
    assert row.actor_uid == "admin-uid"
    assert row.detail == {"value": 5000000}
