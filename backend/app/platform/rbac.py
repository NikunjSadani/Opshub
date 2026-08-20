"""Authorization (RBAC v2, inc 26) — a user holds one ROLE; the role grants
per-module LEVELS (View < Operate < Manage) and a few PLATFORM permissions.

Public surface (call-sites depend on these staying stable):
  can_access_module(user, key)        -> may open the module at all (>= View)
  module_level(user, key)             -> the user's Level for a module, or None
  has_at_least(user, key, level)      -> module level >= `level`
  require_module(user, key)           -> raise 403 unless >= View
  require_level(user, key, level)     -> raise 403 unless >= `level`
  can(user, action)                   -> discrete action gate via ACTION_CATALOG
  is_administrator(user)              -> holds the protected Administrator role
  has_platform(user, perm)            -> holds a platform permission

DEFAULT-DENY throughout: no active role, no matching grant, or an unknown action
all resolve to "denied". `resource` is reserved for a future per-entity scoping
axis (see docs/plans/RBAC-ROLES-DESIGN.md §7) and is unused today.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from app.platform.models import LEVEL_RANK, Level, PlatformPerm, User

# Registered module keys (mirror the ModuleSpec keys in app/main.py). The challan
# module owns its Master Data + Numbering sub-surfaces, so they share its key.
CHALLAN = "document_automation"
PROJECTS = "projects"
EXPENSE = "expense_invoice"


@dataclass(frozen=True)
class _ModuleReq:
    key: str
    level: Level


@dataclass(frozen=True)
class _PlatformReq:
    perm: PlatformPerm


# The one source of truth for what each discrete action requires. A module rule maps
# to (module, min level); a platform rule to a platform permission.
ACTION_CATALOG: dict[str, _ModuleReq | _PlatformReq] = {
    # --- Delivery Challan (document_automation) ---
    "challan.generate": _ModuleReq(CHALLAN, Level.OPERATE),
    "challan.void": _ModuleReq(CHALLAN, Level.MANAGE),
    "challan.recover": _ModuleReq(CHALLAN, Level.MANAGE),
    "validation.override_bulk": _ModuleReq(CHALLAN, Level.MANAGE),
    "masterdata.edit": _ModuleReq(CHALLAN, Level.MANAGE),
    "series.seed": _ModuleReq(CHALLAN, Level.MANAGE),
    "numbering.override": _ModuleReq(CHALLAN, Level.MANAGE),
    # --- Projects ---
    "project.create": _ModuleReq(PROJECTS, Level.OPERATE),
    "project.manage": _ModuleReq(PROJECTS, Level.MANAGE),
    # --- Expense / Invoice ---
    "expense.operate": _ModuleReq(EXPENSE, Level.OPERATE),
    "expense.delete": _ModuleReq(EXPENSE, Level.MANAGE),
    # --- Platform (cross-cutting) ---
    "user.manage": _PlatformReq(PlatformPerm.IAM),
    "role.manage": _PlatformReq(PlatformPerm.IAM),
    "settings.edit": _PlatformReq(PlatformPerm.SETTINGS),
}


def is_administrator(user: User) -> bool:
    """True if `user` holds the protected built-in Administrator role (all access)."""
    return user.active and user.role is not None and user.role.is_system


def module_level(user: User, module_key: str) -> Level | None:
    """The user's access level for `module_key`, or None if they have no access.

    Administrator ⇒ MANAGE everywhere; otherwise the role's explicit grant (if any).
    """
    if not user.active or user.role is None:
        return None
    if user.role.is_system:
        return Level.MANAGE
    for mp in user.role.module_permissions:
        if mp.module_key == module_key:
            return mp.level
    return None


def can_access_module(user: User, module_key: str) -> bool:
    """True if the user may open `module_key` at all (>= View)."""
    return module_level(user, module_key) is not None


def has_at_least(user: User, module_key: str, level: Level) -> bool:
    """True if the user's level for `module_key` is `level` or higher."""
    current = module_level(user, module_key)
    return current is not None and LEVEL_RANK[current] >= LEVEL_RANK[level]


def has_any_module(user: User, min_level: Level = Level.VIEW) -> bool:
    """True if the user has ANY module grant at `min_level` or higher (Administrator: yes)."""
    if not user.active or user.role is None:
        return False
    if user.role.is_system:
        return True
    return any(LEVEL_RANK[mp.level] >= LEVEL_RANK[min_level] for mp in user.role.module_permissions)


def has_platform(user: User, perm: PlatformPerm) -> bool:
    """True if the user's role carries platform permission `perm` (Administrator: all)."""
    if not user.active or user.role is None:
        return False
    if user.role.is_system:
        return True
    return any(pp.permission_key == perm for pp in user.role.platform_permissions)


def can(user: User, action: str, resource: Any | None = None) -> bool:
    """True if `user` may perform `action`. DEFAULT-DENY: an action absent from
    ACTION_CATALOG returns False, so a typo can never silently authorize anyone.
    `resource` reserved for future per-entity scoping (unused)."""
    if not user.active:
        return False
    req = ACTION_CATALOG.get(action)
    if req is None:
        return False
    if isinstance(req, _ModuleReq):
        return has_at_least(user, req.key, req.level)
    return has_platform(user, req.perm)


def require_module(user: User, module_key: str) -> None:
    """Raise 403 unless the user can open `module_key` (>= View)."""
    if not can_access_module(user, module_key):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "module access required")


def require_level(user: User, module_key: str, level: Level) -> None:
    """Raise 403 unless the user's level for `module_key` is `level` or higher."""
    if not has_at_least(user, module_key, level):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{level.value.lower()} access to this module is required",
        )
