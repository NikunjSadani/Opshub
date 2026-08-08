"""Platform tables (unprefixed). Module tables live in each module, prefixed.

user · role · user_module_access · audit_log · setting
"""
import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    MIS = "MIS"
    OPERATIONS = "OPERATIONS"
    FINANCE = "FINANCE"


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.OPERATIONS)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    module_access: Mapped[list["UserModuleAccess"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserModuleAccess(Base):
    """EXPLICIT per-user module grants (not role-derived). One row = one granted module."""

    __tablename__ = "user_module_access"
    __table_args__ = (UniqueConstraint("user_id", "module_key", name="uq_user_module"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    module_key: Mapped[str] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="module_access")


class AuditLog(Base):
    """Append-only, hash-chained. The app DB role must NOT be granted UPDATE/DELETE here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    actor_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity: Mapped[str | None] = mapped_column(String(120))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    # UNIQUE forces the chain to stay linear: a concurrent second append with the
    # same prev_hash fails the constraint and retries against the new head.
    prev_hash: Mapped[str] = mapped_column(String(64), default="", unique=True)
    row_hash: Mapped[str] = mapped_column(String(64), index=True)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
