"""Settings CRUD routes — governed by the settings registry (see `registry.py`).

  GET  /settings        -> settings the caller may read (visibility-filtered)
  GET  /settings/{key}  -> one Setting (404 if missing OR not visible to the caller)
  PUT  /settings/{key}  -> ADMIN-only upsert of a DECLARED key (audited)

Reads are gated by each key's declared visibility (unknown/legacy keys fail closed to
admin-only); writes are restricted to registry keys so a secret can never enter this
store. Mounts at `/api/v1` in `app.main`, giving `/api/v1/settings...`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.settings import registry
from app.platform import audit
from app.platform.auth import current_user
from app.platform.models import PlatformPerm, Setting, User
from app.platform.rbac import can, has_platform

router = APIRouter()


def _is_admin(user: User) -> bool:
    # Admin-visibility settings are readable by whoever may edit settings (the SETTINGS
    # platform permission — always true for the Administrator role). Inactive accounts
    # never qualify (has_platform checks `active`), defense-in-depth vs the auth gate.
    return has_platform(user, PlatformPerm.SETTINGS)


class SettingOut(BaseModel):
    """Typed view of a Setting row."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    value: Any
    updated_at: datetime


class SettingUpdate(BaseModel):
    """PUT body: the new JSON value for the setting."""

    value: Any


@router.get("/settings", response_model=list[SettingOut])
def list_settings(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[Setting]:
    """Settings the caller may read — visibility-filtered (unknown keys admin-only)."""
    is_admin = _is_admin(user)
    rows = db.execute(select(Setting).order_by(Setting.key)).scalars()
    return [s for s in rows if registry.can_read(s.key, is_admin=is_admin)]


@router.get("/settings/{key}", response_model=SettingOut)
def get_setting(
    key: str,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Setting:
    """One setting by key. 404 if it does not exist OR the caller may not read it
    (a uniform 404 so a hidden key's existence isn't leaked to a non-admin)."""
    if not registry.can_read(key, is_admin=_is_admin(user)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "setting not found")
    setting = db.get(Setting, key)
    if setting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "setting not found")
    return setting


@router.put("/settings/{key}", response_model=SettingOut)
def upsert_setting(
    key: str,
    body: SettingUpdate,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Setting:
    """Create or update a DECLARED setting. ADMIN-only; every write is audited.

    A key not in the registry is rejected (422) — settings must be declared, so a
    secret can never be smuggled into this world-adjacent store; it belongs in Secret
    Manager instead.
    """
    if not can(user, "settings.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "settings.edit requires ADMIN")
    if not registry.is_writable(key):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown setting '{key}' — settings must be declared in the registry "
            "(secrets belong in Secret Manager, not here)")

    setting = db.get(Setting, key)
    if setting is None:
        setting = Setting(key=key)
        db.add(setting)
    setting.value = body.value
    setting.updated_by = user.firebase_uid
    setting.updated_at = datetime.now(UTC)

    audit.log(
        db,
        action="settings.edit",
        actor_uid=user.firebase_uid,
        entity="setting",
        entity_id=key,
        detail={"value": body.value},
    )
    db.commit()
    db.refresh(setting)
    return setting
