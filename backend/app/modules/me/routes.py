"""`GET /me` — the authenticated user's identity + EFFECTIVE permissions.

This is the single contract the frontend reads to drive nav + action gating, and the
one the real Firebase provider will call after login. It reports the user's own view
of their access — never anyone else's — so it needs no special permission beyond being
an active, authenticated user.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.platform.auth import current_user
from app.platform.models import PlatformPerm, User
from app.platform.module_registry import grantable_modules
from app.platform.rbac import has_platform, is_administrator, module_level

router = APIRouter()


class MeOut(BaseModel):
    id: int
    email: str
    name: str
    role_id: int | None
    role_name: str | None
    is_administrator: bool
    # module_key -> level ("VIEW"|"OPERATE"|"MANAGE"); only modules the user can access.
    module_levels: dict[str, str]
    # platform permission keys the user holds ("iam", "settings").
    platform: list[str]


@router.get("/me", response_model=MeOut)
def read_me(user: Annotated[User, Depends(current_user)]) -> MeOut:
    levels: dict[str, str] = {}
    for spec in grantable_modules():
        level = module_level(user, spec.key)
        if level is not None:
            levels[spec.key] = level.value
    platform = [p.value for p in PlatformPerm if has_platform(user, p)]
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role_id=user.role_id,
        role_name=user.role.name if user.role is not None else None,
        is_administrator=is_administrator(user),
        module_levels=levels,
        platform=platform,
    )
