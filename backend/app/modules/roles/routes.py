"""Roles admin surface (mounted at `/api/v1`, a platform primitive).

  GET    /roles                     -> list roles (+ grants + user counts)
  GET    /roles/assignable-modules  -> the modules a role may be granted, for the editor
  POST   /roles                     -> create a role -> 201
  PATCH  /roles/{id}                -> replace a role's name/description/grants
  DELETE /roles/{id}                -> 204

Every endpoint requires the IAM platform permission (`role.manage`) — editing roles
defines everyone's access, so it's the highest-trust surface in the app.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.roles import service
from app.platform.auth import current_user
from app.platform.models import Role, User
from app.platform.module_registry import grantable_modules
from app.platform.rbac import can

router = APIRouter()


def _require_iam(user: User) -> None:
    if not can(user, "role.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "managing roles requires the IAM permission")


# ------------------------------------------------------------------- schemas

class RoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=400)
    # module_key -> "VIEW" | "OPERATE" | "MANAGE"
    module_levels: dict[str, str] = Field(default_factory=dict)
    platform: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    id: int
    name: str
    description: str
    is_system: bool
    module_levels: dict[str, str]
    platform: list[str]
    user_count: int


class ModuleOut(BaseModel):
    key: str
    title: str


def _role_out(role: Role, user_count: int) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        description=role.description,
        is_system=role.is_system,
        module_levels={mp.module_key: mp.level.value for mp in role.module_permissions},
        platform=[pp.permission_key.value for pp in role.platform_permissions],
        user_count=user_count,
    )


def _load(db: Session, role_id: int) -> Role:
    role = service.get_role(db, role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "role not found")
    return role


# --------------------------------------------------------------------- routes

@router.get("/roles", response_model=list[RoleOut])
def list_roles(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[RoleOut]:
    _require_iam(user)
    counts = service.user_counts(db)
    return [_role_out(r, counts.get(r.id, 0)) for r in service.list_roles(db)]


@router.get("/roles/assignable-modules", response_model=list[ModuleOut])
def assignable_modules(
    user: Annotated[User, Depends(current_user)],
) -> list[ModuleOut]:
    _require_iam(user)
    return [ModuleOut(key=m.key, title=m.title) for m in grantable_modules()]


@router.post("/roles", response_model=RoleOut, status_code=201)
def create_role(
    body: RoleBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RoleOut:
    _require_iam(user)
    try:
        role = service.create_role(
            db, name=body.name, description=body.description,
            module_levels=body.module_levels, platform=body.platform,
            actor_uid=user.firebase_uid,
        )
    except service.RoleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except service.RoleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    counts = service.user_counts(db)
    return _role_out(role, counts.get(role.id, 0))


@router.patch("/roles/{role_id}", response_model=RoleOut)
def update_role(
    role_id: int,
    body: RoleBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> RoleOut:
    _require_iam(user)
    role = _load(db, role_id)
    try:
        service.update_role(
            db, role, name=body.name, description=body.description,
            module_levels=body.module_levels, platform=body.platform,
            actor_uid=user.firebase_uid,
        )
    except service.RoleValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except service.RoleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    counts = service.user_counts(db)
    return _role_out(role, counts.get(role.id, 0))


@router.delete("/roles/{role_id}", status_code=204, response_class=Response)
def delete_role(
    role_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _require_iam(user)
    role = _load(db, role_id)
    try:
        service.delete_role(db, role, actor_uid=user.firebase_uid)
    except service.RoleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
