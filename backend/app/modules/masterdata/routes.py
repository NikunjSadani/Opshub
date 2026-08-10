"""Master-data CRUD (mounted at `/api/v1`, giving `/api/v1/masterdata/...`).

  GET  /masterdata/{kind}            -> list (module-gated read)
  POST /masterdata/{kind}            -> create (ADMIN `masterdata.edit`, audited)
  PUT  /masterdata/{kind}/{id}       -> update (ADMIN, audited)
  POST /masterdata/{kind}/{id}/active-> enable/disable (ADMIN, audited)

`{kind}` in consignor | consignee | hsn | series. Rows are soft-disabled
(`active=false`), never hard-deleted, so an issued challan's provenance stays
resolvable. Writes are Admin-only and audited; reads need the document_automation
module grant.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.masterdata.models import Consignee, Consignor, HsnCode, Series
from app.modules.masterdata.normalize import collapse_ws, valid_gstin_state
from app.platform import audit
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can, can_access_module

router = APIRouter()

MODULE_KEY = "document_automation"
_GSTIN = r"^[0-9A-Z]{15}$"


def _clean_text(v: str) -> str:
    return collapse_ws(v)


def _check_gstin(v: str) -> str:
    if not valid_gstin_state(v):
        raise ValueError("GSTIN state code is not a valid GST state code")
    return v


def _check_rate_scale(v: Decimal) -> Decimal:
    exp = v.as_tuple().exponent  # int for finite Decimals; 'n'/'N'/'F' for NaN/Inf
    if isinstance(exp, int) and exp < -2:  # >2 decimal places would be quantized
        raise ValueError("gst_rate supports at most 2 decimal places")
    return v


def _save(db: Session) -> None:
    """Commit, translating a UNIQUE violation (concurrent create) into a 409.

    The pre-checks catch the common case; this makes the race return a clean
    conflict instead of a 500 while the DB backstop preserves integrity.
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate key") from exc


def _flush_unique(db: Session) -> None:
    """Flush a pending insert, translating a UNIQUE violation into a 409.

    Needed because the case-insensitive consignee index (and any concurrent-create
    race) fires at flush time — before `_save` — and the case-sensitive pre-check
    can't see a case/whitespace variant.
    """
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "duplicate key") from exc


