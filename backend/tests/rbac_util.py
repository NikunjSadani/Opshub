"""Shared helpers for building role-based test users (RBAC v2).

Builds detached ORM objects — no DB needed — for tests that override `current_user`.
`rbac` reads plain attributes (`user.role.module_permissions`, `.is_system`, …), so an
in-memory Role with its child grants is enough to exercise every check.
"""
from __future__ import annotations

from app.platform.models import (
    Level,
    PlatformPerm,
    Role,
    RoleModulePermission,
    RolePlatformPermission,
    User,
)


def make_role(
    name: str = "Test Role",
    *,
    is_system: bool = False,
    module_levels: dict[str, Level] | None = None,
    platform: list[PlatformPerm] | None = None,
) -> Role:
    role = Role(name=name, description="", is_system=is_system)
    role.module_permissions = [
        RoleModulePermission(module_key=k, level=v) for k, v in (module_levels or {}).items()
    ]
    role.platform_permissions = [
        RolePlatformPermission(permission_key=p) for p in (platform or [])
    ]
    return role


def make_user(
    uid: str = "u",
    *,
    role: Role | None = None,
    active: bool = True,
    email: str | None = None,
) -> User:
    return User(
        firebase_uid=uid,
        email=email or f"{uid}@opshub.local",
        name=uid,
        role=role,
        active=active,
    )
