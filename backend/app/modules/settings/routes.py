"""Settings CRUD routes.

  GET  /settings        -> list all Setting rows (any authenticated user)
  GET  /settings/{key}  -> one Setting (404 if missing)
  PUT  /settings/{key}  -> ADMIN-only upsert (rbac action "settings.edit"), audited

Mounts at `/api/v1` in `app.main`, giving `/api/v1/settings...`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.platform import audit
from app.platform.auth import current_user
from app.platform.models import Setting, User
from app.platform.rbac import can

router = APIRouter()


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
    _user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[Setting]:
    """All settings (any authenticated user may read)."""
    return list(db.execute(select(Setting).order_by(Setting.key)).scalars())


@router.get("/settings/{key}", response_model=SettingOut)
def get_setting(
    key: str,
    _user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Setting:
    """One setting by key (404 if it does not exist)."""
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
    """Create or update a setting. ADMIN-only; every write is audited."""
    if not can(user, "settings.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "settings.edit requires ADMIN")

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
