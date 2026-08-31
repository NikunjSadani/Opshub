"""Platform tables (unprefixed). Module tables live in each module, prefixed.

user · role · role_module_permission · role_platform_permission · audit_log · setting

RBAC v2 (inc 26): access is a NAMED ROLE composed of per-module access LEVELS
(View < Operate < Manage) plus a small set of cross-cutting PLATFORM permissions.
A user holds exactly one role; the role is the single source of what they can do.
See `app/platform/rbac.py` for enforcement and `docs/plans/RBAC-ROLES-DESIGN.md`.
"""
import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Level(str, enum.Enum):
    """Per-module access level, strictly ordered VIEW < OPERATE < MANAGE (see LEVEL_RANK)."""

    VIEW = "VIEW"        # read-only: see + download
    OPERATE = "OPERATE"  # do the day-to-day work (upload/generate/confirm/create)
    MANAGE = "MANAGE"    # sensitive/destructive/module-admin (void/delete/edit-master)


# Numeric rank for "at least this level" comparisons. Higher includes lower.
LEVEL_RANK: dict["Level", int] = {Level.VIEW: 1, Level.OPERATE: 2, Level.MANAGE: 3}


class PlatformPerm(str, enum.Enum):
    """Cross-cutting capabilities not tied to a single module."""

    IAM = "iam"            # manage users AND roles (create/edit/deactivate/assign)
    SETTINGS = "settings"  # edit platform settings


class Role(Base):
    """A named, admin-composed bundle of per-module levels + platform permissions.

    `is_system` marks the protected built-in **Administrator** role (all modules at
    MANAGE + every platform permission) — it can't be edited or deleted, and at least
    one active user must always hold it.
    """

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(400), default="")
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_by: Mapped[str | None] = mapped_column(String(128), default=None)

    module_permissions: Mapped[list["RoleModulePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    platform_permissions: Mapped[list["RolePlatformPermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(back_populates="role")


class RoleModulePermission(Base):
    """One module grant on a role: `module_key` at `level`. Absent row = no access."""

    __tablename__ = "role_module_permission"
    __table_args__ = (UniqueConstraint("role_id", "module_key", name="uq_role_module"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"), index=True)
    module_key: Mapped[str] = mapped_column(String(64))
    level: Mapped[Level] = mapped_column(Enum(Level))

    role: Mapped[Role] = relationship(back_populates="module_permissions")


class RolePlatformPermission(Base):
    """One platform permission granted to a role."""

    __tablename__ = "role_platform_permission"
    __table_args__ = (UniqueConstraint("role_id", "permission_key", name="uq_role_platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"), index=True)
    permission_key: Mapped[PlatformPerm] = mapped_column(Enum(PlatformPerm))

    role: Mapped[Role] = relationship(back_populates="platform_permissions")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    # A user without a role has NO access anywhere (fail-closed). Normally always set.
    role_id: Mapped[int | None] = mapped_column(ForeignKey("role.id"), index=True, default=None)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Access-tracking (inc 38). `last_login_at` is stamped by POST /auth/login-event on an
    # explicit sign-in; `last_seen_at` is stamped (throttled) on GET /me so admins can see
    # who is actually active. Both nullable — no value until the user next logs in / is seen.
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    role: Mapped[Role | None] = relationship(back_populates="users")


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


class LoginEvent(Base):
    """One row per explicit sign-in (recorded by POST /auth/login-event).

    Feeds the admin-only "Audit & Access" login report. `ip` is the Cloudflare-forwarded
    client IP (best-effort); `user_agent` is truncated to fit. Append-only in practice
    (never edited), but NOT hash-chained — the tamper-evident chain is `audit_log`.
    """

    __tablename__ = "login_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(400))


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
