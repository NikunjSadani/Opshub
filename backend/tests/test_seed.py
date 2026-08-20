"""Seed: idempotent, creates the expected rows, and FAILS CLOSED on prod."""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import Base
from app.platform.models import Level, Setting, User
from app.seed import assert_seedable, seed

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
)
TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def db() -> Iterator[Session]:
    Base.metadata.create_all(engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def test_seed_creates_role_based_users_and_setting(db: Session) -> None:
    seed(db)
    admin = db.execute(select(User).where(User.firebase_uid == "dev-admin")).scalar_one()
    assert admin.role is not None and admin.role.is_system  # the protected Administrator role
    operator = db.execute(select(User).where(User.firebase_uid == "dev-operator")).scalar_one()
    assert operator.role is not None and operator.role.name == "Challan Operator"
    assert {mp.module_key: mp.level for mp in operator.role.module_permissions} == {
        "document_automation": Level.OPERATE
    }
    assert db.get(Setting, "eway_threshold") is not None


def test_seed_is_idempotent(db: Session) -> None:
    seed(db)
    seed(db)  # second run must not duplicate
    count = db.execute(select(func.count()).select_from(User)).scalar_one()
    assert count == 4  # dev-admin, dev-manager, dev-operator, dev-viewer


def test_seed_refuses_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="prod"):
            assert_seedable()
    finally:
        get_settings.cache_clear()
