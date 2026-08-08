"""Database engine + session (SQLAlchemy).

Postgres in staging/prod (Cloud SQL); sqlite locally for the bootstrap skeleton.
One schema, table-prefixed models (platform tables unprefixed, modules prefixed).
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

# sqlite needs check_same_thread=False for the dev bootstrap; Postgres ignores it.
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}

engine = create_engine(_settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency: a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
