"""Upload authorization (LOW-2): the /files/upload endpoint must require the
caller to hold at least one module grant, and must reject a client-supplied
module_key the caller cannot access.

Auth + DB are dependency-overridden (no Firebase / Postgres); blobs land in a
per-test tmp dir via FILES_DIR.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.modules.files.models import StoredFile  # noqa: F401 - registers the table
from app.modules.files.routes import router
from app.platform.auth import current_user
from app.platform.models import Level, User
from tests.rbac_util import make_role, make_user

NO_GRANT = make_user("nogrant", role=make_role())
DOCAUTO = make_user("doc", role=make_role(module_levels={"document_automation": Level.OPERATE}))
ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", str(tmp_path / "_files"))

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/files")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[current_user] = lambda: DOCAUTO  # default; per-test override
    yield TestClient(app)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _upload(client: TestClient, module_key: str | None = None) -> httpx.Response:
    data = {"module_key": module_key} if module_key is not None else None
    return client.post(
        "/api/v1/files/upload",
        files={"file": ("report.txt", b"payload", "text/plain")},
        data=data,
    )


def test_no_grant_user_cannot_upload(client: TestClient) -> None:
    _as(client, NO_GRANT)
    r = _upload(client)
    assert r.status_code == 403, r.text
    # ...even when they don't supply a module_key at all.
    r2 = _upload(client, module_key="document_automation")
    assert r2.status_code == 403


def test_granted_user_uploads_to_their_module(client: TestClient) -> None:
    _as(client, DOCAUTO)
    r = _upload(client, module_key="document_automation")
    assert r.status_code == 200, r.text
    assert r.json()["filename"] == "report.txt"
    # A grant-holder may also upload without a module_key (personal blob).
    r2 = _upload(client)
    assert r2.status_code == 200, r2.text


def test_granted_user_rejected_for_other_module(client: TestClient) -> None:
    _as(client, DOCAUTO)
    r = _upload(client, module_key="projects")  # a module this user does NOT hold
    assert r.status_code == 403, r.text


def test_admin_can_upload_any_module(client: TestClient) -> None:
    _as(client, ADMIN)
    r = _upload(client, module_key="projects")
    assert r.status_code == 200, r.text
