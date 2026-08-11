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
from typing import Annotated, Any
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
from sqlalchemy import select
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
    while chunk := file.file.read(_UPLOAD_CHUNK):  # cap BEFORE buffering the whole body
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

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
    if batch.status not in (BatchStatus.VALIDATED.value, BatchStatus.FAILED.value):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"batch is {batch.status}, expected VALIDATED or FAILED (retry)")
    batch.status = BatchStatus.GENERATING.value
    db.commit()
    db.refresh(batch)
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