def _require_read(user: User) -> None:
    if not can_access_module(user, MODULE_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to document automation")


def _require_edit(user: User) -> None:
    if not can(user, "masterdata.edit"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "masterdata.edit requires ADMIN")


# ------------------------------------------------------------------- schemas

class ConsignorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gstin: str = Field(pattern=_GSTIN)
    state: str = Field(min_length=1, max_length=60)
    address: str = Field(default="", max_length=600)

    _norm = field_validator("name", "state", mode="after")(_clean_text)
    _gstin = field_validator("gstin", mode="after")(_check_gstin)


class ConsignorOut(ConsignorIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class ConsigneeIn(BaseModel):
    brand: str = Field(min_length=1, max_length=120)
    state: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    gstin: str = Field(pattern=_GSTIN)
    address: str = Field(default="", max_length=600)

    _norm = field_validator("brand", "state", "name", mode="after")(_clean_text)
    _gstin = field_validator("gstin", mode="after")(_check_gstin)


class ConsigneeOut(ConsigneeIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class HsnIn(BaseModel):
    hsn: str = Field(min_length=2, max_length=12, pattern=r"^[0-9]{2,12}$")
    description: str = Field(default="", max_length=300)
    gst_rate: Decimal = Field(ge=0, le=100)

    _scale = field_validator("gst_rate", mode="after")(_check_rate_scale)


class HsnOut(HsnIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class SeriesIn(BaseModel):
    letter: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]{1,8}$")
    label: str = Field(default="", max_length=120)


class SeriesOut(SeriesIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool


class ActiveIn(BaseModel):
    active: bool


def _audit(
    db: Session, user: User, action: str, kind: str, entity_id: str, detail: dict[str, Any]
) -> None:
    audit.log(
        db,
        action=action,
        actor_uid=user.firebase_uid,
        entity=f"md_{kind}",
        entity_id=entity_id,
        detail=detail,
    )


# ------------------------------------------------------------------ consignor

@router.get("/masterdata/consignor", response_model=list[ConsignorOut])
def list_consignor(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    active: Annotated[bool | None, Query()] = None,
) -> list[Consignor]:
    _require_read(user)
    stmt = select(Consignor).order_by(Consignor.name)
    if active is not None:
        stmt = stmt.where(Consignor.active == active)
    return list(db.execute(stmt).scalars())


@router.post("/masterdata/consignor", response_model=ConsignorOut, status_code=201)
def create_consignor(
    body: ConsignorIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignor:
    _require_edit(user)
    row = Consignor(**body.model_dump(), updated_by=user.firebase_uid)
    db.add(row)
    db.flush()
    _audit(db, user, "masterdata.create", "consignor", str(row.id), body.model_dump())
    db.commit()
    db.refresh(row)
    return row


@router.put("/masterdata/consignor/{row_id}", response_model=ConsignorOut)
def update_consignor(
    row_id: int,
    body: ConsignorIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignor:
    _require_edit(user)
    row = db.get(Consignor, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.update", "consignor", str(row_id), body.model_dump())
    db.commit()
    db.refresh(row)
    return row


@router.post("/masterdata/consignor/{row_id}/active", response_model=ConsignorOut)
def set_active_consignor(
    row_id: int,
    body: ActiveIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignor:
    _require_edit(user)
    row = db.get(Consignor, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    row.active = body.active
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.active", "consignor", str(row_id), {"active": body.active})
    db.commit()
    db.refresh(row)
    return row


# ------------------------------------------------------------------ consignee

@router.get("/masterdata/consignee", response_model=list[ConsigneeOut])
def list_consignee(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    brand: Annotated[str | None, Query(max_length=120)] = None,
    state: Annotated[str | None, Query(max_length=60)] = None,
    active: Annotated[bool | None, Query()] = None,
) -> list[Consignee]:
    _require_read(user)
    stmt = select(Consignee).order_by(Consignee.brand, Consignee.state)
    if brand:
        stmt = stmt.where(Consignee.brand == brand)
    if state:
        stmt = stmt.where(Consignee.state == state)
    if active is not None:
        stmt = stmt.where(Consignee.active == active)
    return list(db.execute(stmt).scalars())


@router.post("/masterdata/consignee", response_model=ConsigneeOut, status_code=201)
def create_consignee(
    body: ConsigneeIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignee:
    _require_edit(user)
    if db.execute(
        select(Consignee).where(Consignee.brand == body.brand, Consignee.state == body.state)
    ).scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "brand+state already exists")
    row = Consignee(**body.model_dump(), updated_by=user.firebase_uid)
    db.add(row)
    _flush_unique(db)
    _audit(db, user, "masterdata.create", "consignee", str(row.id), body.model_dump())
    _save(db)
    db.refresh(row)
    return row


@router.put("/masterdata/consignee/{row_id}", response_model=ConsigneeOut)
def update_consignee(
    row_id: int,
    body: ConsigneeIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignee:
    _require_edit(user)
    row = db.get(Consignee, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    clash = db.execute(
        select(Consignee).where(Consignee.brand == body.brand, Consignee.state == body.state)
    ).scalar_one_or_none()
    if clash is not None and clash.id != row_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "brand+state already exists")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.update", "consignee", str(row_id), body.model_dump())
    _save(db)
    db.refresh(row)
    return row


@router.post("/masterdata/consignee/{row_id}/active", response_model=ConsigneeOut)
def set_active_consignee(
    row_id: int,
    body: ActiveIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Consignee:
    _require_edit(user)
    row = db.get(Consignee, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    row.active = body.active
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.active", "consignee", str(row_id), {"active": body.active})
    db.commit()
    db.refresh(row)
    return row


# ----------------------------------------------------------------------- hsn

@router.get("/masterdata/hsn", response_model=list[HsnOut])
def list_hsn(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    active: Annotated[bool | None, Query()] = None,
) -> list[HsnCode]:
    _require_read(user)
    stmt = select(HsnCode).order_by(HsnCode.hsn)
    if active is not None:
        stmt = stmt.where(HsnCode.active == active)
    return list(db.execute(stmt).scalars())


@router.post("/masterdata/hsn", response_model=HsnOut, status_code=201)
def create_hsn(
    body: HsnIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> HsnCode:
    _require_edit(user)
    if db.execute(select(HsnCode).where(HsnCode.hsn == body.hsn)).scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "hsn already exists")
    row = HsnCode(**body.model_dump(), updated_by=user.firebase_uid)
    db.add(row)
    _flush_unique(db)
    _audit(db, user, "masterdata.create", "hsn", str(row.id), body.model_dump(mode="json"))
    _save(db)
    db.refresh(row)
    return row


@router.put("/masterdata/hsn/{row_id}", response_model=HsnOut)
def update_hsn(
    row_id: int,
    body: HsnIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> HsnCode:
    _require_edit(user)
    row = db.get(HsnCode, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    clash = db.execute(select(HsnCode).where(HsnCode.hsn == body.hsn)).scalar_one_or_none()
    if clash is not None and clash.id != row_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "hsn already exists")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.update", "hsn", str(row_id), body.model_dump(mode="json"))
    _save(db)
    db.refresh(row)
    return row


@router.post("/masterdata/hsn/{row_id}/active", response_model=HsnOut)
def set_active_hsn(
    row_id: int,
    body: ActiveIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> HsnCode:
    _require_edit(user)
    row = db.get(HsnCode, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    row.active = body.active
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.active", "hsn", str(row_id), {"active": body.active})
    db.commit()
    db.refresh(row)
    return row


# -------------------------------------------------------------------- series

@router.get("/masterdata/series", response_model=list[SeriesOut])
def list_series(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    active: Annotated[bool | None, Query()] = None,
) -> list[Series]:
    _require_read(user)
    stmt = select(Series).order_by(Series.letter)
    if active is not None:
        stmt = stmt.where(Series.active == active)
    return list(db.execute(stmt).scalars())


@router.post("/masterdata/series", response_model=SeriesOut, status_code=201)
def create_series(
    body: SeriesIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Series:
    _require_edit(user)
    letter = body.letter.upper()
    if db.execute(select(Series).where(Series.letter == letter)).scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "series already exists")
    row = Series(letter=letter, label=body.label, updated_by=user.firebase_uid)
    db.add(row)
    _flush_unique(db)
    _audit(db, user, "masterdata.create", "series", str(row.id),
           {"letter": letter, "label": body.label})
    _save(db)
    db.refresh(row)
    return row


@router.put("/masterdata/series/{row_id}", response_model=SeriesOut)
def update_series(
    row_id: int,
    body: SeriesIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Series:
    _require_edit(user)
    row = db.get(Series, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    letter = body.letter.upper()
    clash = db.execute(select(Series).where(Series.letter == letter)).scalar_one_or_none()
    if clash is not None and clash.id != row_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "series already exists")
    row.letter = letter
    row.label = body.label
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.update", "series", str(row_id),
           {"letter": letter, "label": body.label})
    _save(db)
    db.refresh(row)
    return row


@router.post("/masterdata/series/{row_id}/active", response_model=SeriesOut)
def set_active_series(
    row_id: int,
    body: ActiveIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Series:
    _require_edit(user)
    row = db.get(Series, row_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    row.active = body.active
    row.updated_by = user.firebase_uid
    _audit(db, user, "masterdata.active", "series", str(row_id), {"active": body.active})
    db.commit()
    db.refresh(row)
    return row
