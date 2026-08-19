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
        "expense.delete",       # delete a CONFIRMED expense invoice (unconfirmed = module-gated)
        "challan.void",
        "challan.recover",      # reset a batch wedged in GENERATING (dead worker) → retryable
        "numbering.override",
        "validation.override_bulk",
        "masterdata.edit",
        "settings.edit",
        "series.seed",
        "project.manage",       # register clients + change project status
    }
)

# Non-admin actions any ACTIVE user may perform (empty for now — module access is the
# real gate for module use). Keeping this explicit makes `can()` DEFAULT-DENY.
NON_ADMIN_ACTIONS: frozenset[str] = frozenset()


def can(user: User, action: str, resource: Any | None = None) -> bool:
    """True if `user` may perform `action`. `resource` reserved for future scoping.

    DEFAULT-DENY: an unknown/unregistered action returns False, so a typo (e.g.
    `setting.edit` vs `settings.edit`) can never silently authorize everyone.
    """
    if not user.active:
        return False
    if action in ADMIN_ONLY:
        return user.role == Role.ADMIN
    return action in NON_ADMIN_ACTIONS


def can_access_module(user: User, module_key: str) -> bool:
    """True if `user` has an EXPLICIT grant for `module_key` (Admins see all modules)."""
    if not user.active:
        return False
    if user.role == Role.ADMIN:
        return True
    return any(m.module_key == module_key for m in user.module_access)
