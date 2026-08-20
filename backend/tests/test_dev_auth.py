"""The LOCAL-ONLY dev-auth shim must be inert unless env=='local' AND dev_auth.

Proves the double guard: in staging/prod, or with dev_auth off, current_user
falls through to real Firebase verification (never the bypass).
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base
from app.platform import auth
from app.platform.models import User


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine, tables=[User.__table__])
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    session.add(User(firebase_uid="dev-admin", email="a@x.com", name="Dev Admin", active=True))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _boom(_token: str) -> dict[str, object]:
    raise RuntimeError("verify called")  # sentinel: proves the bypass did NOT fire


def test_dev_auth_bypasses_firebase_in_local(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("DEV_AUTH", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "_verify_token", _boom)  # must NOT be called
    user = auth.current_user(db, "Bearer mock-token.not-real")
    assert user.firebase_uid == "dev-admin"
    get_settings.cache_clear()


def test_dev_auth_ignored_outside_local(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("DEV_AUTH", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "_verify_token", _boom)
    with pytest.raises(RuntimeError, match="verify called"):  # fell through to Firebase
        auth.current_user(db, "Bearer x")
    get_settings.cache_clear()


def test_dev_auth_off_by_default(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "local")  # DEV_AUTH unset -> False
    get_settings.cache_clear()
    monkeypatch.setattr(auth, "_verify_token", _boom)
    with pytest.raises(RuntimeError, match="verify called"):
        auth.current_user(db, "Bearer x")
    get_settings.cache_clear()


def test_dev_auth_still_needs_a_bearer(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("DEV_AUTH", "true")
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as exc:
        auth.current_user(db, None)
    assert exc.value.status_code == 401
    get_settings.cache_clear()
