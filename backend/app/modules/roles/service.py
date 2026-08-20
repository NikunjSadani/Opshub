"""Roles service — create/edit/delete the named roles that grant access.

A role bundles per-module LEVELS (View/Operate/Manage) + platform permissions. The
built-in **Administrator** role (`is_system`) is protected: it can't be edited or
deleted, and a role can't be deleted while any user still holds it. Every mutation is
audited. Editing roles is the highest-trust action in the app (it defines everyone's
access), so it's gated on the IAM platform permission at the route layer.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.platform import audit
from app.platform.models import (
    Level,
    PlatformPerm,
    Role,
    RoleModulePermission,
    RolePlatformPermission,
    User,
)
from app.platform.module_registry import grantable_module_keys

_MAX_NAME = 80
_MAX_DESC = 400


class RoleError(Exception):
    """Invalid roles operation (route -> 409 by default)."""


class RoleValidationError(RoleError):
    """Bad input — name/level/module/permission (route -> 400)."""


class DuplicateRoleName(RoleError):
    """A role with that name already exists (route -> 409)."""


class RoleProtected(RoleError):
    """The Administrator (system) role can't be edited or deleted (route -> 409)."""


class RoleInUse(RoleError):
    """A role assigned to at least one user can't be deleted (route -> 409)."""


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise RoleValidationError("role name is required")
    if len(cleaned) > _MAX_NAME:
        raise RoleValidationError(f"role name must be at most {_MAX_NAME} characters")
    return cleaned


def _clean_description(description: str | None) -> str:
    desc = (description or "").strip()
    if len(desc) > _MAX_DESC:
        raise RoleValidationError(f"description must be at most {_MAX_DESC} characters")
    return desc


def _parse_module_levels(module_levels: dict[str, str]) -> dict[str, Level]:
    """Validate a {module_key: level_name} map against the grantable modules + Level enum."""
    grantable = grantable_module_keys()
    parsed: dict[str, Level] = {}
    for key, raw in module_levels.items():
        if key not in grantable:
            raise RoleValidationError(f"unknown module: {key!r}")
        try:
            parsed[key] = Level(raw)
        except ValueError as exc:
            allowed = [level.value for level in Level]
            raise RoleValidationError(f"level for {key!r} must be one of {allowed}") from exc
    return parsed


def _parse_platform(platform: list[str]) -> list[PlatformPerm]:
    parsed: list[PlatformPerm] = []
    seen: set[PlatformPerm] = set()
    for raw in platform:
        try:
            perm = PlatformPerm(raw)
        except ValueError as exc:
            allowed = [p.value for p in PlatformPerm]
            raise RoleValidationError(f"platform permission must be one of {allowed}") from exc
        if perm not in seen:
            seen.add(perm)
            parsed.append(perm)
    return parsed


def _apply_grants(role: Role, modules: dict[str, Level], platform: list[PlatformPerm]) -> None:
    """Replace a role's module + platform grants (cascade delete-orphan clears the old)."""
    role.module_permissions = [
        RoleModulePermission(module_key=key, level=level) for key, level in modules.items()
    ]
    role.platform_permissions = [
        RolePlatformPermission(permission_key=perm) for perm in platform
    ]


def list_roles(db: Session) -> list[Role]:
    """All roles, system first then by name, with grants eager-loaded."""
    return list(
        db.execute(
            select(Role)
            .options(
                selectinload(Role.module_permissions),
                selectinload(Role.platform_permissions),
            )
            .order_by(Role.is_system.desc(), Role.name)
        ).scalars()
    )


def user_counts(db: Session) -> dict[int, int]:
    """role_id -> number of users assigned it (0 for unused roles is simply absent)."""
    rows = db.execute(
        select(User.role_id, func.count(User.id)).where(User.role_id.is_not(None)).group_by(
            User.role_id
        )
    ).all()
    return {rid: n for rid, n in rows if rid is not None}


def get_role(db: Session, role_id: int) -> Role | None:
    return db.get(Role, role_id)


def create_role(
    db: Session,
    *,
    name: str,
    description: str | None,
    module_levels: dict[str, str],
    platform: list[str],
    actor_uid: str | None,
) -> Role:
    """Create a non-system role from a name + per-module levels + platform permissions."""
    name = _clean_name(name)
    description = _clean_description(description)
    modules = _parse_module_levels(module_levels)
    perms = _parse_platform(platform)

    if db.execute(select(Role).where(func.lower(Role.name) == name.lower())).scalar_one_or_none():
        raise DuplicateRoleName(f"a role named {name!r} already exists")

    role = Role(name=name, description=description, is_system=False, created_by=actor_uid)
    _apply_grants(role, modules, perms)
    db.add(role)
    try:
        db.flush()
    except IntegrityError as exc:  # unique-name race
        db.rollback()
        raise DuplicateRoleName(f"a role named {name!r} already exists") from exc
    audit.log(
        db, action="role.created", actor_uid=actor_uid, entity="role", entity_id=str(role.id),
        detail={"name": name, "modules": {k: v.value for k, v in modules.items()},
                "platform": [p.value for p in perms]},
    )
    db.commit()
    return role


def update_role(
    db: Session,
    role: Role,
    *,
    name: str,
    description: str | None,
    module_levels: dict[str, str],
    platform: list[str],
    actor_uid: str | None,
) -> Role:
    """Replace a role's name/description/grants. Refuses to touch the system role."""
    if role.is_system:
        raise RoleProtected("the Administrator role can't be edited")

    name = _clean_name(name)
    description = _clean_description(description)
    modules = _parse_module_levels(module_levels)
    perms = _parse_platform(platform)

    clash = db.execute(
        select(Role).where(func.lower(Role.name) == name.lower(), Role.id != role.id)
    ).scalar_one_or_none()
    if clash is not None:
        raise DuplicateRoleName(f"a role named {name!r} already exists")

    role.name = name
    role.description = description
    # Clear the old grants and FLUSH the deletes BEFORE inserting the new ones — otherwise a
    # replacement that reuses a (role_id, module_key) pair would insert-before-delete and trip
    # the unique constraint mid-flush.
    role.module_permissions.clear()
    role.platform_permissions.clear()
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent rename to a taken name
        db.rollback()
        raise DuplicateRoleName(f"a role named {name!r} already exists") from exc
    _apply_grants(role, modules, perms)
    db.flush()
    audit.log(
        db, action="role.updated", actor_uid=actor_uid, entity="role", entity_id=str(role.id),
        detail={"name": name, "modules": {k: v.value for k, v in modules.items()},
                "platform": [p.value for p in perms]},
    )
    db.commit()
    return role


def delete_role(db: Session, role: Role, *, actor_uid: str | None) -> None:
    """Delete a non-system role that no user holds."""
    if role.is_system:
        raise RoleProtected("the Administrator role can't be deleted")
    in_use = db.execute(
        select(func.count(User.id)).where(User.role_id == role.id)
    ).scalar_one()
    if in_use:
        raise RoleInUse(f"{in_use} user(s) still hold this role — reassign them first")
    role_id, role_name = role.id, role.name
    db.delete(role)
    audit.log(
        db, action="role.deleted", actor_uid=actor_uid, entity="role", entity_id=str(role_id),
        detail={"name": role_name},
    )
    db.commit()
