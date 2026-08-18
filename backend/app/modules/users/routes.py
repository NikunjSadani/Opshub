"""User Management operator surface (mounted at `/api/v1`, a platform primitive).

  GET   /users                 -> list users (ADMIN `user.manage`)
  POST  /users                 -> create a user + module grants -> 201 {user, setup_link}
  PATCH /users/{id}            -> update name/role/active/module_keys (ADMIN)
  POST  /users/{id}/setup-link -> (re)issue a password-setup link (ADMIN)

Every endpoint is ADMIN-only (`user.manage`). A user is created with a backing
Firebase auth account via the provisioner seam; a password is NEVER accepted or
stored. Writes are audited by the service. The raw firebase_uid is never exposed
— only a derived `is_provisioned` flag.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.db import get_db
from app.modules.users import service
from app.modules.users.provisioner import ProvisionError, get_provisioner
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can

router = APIRouter()


def _require_admin(user: User) -> None:
    if not can(user, "user.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "user.manage requires ADMIN")


# ------------------------------------------------------------------- schemas

class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    role: str
    module_keys: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = None
    active: bool | None = None
    module_keys: list[str] | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str
    role: str
    active: bool
    module_keys: list[str]
    is_provisioned: bool
    created_at: datetime


class CreateResult(BaseModel):
    user: UserOut
    setup_link: str | None


class SetupLinkOut(BaseModel):
    setup_link: str | None


def _user_out(u: User) -> UserOut:
    """Serialize a user for the API. Never exposes the raw firebase_uid; instead
    derives `is_provisioned` (a real Firebase account, not the local stub)."""
    return UserOut(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role.value,
        active=u.active,
        module_keys=sorted(m.module_key for m in u.module_access),
        is_provisioned=not u.firebase_uid.startswith("local:"),
        created_at=u.created_at,
    )


def _load(db: Session, user_id: int) -> User:
    user = db.execute(
        select(User).options(selectinload(User.module_access)).where(User.id == user_id)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return user


# --------------------------------------------------------------------- routes

@router.get("/users", response_model=list[UserOut])
def list_users(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[UserOut]:
    _require_admin(user)
    return [_user_out(u) for u in service.list_users(db)]


@router.post("/users", response_model=CreateResult, status_code=201)
def create_user(
    body: UserCreate,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CreateResult:
    _require_admin(user)
    provisioner = get_provisioner(get_settings())
    try:
        new_user, link = service.create_user(
            db,
            email=body.email,
            name=body.name,
            role=body.role,
            module_keys=body.module_keys,
            actor_uid=user.firebase_uid,
            provisioner=provisioner,
        )
    except service.ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (service.UserError, ProvisionError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # service.create_user owns the commit (saga: compensating-delete on failure).
    db.refresh(new_user)
    return CreateResult(user=_user_out(new_user), setup_link=link)


@router.patch("/users/{user_id}", response_model=UserOut)
def patch_user(
    user_id: int,
    body: UserUpdate,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    _require_admin(user)
    target = _load(db, user_id)
    try:
        service.update_user(
            db,
            target,
            name=body.name,
            role=body.role,
            active=body.active,
            module_keys=body.module_keys,
            actor_uid=user.firebase_uid,
            acting_user=user,
        )
    except service.ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except service.UserError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.commit()
    db.refresh(target)
    return _user_out(target)


@router.post("/users/{user_id}/setup-link", response_model=SetupLinkOut)
def issue_setup_link(
    user_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SetupLinkOut:
    _require_admin(user)
    target = _load(db, user_id)
    link = service.setup_link(
        db, target, provisioner=get_provisioner(get_settings()), actor_uid=user.firebase_uid)
    db.commit()
    return SetupLinkOut(setup_link=link)
