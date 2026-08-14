"""Projects operator surface (mounted at `/api/v1`).

  POST /projects/clients        -> register a client (ADMIN `project.manage`)
  GET  /projects/clients        -> list clients (module-gated read)
  POST /projects                -> create a project (module-gated)
  GET  /projects                -> list projects (module-gated; filter/paginate)
  GET  /projects/{id}           -> one project (module-gated)
  PATCH /projects/{id}          -> change status (ADMIN `project.manage`)

Reads need the `projects` module grant; client registration + status changes are
Admin-only. Every write is audited by the service. Client codes are 3 uppercase
letters and unique; project ids are `<CLIENT_CODE>-<seq>` minted per client.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.modules.projects import service
from app.modules.projects.models import Project, ProjectClient
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can, can_access_module

router = APIRouter()

MODULE_KEY = "projects"


def _require_module(user: User) -> None:
    if not can_access_module(user, MODULE_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to projects")


def _require_admin(user: User) -> None:
    if not can(user, "project.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project.manage requires ADMIN")


# ------------------------------------------------------------------- schemas

class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Case-insensitive at the edge so 'bri' normalizes (and dedupes) to 'BRI' in
    # the service; a wrong LENGTH/charset (e.g. "BR", "BRIX", "12A") 422s here.
    code: str = Field(pattern=r"^[A-Za-z]{3}$")


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str
    active: bool


class ProjectIn(BaseModel):
    client_id: int
    name: str = Field(min_length=1, max_length=200)
    start_date: date | None = None
    description: str | None = Field(default=None, max_length=1000)


class StatusIn(BaseModel):
    status: Literal["ACTIVE", "ON_HOLD", "CLOSED"]


class ProjectOut(BaseModel):
    id: int
    code: str
    client_id: int
    client_code: str
    client_name: str
    name: str
    start_date: date | None
    status: str
    description: str | None
    created_at: datetime


def _project_out(p: Project) -> ProjectOut:
    """Serialize a project, resolving client_code/client_name via the relationship."""
    return ProjectOut(
        id=p.id,
        code=p.code,
        client_id=p.client_id,
        client_code=p.client.code,
        client_name=p.client.name,
        name=p.name,
        start_date=p.start_date,
        status=p.status,
        description=p.description,
        created_at=p.created_at,
    )


# ------------------------------------------------------------------- clients

@router.post("/projects/clients", response_model=ClientOut, status_code=201)
def create_client(
    body: ClientIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectClient:
    _require_admin(user)
    try:
        client = service.create_client(
            db, name=body.name, code=body.code, actor_uid=user.firebase_uid
        )
    except service.DuplicateClientCode as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.ProjectError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(client)
    return client


@router.get("/projects/clients", response_model=list[ClientOut])
def list_clients(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    active: Annotated[bool | None, Query()] = None,
) -> list[ProjectClient]:
    _require_module(user)
    stmt = select(ProjectClient).order_by(ProjectClient.name)
    if active is not None:
        stmt = stmt.where(ProjectClient.active == active)
    return list(db.execute(stmt).scalars())


# ------------------------------------------------------------------ projects

@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectOut:
    _require_module(user)
    try:
        project = service.create_project(
            db,
            client_id=body.client_id,
            name=body.name,
            start_date=body.start_date,
            description=body.description,
            actor_uid=user.firebase_uid,
        )
    except service.ProjectClientNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except service.ProjectIdContention as exc:  # transient — caller should retry
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.ProjectError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(project)
    return _project_out(project)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[
        Literal["ACTIVE", "ON_HOLD", "CLOSED"] | None, Query(alias="status")
    ] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProjectOut]:
    _require_module(user)
    stmt = (
        select(Project)
        .options(joinedload(Project.client))
        .order_by(Project.id.desc())
    )
    if client_id is not None:
        stmt = stmt.where(Project.client_id == client_id)
    if status_filter is not None:
        stmt = stmt.where(Project.status == status_filter)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Project.name.ilike(like) | Project.code.ilike(like))
    stmt = stmt.limit(limit).offset(offset)
    return [_project_out(p) for p in db.execute(stmt).scalars()]


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectOut:
    _require_module(user)
    project = db.execute(
        select(Project).options(joinedload(Project.client)).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return _project_out(project)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
def patch_project(
    project_id: int,
    body: StatusIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectOut:
    _require_admin(user)
    project = db.execute(
        select(Project).options(joinedload(Project.client)).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    try:
        service.set_status(db, project=project, status=body.status, actor_uid=user.firebase_uid)
    except service.ProjectError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(project)
    return _project_out(project)
