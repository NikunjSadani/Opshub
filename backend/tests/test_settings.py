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
from app.platform.models import AuditLog, Role, User

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)

ADMIN = User(id=1, firebase_uid="admin-uid", email="admin@x.com", role=Role.ADMIN, active=True)
OPS = User(id=2, firebase_uid="ops-uid", email="ops@x.com", role=Role.OPERATIONS, active=True)


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


def test_admin_put_creates_then_reads_back(app: FastAPI) -> None:
    client = _as(app, ADMIN)
    r = client.put("/api/v1/settings/numbering.prefix", json={"value": {"dc": "DC-2026"}})
    assert r.status_code == 200
    assert r.json()["value"] == {"dc": "DC-2026"}

    r2 = client.get("/api/v1/settings/numbering.prefix")
    assert r2.status_code == 200
    body = r2.json()
    assert body["key"] == "numbering.prefix"
    assert body["value"] == {"dc": "DC-2026"}
    assert "updated_at" in body


def test_admin_put_updates_existing(app: FastAPI) -> None:
    client = _as(app, ADMIN)
    client.put("/api/v1/settings/gst.rate", json={"value": 18})
    r = client.put("/api/v1/settings/gst.rate", json={"value": 12})
    assert r.status_code == 200
    assert r.json()["value"] == 12
    assert client.get("/api/v1/settings/gst.rate").json()["value"] == 12


def test_list_settings(app: FastAPI) -> None:
    client = _as(app, ADMIN)
    client.put("/api/v1/settings/a", json={"value": 1})
    client.put("/api/v1/settings/b", json={"value": "two"})
    r = _as(app, OPS).get("/api/v1/settings")
    assert r.status_code == 200
    keys = {row["key"] for row in r.json()}
    assert {"a", "b"} <= keys


def test_get_missing_is_404(app: FastAPI) -> None:
    r = _as(app, OPS).get("/api/v1/settings/nope")
    assert r.status_code == 404


def test_non_admin_put_forbidden(app: FastAPI) -> None:
    r = _as(app, OPS).put("/api/v1/settings/gst.rate", json={"value": 5})
    assert r.status_code == 403
    # nothing was written
    assert _as(app, ADMIN).get("/api/v1/settings/gst.rate").status_code == 404


def test_admin_put_writes_audit_row(app: FastAPI) -> None:
    _as(app, ADMIN).put("/api/v1/settings/theme", json={"value": "dark"})
    with _TestSession() as db:
        rows = list(
            db.execute(select(AuditLog).where(AuditLog.action == "settings.edit")).scalars()
        )
    assert len(rows) == 1
    row = rows[0]
    assert row.entity == "setting"
    assert row.entity_id == "theme"
    assert row.actor_uid == "admin-uid"
    assert row.detail == {"value": "dark"}
