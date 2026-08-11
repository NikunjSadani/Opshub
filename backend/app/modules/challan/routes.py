"""Challan generator operator surface (mounted at `/api/v1`).

  POST /challan/batches                 -> upload Excel + validate (module-gated)
  POST /challan/batches/{id}/generate   -> reserve+render+issue in background
  GET  /challan/batches | /batches/{id} -> batch status + artifact file ids
  GET  /challan/challans                -> the challan register (filter series/fy/status)
  POST /challan/challans/{id}/void      -> ADMIN void a challan + its number

Artifacts (error report / PDFs / ZIP / merged) are StoredFiles tagged with the
document_automation module, so they download via `/api/v1/files/{id}/download`
under that module's access gate. Uploads/reads need the module grant; void is
Admin-only (`challan.void`).
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.challan import render, service
from app.modules.challan.models import BatchStatus, Challan, ChallanBatch, ChallanStatus
from app.modules.files.models import StoredFile
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can, can_access_module
from app.platform.storage import get_storage

router = APIRouter()

MODULE_KEY = "document_automation"
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_filename(name: str) -> str:
    return _CONTROL.sub("", name).strip()[:512] or "upload.xlsx"


def _require_module(user: User) -> None:
    if not can_access_module(user, MODULE_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access to document automation")


# ------------------------------------------------------------------- schemas

class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    challan_count: int
    line_count: int
    message: str | None
    error_report_file_id: int | None
    zip_file_id: int | None
    merged_pdf_file_id: int | None


class GenerateBody(BaseModel):
    series: str = Field(default="L", min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]{1,8}$")


class ChallanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    number: str
    series: str
    fy: str
    challan_date: Any
    consignee_brand: str
    consignee_name: str
    ship_to_state: str
    eway_required: bool
    total_paise: int | None  # None for a value-free challan
    status: str
    pdf_file_id: int | None


class SeriesBreakdownOut(BaseModel):
    series: str
    fy: str
    issued: int
    void: int
    total_value_paise: int


class ChallanSummaryOut(BaseModel):
    issued_count: int
    void_count: int
    eway_count: int
    valued_count: int
    total_value_paise: int
    by_series: list[SeriesBreakdownOut]


class VoidBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


# ------------------------------------------------------------- upload/validate

@router.post("/challan/batches", response_model=BatchOut, status_code=201)
def upload_batch(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
) -> ChallanBatch:
    """Upload an Excel workbook and validate it synchronously (no rendering)."""
    _require_module(user)
    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):  # cap BEFORE appending, so peak
        if len(data) + len(chunk) > max_bytes:      # buffered bytes never exceed the cap
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)

    filename = _sanitize_filename(file.filename or "upload.xlsx")
    # Storage key is a fresh uuid — the client filename never enters the path.
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix
    ref = get_storage().save(f"challan-source/{uuid4().hex}{suffix}", bytes(data))
    source = StoredFile(
        kind="challan-source",
        filename=filename,
        content_type=file.content_type,
        size=len(data),
        storage_ref=ref,
        uploaded_by=user.firebase_uid,
        module_key=MODULE_KEY,
    )
    db.add(source)
    db.flush()

    batch = service.new_batch(db, source_file_id=source.id, actor_uid=user.firebase_uid)
    service.validate_batch(db, batch, actor_uid=user.firebase_uid)
    db.refresh(batch)
    return batch


# ------------------------------------------------------------------ generate

def _generate_worker(batch_id: int, series: str, actor_uid: str | None) -> None:
    """Background entrypoint: opens its own session, renders + issues the batch."""
    from app.db import SessionLocal  # local import to avoid request-scope coupling

    db = SessionLocal()
    try:
        batch = db.get(ChallanBatch, batch_id)
        if batch is not None:
            service.generate(db, batch, render.WeasyPrintRenderer(),
                             series=series, actor_uid=actor_uid)
    finally:
        db.close()


@router.post("/challan/batches/{batch_id}/generate", response_model=BatchOut)
def generate_batch(
    batch_id: int,
    body: GenerateBody,
    background: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    """Kick off reserve->render->issue in the background for a VALIDATED batch."""
    _require_module(user)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    # Atomically claim the batch: only a VALIDATED/FAILED row flips to GENERATING,
    # and only ONE of two overlapping requests wins the UPDATE — so we never
    # schedule two workers that race the batch's final status. The loser gets 409.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(ChallanBatch)
            .where(
                ChallanBatch.id == batch_id,
                ChallanBatch.status.in_(
                    [BatchStatus.VALIDATED.value, BatchStatus.FAILED.value]
                ),
            )
            .values(status=BatchStatus.GENERATING.value)
            .execution_options(synchronize_session=False)
        ),
    ).rowcount
    db.commit()
    db.refresh(batch)
    if not claimed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"batch is {batch.status}, expected VALIDATED or FAILED (retry)",
        )
    background.add_task(_generate_worker, batch.id, body.series, user.firebase_uid)
    return batch


# --------------------------------------------------------------------- reads

@router.get("/challan/batches", response_model=list[BatchOut])
def list_batches(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ChallanBatch]:
    _require_module(user)
    return list(db.execute(
        select(ChallanBatch).order_by(ChallanBatch.id.desc()).limit(limit)
    ).scalars())


@router.get("/challan/batches/{batch_id}", response_model=BatchOut)
def get_batch(
    batch_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    _require_module(user)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    return batch


@router.get("/challan/challans", response_model=list[ChallanOut])
def list_challans(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str | None, Query(max_length=8)] = None,
    fy: Annotated[str | None, Query(max_length=7)] = None,
    status_filter: Annotated[ChallanStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Challan]:
    _require_module(user)
    stmt = select(Challan)
    if series:
        stmt = stmt.where(Challan.series == series.strip().upper())
    if fy:
        stmt = stmt.where(Challan.fy == fy)
    if status_filter is not None:
        stmt = stmt.where(Challan.status == status_filter.value)
    stmt = stmt.order_by(Challan.id.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


@router.get("/challan/summary", response_model=ChallanSummaryOut)
def challan_summary(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    fy: Annotated[str | None, Query(max_length=7)] = None,
    series: Annotated[str | None, Query(max_length=8)] = None,
) -> ChallanSummaryOut:
    """Aggregate counts + value over the challan register (module-gated, read-only).

    Value (`total_value_paise`) sums ISSUED rows only, ignoring VOID rows and
    value-free (NULL `total_paise`) rows; an empty scope yields 0, never NULL.
    """
    _require_module(user)
    issued = ChallanStatus.ISSUED.value
    void = ChallanStatus.VOID.value

    conds = []
    if series:
        conds.append(Challan.series == series.strip().upper())
    if fy:
        conds.append(Challan.fy == fy)

    issued_hit = case((Challan.status == issued, 1), else_=0)
    void_hit = case((Challan.status == void, 1), else_=0)
    eway_hit = case(
        ((Challan.status == issued) & (Challan.eway_required.is_(True)), 1), else_=0
    )
    valued_hit = case(
        ((Challan.status == issued) & (Challan.total_paise.is_not(None)), 1), else_=0
    )
    issued_value = case((Challan.status == issued, Challan.total_paise), else_=None)

    agg = db.execute(
        select(
            func.coalesce(func.sum(issued_hit), 0).label("issued_count"),
            func.coalesce(func.sum(void_hit), 0).label("void_count"),
            func.coalesce(func.sum(eway_hit), 0).label("eway_count"),
            func.coalesce(func.sum(valued_hit), 0).label("valued_count"),
            func.coalesce(func.sum(issued_value), 0).label("total_value_paise"),
        ).where(*conds)
    ).one()

    grp_stmt = (
        select(
            Challan.series.label("series"),
            Challan.fy.label("fy"),
            func.coalesce(func.sum(issued_hit), 0).label("issued"),
            func.coalesce(func.sum(void_hit), 0).label("void"),
            func.coalesce(func.sum(issued_value), 0).label("total_value_paise"),
        )
        .where(*conds)
        .group_by(Challan.series, Challan.fy)
        .order_by(Challan.fy.desc(), Challan.series.asc())
    )
    by_series = [
        SeriesBreakdownOut(
            series=row.series,
            fy=row.fy,
            issued=int(row.issued or 0),
            void=int(row.void or 0),
            total_value_paise=int(row.total_value_paise or 0),
        )
        for row in db.execute(grp_stmt)
    ]

    return ChallanSummaryOut(
        issued_count=int(agg.issued_count or 0),
        void_count=int(agg.void_count or 0),
        eway_count=int(agg.eway_count or 0),
        valued_count=int(agg.valued_count or 0),
        total_value_paise=int(agg.total_value_paise or 0),
        by_series=by_series,
    )


# --------------------------------------------------------------------- void

@router.post("/challan/challans/{challan_id}/void", response_model=ChallanOut)
def void_challan(
    challan_id: int,
    body: VoidBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Challan:
    """Void a challan and its bound number. ADMIN only."""
    if not can(user, "challan.void"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "challan.void requires ADMIN")
    challan = db.get(Challan, challan_id)
    if challan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challan not found")
    try:
        service.void_challan(db, challan, reason=body.reason, actor_uid=user.firebase_uid)
    except service.ChallanError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.refresh(challan)
    return challan
