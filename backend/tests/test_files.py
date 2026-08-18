"""Files module smoke: upload -> download round-trip + storage path-safety.

Auth and the DB session are dependency-overridden so no Firebase/Postgres is
needed; blobs land in a per-test tmp dir via FILES_DIR.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
from app.platform.models import Role, User, UserModuleAccess
from app.platform.storage import LocalStorage


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", str(tmp_path / "_files"))

    engine = create_engine(
        "sqlite://",  # in-memory
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # share one connection so create_all is visible to requests
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

    def _override_user() -> User:
        # Holds a module grant so it clears the upload authz gate (upload requires
        # at least one grant); the round-trip tests exercise mechanics, not authz.
        return User(
            firebase_uid="fb-tester",
            email="tester@x.com",
            active=True,
            module_access=[UserModuleAccess(module_key="document_automation")],
        )

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/files")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[current_user] = _override_user

    yield TestClient(app)

    Base.metadata.drop_all(engine)
    engine.dispose()


def test_upload_then_download_round_trips(client: TestClient) -> None:
    payload = b"hello opshub \x00\x01 bytes"
    up = client.post(
        "/api/v1/files/upload",
        files={"file": ("report.txt", payload, "text/plain")},
    )
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["filename"] == "report.txt"
    assert body["size"] == len(payload)
    file_id = body["id"]

    down = client.get(f"/api/v1/files/{file_id}/download")
    assert down.status_code == 200
    assert down.content == payload
    assert down.headers["content-type"].startswith("text/plain")
    assert 'attachment; filename="report.txt"' in down.headers["content-disposition"]


def test_download_missing_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/files/999999/download").status_code == 404


def test_download_denied_for_other_user(client: TestClient) -> None:
    """IDOR guard: a different non-admin user can't pull someone else's file (404, no leak)."""
    up = client.post("/api/v1/files/upload", files={"file": ("secret.txt", b"top", "text/plain")})
    file_id = up.json()["id"]

    intruder = User(firebase_uid="intruder", email="x@x.com", role=Role.OPERATIONS, active=True)
    client.app.dependency_overrides[current_user] = lambda: intruder
    assert client.get(f"/api/v1/files/{file_id}/download").status_code == 404

    admin = User(firebase_uid="adm", email="a@x.com", role=Role.ADMIN, active=True)
    client.app.dependency_overrides[current_user] = lambda: admin
    assert client.get(f"/api/v1/files/{file_id}/download").status_code == 200


def test_upload_rejects_oversized_file(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """DoS guard: an upload past the cap is rejected with 413."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "16")
    big = client.post(
        "/api/v1/files/upload",
        files={"file": ("big.bin", b"x" * 64, "application/octet-stream")},
    )
    get_settings.cache_clear()
    assert big.status_code == 413


def test_upload_ignores_traversal_filename(client: TestClient, tmp_path: Path) -> None:
    # A malicious filename must not create anything outside the base dir.
    up = client.post(
        "/api/v1/files/upload",
        files={"file": ("../../../../evil.txt", b"pwn", "text/plain")},
    )
    assert up.status_code == 200, up.text
    # Nothing escaped: no stray evil.txt anywhere above the base dir.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    # And it still round-trips from inside the base dir.
    down = client.get(f"/api/v1/files/{up.json()['id']}/download")
    assert down.status_code == 200
    assert down.content == b"pwn"


def test_storage_rejects_traversal_keys(tmp_path: Path) -> None:
    store = LocalStorage(base_dir=str(tmp_path / "base"))
    for bad in ["../escape.txt", "/abs/path.txt", "C:/win.txt", "a/../../b.txt", ""]:
        with pytest.raises(ValueError):
            store.save(bad, b"x")
    # A file written outside the base must never be reachable via a traversal ref.
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"top-secret")
    with pytest.raises(ValueError):
        store.open("../secret.txt")
