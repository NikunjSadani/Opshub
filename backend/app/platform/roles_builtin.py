"""Built-in roles — the protected Administrator plus a few convenience presets.

`ensure_builtin_roles` is idempotent and the single place these are defined, so seed,
tests, the E2E bootstrap, and a future prod bootstrap all get the same roles. The
Administrator role (`is_system=True`) needs NO explicit module rows — `rbac.module_level`
short-circuits a system role to MANAGE everywhere and `has_platform` to all permissions.
The presets are ordinary roles an admin may rename, retune, clone, or delete.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.platform.models import (
    Level,
    Role,
    RoleModulePermission,
)
from app.platform.rbac import (
    ACTION_CENTER,
    BILLING,
    CHALLAN,
    EXPENSE,
    FINANCE,
    LOGISTICS,
    PROJECTS,
    SALES_ORDERS,
)

ADMINISTRATOR = "Administrator"

# Preset (non-system) roles: name -> (description, {module_key: level}).
_PRESETS: dict[str, tuple[str, dict[str, Level]]] = {
    "Challan Operator": (
        "Create and issue delivery challans.",
        {CHALLAN: Level.OPERATE},
    ),
    "Challan Manager": (
        "Full delivery-challan control, incl. void, master data, and numbering.",
        {CHALLAN: Level.MANAGE},
    ),
    "Finance": (
        "Work the Expense/Invoice module.",
        {EXPENSE: Level.OPERATE},
    ),
    "Viewer": (
        "Read-only access to every module.",
        {CHALLAN: Level.VIEW, PROJECTS: Level.VIEW, EXPENSE: Level.VIEW,
         SALES_ORDERS: Level.VIEW, BILLING: Level.VIEW, FINANCE: Level.VIEW,
         LOGISTICS: Level.VIEW, ACTION_CENTER: Level.VIEW},
    ),
}


def ensure_builtin_roles(db: Session) -> dict[str, Role]:
    """Idempotently create the Administrator role + presets. Returns name -> Role.

    Never mutates a role that already exists (an admin may have retuned a preset), so
    it's safe to call on every boot/seed. Caller commits.
    """
    existing = {r.name: r for r in db.execute(select(Role)).scalars()}

    if ADMINISTRATOR not in existing:
        admin = Role(
            name=ADMINISTRATOR,
            description="Full access to every module and all platform permissions.",
            is_system=True,
            created_by="system",
        )
        db.add(admin)
        db.flush()
        existing[ADMINISTRATOR] = admin

    for name, (description, modules) in _PRESETS.items():
        if name in existing:
            continue
        role = Role(name=name, description=description, is_system=False, created_by="system")
        role.module_permissions = [
            RoleModulePermission(module_key=key, level=level) for key, level in modules.items()
        ]
        db.add(role)
        db.flush()
        existing[name] = role

    return existing


def administrator_role(db: Session) -> Role:
    """Fetch the protected Administrator role (must exist — call ensure_builtin_roles first)."""
    role = db.execute(select(Role).where(Role.is_system.is_(True))).scalar_one_or_none()
    if role is None:  # pragma: no cover - defensive; seed/migration always creates it
        raise RuntimeError("Administrator role missing — run ensure_builtin_roles first")
    return role
