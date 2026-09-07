"""Projects operator surface (mounted at `/api/v1`).

  POST /projects/clients        -> register a client (ADMIN `project.manage`)
  GET  /projects/clients        -> list clients (module-gated read)
  POST /projects                -> create a project (module-gated)
  GET  /projects                -> list projects (module-gated; filter/paginate)
  GET  /projects/{id}           -> one project (module-gated)
  PATCH /projects/{id}          -> edit details (name/date/desc) and/or status
                                   (ADMIN `project.manage`)

Reads need the `projects` module grant; client registration + status changes are
Admin-only. Every write is audited by the service. Client codes are 3 uppercase
letters and unique; project ids are `<CLIENT_CODE>-<seq>` minted per client.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.db import get_db
from app.modules.projects import service
from app.modules.projects.models import Project, ProjectClient
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.rbac import can

router = APIRouter()

MODULE_KEY = "projects"


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed `%`/`_` matches literally (paired with
    `escape="\\"` on the `.ilike()`). Without this a literal `%` in `q` matches every
    row. The backslash itself is escaped first so it stays the escape char."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


def _require_admin(user: User) -> None:
    if not can(user, "project.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "project.manage requires ADMIN")


def _require_client_manage(user: User) -> None:
    """Client-master writes require PROJECTS MANAGE (`client.manage`)."""
    if not can(user, "client.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "client.manage required")


# ------------------------------------------------------------------- schemas

class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Case-insensitive at the edge so 'bri' normalizes (and dedupes) to 'BRI' in
    # the service; a wrong LENGTH/charset (e.g. "BR", "BRIX", "12A") 422s here.
    code: str = Field(pattern=r"^[A-Za-z]{3}$")
    # Optional master fields, captured at registration (same fields editable later via
    # PATCH). Normalized + length-checked in the service; `ge=0` gives a clean 422 on a
    # negative term. `pan` accepts up to 15 raw chars here (pre-strip); the service caps
    # the normalized value at 10.
    pan: str | None = Field(default=None, max_length=15)
    credit_terms_days: int | None = Field(default=None, ge=0)


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    code: str
    pan: str | None = None
    credit_terms_days: int | None = None
    active: bool
    # Readable shared secret (the challan-QR password); exposed so the FE can show
    # whether one is set. Staff share it out-of-band; it is never printed.
    access_pin: str | None = None


class ProjectIn(BaseModel):
    client_id: int
    name: str = Field(min_length=1, max_length=200)
    start_date: date | None = None
    description: str | None = Field(default=None, max_length=1000)


class ProjectPatchIn(BaseModel):
    # All optional — a PATCH changes only the fields actually supplied. Editing a
    # project's typed details (to fix a mistake, e.g. a typo in the name) lives on
    # the SAME endpoint + gate as the status change. CODE + CLIENT are omitted on
    # purpose: they are system-assigned identity (`code` is unique + derived from
    # the client), not correctable details, so there is no way to edit them here.
    #
    # `name` cannot be cleared (the column is NOT NULL) — an explicit null is a 422.
    # `start_date` / `description` are nullable, so sending an explicit null CLEARS
    # them (distinct from omitting the key, which leaves the value unchanged).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    start_date: date | None = None
    description: str | None = Field(default=None, max_length=1000)
    status: Literal["ACTIVE", "ON_HOLD", "CLOSED"] | None = None


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
            db,
            name=body.name,
            code=body.code,
            pan=body.pan,
            credit_terms_days=body.credit_terms_days,
            actor_uid=user.firebase_uid,
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
    rbac.require_level(user, rbac.PROJECTS, Level.OPERATE)
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
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            Project.name.ilike(like, escape="\\") | Project.code.ilike(like, escape="\\")
        )
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
    body: ProjectPatchIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectOut:
    """Edit a project's editable details (name / start date / description) and/or
    change its status. Same MANAGE gate (`project.manage`) as the status change;
    every write is audited by the service. CODE + CLIENT are system-assigned
    identity and are NOT accepted here (see `ProjectPatchIn`)."""
    _require_admin(user)
    project = db.execute(
        select(Project).options(joinedload(Project.client)).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    # `exclude_unset` keeps "omitted" distinct from "explicit null" (which CLEARS a
    # nullable field); status is routed to its own service call so its dedicated
    # `project.status_changed` audit action is preserved.
    fields = body.model_dump(exclude_unset=True)
    new_status = fields.pop("status", None)
    try:
        if fields:
            service.update_project(
                db, project=project, actor_uid=user.firebase_uid, **fields
            )
        if "status" in body.model_fields_set and new_status is not None:
            service.set_status(
                db, project=project, status=new_status, actor_uid=user.firebase_uid
            )
    except service.ProjectError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(project)
    return _project_out(project)


# ------------------------------------------------------------- client master
#
# The promoted client master: a client owns child GSTINs / addresses / contacts.
# Reads need the `projects` module (VIEW); every write needs `client.manage`
# (PROJECTS MANAGE) and is audited by the service. GSTINs are 15-char alphanumeric
# and unique per client; deletes are soft (active=False).


class ClientUpdateIn(BaseModel):
    # All optional: a PATCH only changes the fields actually supplied. `pan`/
    # `credit_terms_days` are nullable, so sending an explicit null CLEARS them
    # (distinct from omitting the key, which leaves the value unchanged).
    name: str | None = Field(default=None, min_length=1, max_length=200)
    pan: str | None = Field(default=None, max_length=10)
    credit_terms_days: int | None = Field(default=None, ge=0)
    active: bool | None = None
    # The challan-QR shared secret. Sending "" or null CLEARS it; a set value must
    # be 4-32 chars (enforced in the service). Omit the key to leave it unchanged.
    access_pin: str | None = Field(default=None, max_length=32)


class GstinIn(BaseModel):
    gstin: str = Field(min_length=1, max_length=15)
    legal_name: str | None = Field(default=None, max_length=200)
    state_code: str | None = Field(default=None, max_length=2)
    is_default: bool = False


class GstinUpdateIn(BaseModel):
    gstin: str | None = Field(default=None, min_length=1, max_length=15)
    legal_name: str | None = Field(default=None, max_length=200)
    state_code: str | None = Field(default=None, max_length=2)
    is_default: bool | None = None


class AddressIn(BaseModel):
    gstin_id: int | None = None
    label: str | None = Field(default=None, max_length=120)
    line1: str = Field(min_length=1, max_length=300)
    line2: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=10)
    is_default: bool = False


class AddressUpdateIn(BaseModel):
    gstin_id: int | None = None
    label: str | None = Field(default=None, max_length=120)
    line1: str | None = Field(default=None, min_length=1, max_length=300)
    line2: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, max_length=10)
    is_default: bool | None = None


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=20)
    designation: str | None = Field(default=None, max_length=120)
    is_default: bool = False


class ContactUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=20)
    designation: str | None = Field(default=None, max_length=120)
    is_default: bool | None = None


class GstinOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    gstin: str
    legal_name: str | None
    state_code: str | None
    is_default: bool
    active: bool


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    gstin_id: int | None
    label: str | None
    line1: str
    line2: str | None
    city: str | None
    state: str | None
    pincode: str | None
    is_default: bool
    active: bool


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    email: str | None
    phone: str | None
    designation: str | None
    is_default: bool
    active: bool


class ClientDetailOut(BaseModel):
    id: int
    name: str
    code: str
    pan: str | None
    credit_terms_days: int | None
    active: bool
    # Readable shared secret; present so the edit UI can show/prefill whether a PIN
    # is set. Not printed on the challan — staff share it with the client directly.
    access_pin: str | None
    gstins: list[GstinOut]
    addresses: list[AddressOut]
    contacts: list[ContactOut]


def _default_first(rows: list[Any]) -> list[Any]:
    """Order children for display: the default first, then by id (stable)."""
    return sorted(rows, key=lambda r: (not r.is_default, r.id))


def _client_detail_out(client: ProjectClient) -> ClientDetailOut:
    """Serialize a client with its ACTIVE children (soft-deleted rows are hidden)."""
    gstins = [g for g in client.gstins if g.active]
    addresses = [a for a in client.addresses if a.active]
    contacts = [c for c in client.contacts if c.active]
    return ClientDetailOut(
        id=client.id,
        name=client.name,
        code=client.code,
        pan=client.pan,
        credit_terms_days=client.credit_terms_days,
        active=client.active,
        access_pin=client.access_pin,
        gstins=[GstinOut.model_validate(g) for g in _default_first(gstins)],
        addresses=[AddressOut.model_validate(a) for a in _default_first(addresses)],
        contacts=[ContactOut.model_validate(c) for c in _default_first(contacts)],
    )


def _load_client(db: Session, client_id: int) -> ProjectClient:
    client = service.get_client(db, client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
    return client


def _map_client_error(exc: service.ProjectError) -> HTTPException:
    """Translate a service typed error to the right HTTP status."""
    if isinstance(exc, service.DuplicateClientGstin):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, service.ClientChildNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    # InvalidGstin + any other ProjectError (bad name/terms) -> 422
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


@router.get("/projects/clients/{client_id}", response_model=ClientDetailOut)
def get_client_detail(
    client_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ClientDetailOut:
    _require_module(user)
    client = _load_client(db, client_id)
    return _client_detail_out(client)


@router.patch("/projects/clients/{client_id}", response_model=ClientOut)
def update_client(
    client_id: int,
    body: ClientUpdateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectClient:
    _require_client_manage(user)
    client = _load_client(db, client_id)
    try:
        service.update_client(
            db,
            client=client,
            actor_uid=user.firebase_uid,
            **body.model_dump(exclude_unset=True),
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(client)
    return client


# ------------------------------------------------------------------ gstins


@router.post(
    "/projects/clients/{client_id}/gstins", response_model=GstinOut, status_code=201
)
def add_client_gstin(
    client_id: int,
    body: GstinIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> GstinOut:
    _require_client_manage(user)
    client = _load_client(db, client_id)
    try:
        row = service.add_gstin(
            db,
            client=client,
            gstin=body.gstin,
            legal_name=body.legal_name,
            state_code=body.state_code,
            is_default=body.is_default,
            actor_uid=user.firebase_uid,
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(row)
    return GstinOut.model_validate(row)


@router.patch("/projects/clients/gstins/{gstin_id}", response_model=GstinOut)
def update_client_gstin(
    gstin_id: int,
    body: GstinUpdateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> GstinOut:
    _require_client_manage(user)
    row = service.get_gstin(db, gstin_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "gstin not found")
    try:
        service.update_gstin(
            db, row=row, actor_uid=user.firebase_uid, **body.model_dump(exclude_unset=True)
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(row)
    return GstinOut.model_validate(row)


@router.delete("/projects/clients/gstins/{gstin_id}", response_model=GstinOut)
def deactivate_client_gstin(
    gstin_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> GstinOut:
    _require_client_manage(user)
    row = service.get_gstin(db, gstin_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "gstin not found")
    service.deactivate_gstin(db, row=row, actor_uid=user.firebase_uid)
    db.commit()
    db.refresh(row)
    return GstinOut.model_validate(row)


# ---------------------------------------------------------------- addresses


@router.post(
    "/projects/clients/{client_id}/addresses", response_model=AddressOut, status_code=201
)
def add_client_address(
    client_id: int,
    body: AddressIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AddressOut:
    _require_client_manage(user)
    client = _load_client(db, client_id)
    try:
        row = service.add_address(
            db,
            client=client,
            line1=body.line1,
            gstin_id=body.gstin_id,
            label=body.label,
            line2=body.line2,
            city=body.city,
            state=body.state,
            pincode=body.pincode,
            is_default=body.is_default,
            actor_uid=user.firebase_uid,
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(row)
    return AddressOut.model_validate(row)


@router.patch("/projects/clients/addresses/{address_id}", response_model=AddressOut)
def update_client_address(
    address_id: int,
    body: AddressUpdateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AddressOut:
    _require_client_manage(user)
    row = service.get_address(db, address_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "address not found")
    try:
        service.update_address(
            db, row=row, actor_uid=user.firebase_uid, **body.model_dump(exclude_unset=True)
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(row)
    return AddressOut.model_validate(row)


@router.delete("/projects/clients/addresses/{address_id}", response_model=AddressOut)
def deactivate_client_address(
    address_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AddressOut:
    _require_client_manage(user)
    row = service.get_address(db, address_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "address not found")
    service.deactivate_address(db, row=row, actor_uid=user.firebase_uid)
    db.commit()
    db.refresh(row)
    return AddressOut.model_validate(row)


# ----------------------------------------------------------------- contacts


@router.post(
    "/projects/clients/{client_id}/contacts", response_model=ContactOut, status_code=201
)
def add_client_contact(
    client_id: int,
    body: ContactIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ContactOut:
    _require_client_manage(user)
    client = _load_client(db, client_id)
    row = service.add_contact(
        db,
        client=client,
        name=body.name,
        email=body.email,
        phone=body.phone,
        designation=body.designation,
        is_default=body.is_default,
        actor_uid=user.firebase_uid,
    )
    db.commit()
    db.refresh(row)
    return ContactOut.model_validate(row)


@router.patch("/projects/clients/contacts/{contact_id}", response_model=ContactOut)
def update_client_contact(
    contact_id: int,
    body: ContactUpdateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ContactOut:
    _require_client_manage(user)
    row = service.get_contact(db, contact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    try:
        service.update_contact(
            db, row=row, actor_uid=user.firebase_uid, **body.model_dump(exclude_unset=True)
        )
    except service.ProjectError as exc:
        raise _map_client_error(exc) from exc
    db.commit()
    db.refresh(row)
    return ContactOut.model_validate(row)


@router.delete("/projects/clients/contacts/{contact_id}", response_model=ContactOut)
def deactivate_client_contact(
    contact_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ContactOut:
    _require_client_manage(user)
    row = service.get_contact(db, contact_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    service.deactivate_contact(db, row=row, actor_uid=user.firebase_uid)
    db.commit()
    db.refresh(row)
    return ContactOut.model_validate(row)
