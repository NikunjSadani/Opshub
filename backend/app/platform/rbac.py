"""Authorization — two axes.

  can(user, action, resource=None)  -> what ACTIONS a role may perform
  can_access_module(user, module)   -> which MODULES a user may open (EXPLICIT per-user grants)

`resource` is accepted now (shaped for future per-consignor/brand scoping) but
unused by default: all staff act on all entities. A full per-action permission
matrix is deliberately out of scope — the role governs actions.
"""
from __future__ import annotations

from typing import Any

from app.platform.models import Role, User

# Admin-only actions (hard-coded, not configurable).
ADMIN_ONLY: frozenset[str] = frozenset(
    {
        "user.manage",          # create/invite users, assign role + module access, enable/disable
        "challan.void",
        "numbering.override",
        "validation.override_bulk",
        "masterdata.edit",
        "settings.edit",
        "series.seed",
    }
)


def can(user: User, action: str, resource: Any | None = None) -> bool:
    """True if `user` may perform `action`. `resource` reserved for future scoping."""
    if not user.active:
        return False
    if action in ADMIN_ONLY:
        return user.role == Role.ADMIN
    # Non-admin-gated actions: any active role may perform (module access is the gate).
    return True


def can_access_module(user: User, module_key: str) -> bool:
    """True if `user` has an EXPLICIT grant for `module_key` (Admins see all modules)."""
    if not user.active:
        return False
    if user.role == Role.ADMIN:
        return True
    return any(m.module_key == module_key for m in user.module_access)
