"""Numbering operator surface (mounted at `/api/v1`, so `/api/v1/numbering/...`).

  POST /numbering/seed                  -> ADMIN mode-2 custom-start (audited)
  GET  /numbering/counters              -> current high-water mark per series/fy
  GET  /numbering/allocations           -> the register (filter by series/fy/status)
  POST /numbering/allocations/{id}/void -> ADMIN void a number (audited)

Allocation itself is NOT an HTTP endpoint: numbers are reserved by the challan
module as part of reserve-before-generate (`service.allocate`), never handed out
bare — an "issue me a number" route would just leak the sequence.

Reads require access to the Delivery Challan module (or Admin); the seed/void
mutations are Admin-only via `can()` and every mutation is audited.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.numbering import service
from app.modules.numbering.models import (
    AllocationStatus,
    NumberingAllocation,
    NumberingCounter,
)
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can, can_access_module

router = APIRouter()

MODULE_KEY = "document_automation"  # numbering serves the Delivery Challan module


def _require_module(user: User) -> None:
    if not can_access_module(user, MODULE_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to document automation")


# ------------------------------------------------------------------- schemas

class CounterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    series: str
    fy: str
    last_number: int


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    series: str
    fy: str
    number: int
    formatted: str
    status: str
    entity: str | None
    entity_id: str | None
    void_reason: str | None


class SeedBody(BaseModel):
    series: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]{1,8}$")
    fy: str | None = Field(default=None, pattern=r"^\d{2}-\d{2}$")
    last_number: int = Field(ge=0, le=999_999)


class VoidBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


# --------------------------------------------------------------------- seed

@router.post("/numbering/seed", response_model=CounterOut)
def seed_counter(
    body: SeedBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NumberingCounter:
    """Mode-2 custom-start: set the high-water mark for (series, fy). ADMIN only."""
    if not can(user, "series.seed"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "series.seed requires ADMIN")
    try:
        counter = service.seed_series(
            db,
            body.series,
            fy=body.fy,
            last_number=body.last_number,
            actor_uid=user.firebase_uid,  # service.seed_series audits inside this txn
        )
    except service.NumberingError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.commit()
    db.refresh(counter)
    return counter


# ------------------------------------------------------------------- reads

@router.get("/numbering/counters", response_model=list[CounterOut])
def list_counters(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[NumberingCounter]:
    """Current high-water mark per (series, fy)."""
    _require_module(user)
    return list(
        db.execute(
            select(NumberingCounter).order_by(
                NumberingCounter.fy.desc(), NumberingCounter.series
            )
        ).scalars()
    )


@router.get("/numbering/allocations", response_model=list[AllocationOut])
def list_allocations(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str | None, Query(max_length=8)] = None,
    fy: Annotated[str | None, Query(max_length=7)] = None,
    status_filter: Annotated[AllocationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NumberingAllocation]:
    """The numbering register, newest first. Filter by series / fy / status."""
    _require_module(user)
    stmt = select(NumberingAllocation)
    if series:
        # Match how series is stored (stripped + upper-cased) so a filter like
        # "l " still finds "L".
        stmt = stmt.where(NumberingAllocation.series == series.strip().upper())
    if fy:
        stmt = stmt.where(NumberingAllocation.fy == fy)
    if status_filter is not None:
        stmt = stmt.where(NumberingAllocation.status == status_filter.value)
    stmt = stmt.order_by(NumberingAllocation.id.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


# --------------------------------------------------------------------- void

@router.post("/numbering/allocations/{alloc_id}/void", response_model=AllocationOut)
def void_allocation(
    alloc_id: int,
    body: VoidBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> NumberingAllocation:
    """Void (cancel) a number. ADMIN only; the number is retained, never reused."""
    if not can(user, "challan.void"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "challan.void requires ADMIN")
    alloc = db.get(NumberingAllocation, alloc_id)
    if alloc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "allocation not found")
    try:
        service.void(db, alloc, reason=body.reason, actor_uid=user.firebase_uid)
    except service.NumberingError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.commit()
    db.refresh(alloc)
    return alloc
