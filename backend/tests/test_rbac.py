"""RBAC v2 core — levels, the action catalog, platform perms, default-deny."""
from __future__ import annotations

from app.platform import rbac
from app.platform.models import Level, PlatformPerm
from tests.rbac_util import make_role, make_user

CHALLAN = rbac.CHALLAN
EXPENSE = rbac.EXPENSE


def test_module_level_and_at_least() -> None:
    user = make_user(role=make_role(module_levels={CHALLAN: Level.OPERATE}))
    assert rbac.module_level(user, CHALLAN) == Level.OPERATE
    assert rbac.can_access_module(user, CHALLAN)          # >= View
    assert rbac.has_at_least(user, CHALLAN, Level.VIEW)
    assert rbac.has_at_least(user, CHALLAN, Level.OPERATE)
    assert not rbac.has_at_least(user, CHALLAN, Level.MANAGE)  # Operate < Manage
    assert not rbac.can_access_module(user, EXPENSE)      # no grant for another module


def test_administrator_gets_everything() -> None:
    admin = make_user(role=make_role("Administrator", is_system=True))
    assert rbac.is_administrator(admin)
    assert rbac.module_level(admin, EXPENSE) == Level.MANAGE   # MANAGE everywhere
    assert rbac.has_platform(admin, PlatformPerm.IAM)
    assert rbac.can(admin, "challan.void")
    assert rbac.can(admin, "role.manage")


def test_action_catalog_maps_to_levels() -> None:
    operator = make_user(role=make_role(module_levels={CHALLAN: Level.OPERATE}))
    manager = make_user(role=make_role(module_levels={CHALLAN: Level.MANAGE}))
    # generate needs OPERATE; void/master-data need MANAGE.
    assert rbac.can(operator, "challan.generate")
    assert not rbac.can(operator, "challan.void")
    assert not rbac.can(operator, "masterdata.edit")
    assert rbac.can(manager, "challan.generate")
    assert rbac.can(manager, "challan.void")
    assert rbac.can(manager, "masterdata.edit")


def test_platform_permissions() -> None:
    iam = make_user(role=make_role(platform=[PlatformPerm.IAM]))
    assert rbac.can(iam, "user.manage")
    assert rbac.can(iam, "role.manage")
    assert not rbac.can(iam, "settings.edit")          # different platform perm
    settings = make_user(role=make_role(platform=[PlatformPerm.SETTINGS]))
    assert rbac.can(settings, "settings.edit")
    assert not rbac.can(settings, "user.manage")


def test_default_deny() -> None:
    manager = make_user(role=make_role(module_levels={CHALLAN: Level.MANAGE}))
    assert not rbac.can(manager, "no.such.action")     # unknown action -> denied
    roleless = make_user(role=None)
    assert not rbac.can_access_module(roleless, CHALLAN)
    assert rbac.module_level(roleless, CHALLAN) is None
    inactive = make_user(role=make_role("Administrator", is_system=True), active=False)
    assert not rbac.is_administrator(inactive)          # inactive is never admin
    assert not rbac.can(inactive, "challan.void")


def test_has_any_module() -> None:
    viewer = make_user(role=make_role(module_levels={CHALLAN: Level.VIEW}))
    assert rbac.has_any_module(viewer, Level.VIEW)
    assert not rbac.has_any_module(viewer, Level.OPERATE)  # view-only can't write anywhere
    operator = make_user(role=make_role(module_levels={CHALLAN: Level.OPERATE}))
    assert rbac.has_any_module(operator, Level.OPERATE)
