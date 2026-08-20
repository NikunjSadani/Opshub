"""Expense/Invoice HTTP surface (mounted at `/api/v1`, module key `expense_invoice`).

  POST   /expense/invoices             -> bulk upload N PDFs -> extract -> per-file outcome
  GET    /expense/invoices[.csv]       -> searchable register + CSV export
  GET    /expense/invoices/{id}        -> canonical record + fields + line items
  GET    /expense/invoices/{id}/reviews -> the per-field review surface
  PATCH  /expense/invoices/{id}/reviews -> apply corrections and/or confirm
  DELETE /expense/invoices/{id}        -> delete (supports delete-and-re-upload on a dup)

Every route is gated on `can_access_module(MODULE_KEY)`; every mutation is audited.
RBAC: review / register / corrections / confirm are module-gated; deleting a
CONFIRMED invoice additionally requires `expense.delete` (admin) — deleting an
unconfirmed invoice stays module-gated (delete-and-re-upload on a dup).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import PurePosixPath
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.expense import service
from app.modules.expense.models import Invoice, InvoiceStatus
from app.modules.files.models import StoredFile
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.rbac import can
from app.platform.storage import get_storage

router = APIRouter()

MODULE_KEY = "expense_invoice"
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_UPLOAD_FILES = 100


def _sanitize_filename(name: str) -> str:
    return _CONTROL.sub("", name).strip()[:512] or "invoice.pdf"


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


def _get_invoice(db: Session, invoice_id: int) -> Invoice:
    try:
        return service.get_invoice(db, invoice_id)
    except service.ExpenseNotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found") from err


def _map_service_error(err: service.ExpenseError) -> HTTPException:
    if isinstance(err, service.ExpenseNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(err))
    if isinstance(err, service.ExpenseBadRequest):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))
    if isinstance(err, service.ExpenseForbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(err))
    if isinstance(err, service.ExpenseConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(err))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# ------------------------------------------------------------------- schemas

class FileOutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    file_id: int
    filename: str                     # the stored/sanitized upload filename for this file
    status: str                       # EXTRACTED|NEEDS_REVIEW|NEEDS_OCR|REJECTED|DUPLICATE
    invoice_id: int | None
    duplicate_of: int | None          # the EXISTING invoice id on a DUPLICATE (409-style)
    supplier_name: str | None
    invoice_number: str | None
    grand_total_paise: int | None
    review_reasons: list[str]         # extraction review flags ([] when N/A)
    message: str | None


class UploadOut(BaseModel):
    batch_id: int
    invoice_count: int
    outcomes: list[FileOutcomeOut]


class InvoiceOut(BaseModel):
    """One register row."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    needs_ocr: bool
    supplier_name: str | None
    supplier_gstin: str | None
    invoice_number: str | None
    invoice_date: Any
    total_taxable_paise: int | None
    grand_total_paise: int | None
    created_at: Any


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    field_path: str
    value_normalized: str | None
    value_raw: str
    confidence: float
    source_engine: str
    page: int | None
    status: str


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    line_no: int
    description: str | None
    hsn_sac: str | None
    quantity: Any
    unit: str | None
    unit_rate_paise: int | None
    taxable_paise: int | None
    gst_rate: Any
    cgst_paise: int | None
    sgst_paise: int | None
    igst_paise: int | None
    line_total_paise: int | None


class InvoiceDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_id: int
    status: str
    needs_ocr: bool
    review_reasons: list[str]
    source_file_id: int | None
    supplier_name: str | None
    supplier_gstin: str | None
    supplier_address: str | None
    buyer_name: str | None
    buyer_gstin: str | None
    buyer_address: str | None
    invoice_number: str | None
    invoice_date: Any
    place_of_supply: str | None
    po_ref: str | None
    total_taxable_paise: int | None
    total_cgst_paise: int | None
    total_sgst_paise: int | None
    total_igst_paise: int | None
    round_off_paise: int | None
    grand_total_paise: int | None
    amount_in_words: str | None
    confirmed_by: str | None
    confirmed_at: Any
    fields: list[FieldOut]
    lines: list[LineOut]


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    review_reasons: list[str]
    fields: list[FieldOut]


class CorrectionItem(BaseModel):
    field_path: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=600)


class ReviewPatchBody(BaseModel):
    corrections: list[CorrectionItem] = Field(default_factory=list, max_length=200)
    confirm: bool = False


class DeleteOut(BaseModel):
    id: int
    deleted: bool


# ------------------------------------------------------------------- upload

@router.post("/expense/invoices", response_model=UploadOut, status_code=201)
def upload_invoices(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    files: Annotated[list[UploadFile], File()],
) -> UploadOut:
    """Bulk-upload N PDFs: store each blob, then extract + persist into one batch.

    Returns a per-file outcome list. A file whose identity key matches a stored
    invoice comes back as a DUPLICATE outcome carrying the existing invoice's id +
    summary. When the WHOLE request is duplicates (nothing new persisted) the HTTP
    status is 409; otherwise 201.
    """
    rbac.require_level(user, rbac.EXPENSE, Level.OPERATE)
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no files uploaded")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"too many files (max {MAX_UPLOAD_FILES})")

    max_bytes = get_settings().max_upload_bytes
    storage = get_storage()
    file_ids: list[int] = []
    filename_by_file: dict[int, str] = {}
    for upload in files:
        data = bytearray()
        while chunk := upload.file.read(_UPLOAD_CHUNK):  # cap BEFORE appending
            if len(data) + len(chunk) > max_bytes:
                raise HTTPException(
                    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
            data.extend(chunk)
        filename = _sanitize_filename(upload.filename or "invoice.pdf")
        suffix = PurePosixPath(filename.replace("\\", "/")).suffix
        ref = storage.save(f"expense-source/{uuid4().hex}{suffix}", bytes(data))
        sf = StoredFile(
            kind="expense-source",
            filename=filename,
            content_type=upload.content_type,
            size=len(data),
            storage_ref=ref,
            uploaded_by=user.firebase_uid,
            module_key=MODULE_KEY,
        )
        db.add(sf)
        db.flush()
        file_ids.append(sf.id)
        filename_by_file[sf.id] = filename

    result = service.create_batch(db, file_ids, actor_uid=user.firebase_uid)
    persisted = [o for o in result.outcomes if o.invoice_id is not None
                 and o.status != "DUPLICATE"]
    dups = [o for o in result.outcomes if o.status == "DUPLICATE"]
    if not persisted and dups:
        response.status_code = status.HTTP_409_CONFLICT
    # Echo the stored filename onto each per-file outcome (the service keys on file_id).
    for o in result.outcomes:
        o.filename = filename_by_file.get(o.file_id, "")
    return UploadOut(
        batch_id=result.batch.id,
        invoice_count=result.batch.invoice_count,
        outcomes=[FileOutcomeOut.model_validate(o) for o in result.outcomes],
    )


# ------------------------------------------------------------------- register

@router.get("/expense/invoices", response_model=list[InvoiceOut])
def list_invoices(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=300)] = None,
    supplier: Annotated[str | None, Query(max_length=300)] = None,
    gstin: Annotated[str | None, Query(max_length=15)] = None,
    status_filter: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Invoice]:
    _require_module(user)
    return service.list_invoices(
        db, q=q, supplier=supplier, gstin=gstin,
        status=status_filter.value if status_filter is not None else None,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )


@router.get("/expense/invoices.csv")
def export_invoices_csv(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=300)] = None,
    supplier: Annotated[str | None, Query(max_length=300)] = None,
    gstin: Annotated[str | None, Query(max_length=15)] = None,
    status_filter: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> Response:
    """Export the filtered register (newest-first) as CSV, capped at MAX_CSV_ROWS."""
    _require_module(user)
    rows = service.list_invoices(
        db, q=q, supplier=supplier, gstin=gstin,
        status=status_filter.value if status_filter is not None else None,
        date_from=date_from, date_to=date_to, limit=service.MAX_CSV_ROWS,
    )
    return Response(
        content=service.register_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="expense-register.csv"'},
    )


@router.get("/expense/invoices/{invoice_id}", response_model=InvoiceDetailOut)
def get_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Invoice:
    _require_module(user)
    return _get_invoice(db, invoice_id)


# ------------------------------------------------------------------- reviews

@router.get("/expense/invoices/{invoice_id}/reviews", response_model=ReviewOut)
def get_review(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Invoice:
    """The per-field review surface (status + envelope rows) for one invoice."""
    _require_module(user)
    return _get_invoice(db, invoice_id)


@router.patch("/expense/invoices/{invoice_id}/reviews", response_model=InvoiceDetailOut)
def submit_review(
    invoice_id: int,
    body: ReviewPatchBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Invoice:
    """Apply human corrections and/or confirm the invoice (module-gated).

    Corrections run first (each flips its field to CORRECTED, source "human", and
    overwrites the snapshot scalar), then — if `confirm` is set — the invoice is
    frozen to CONFIRMED (blocked while a required field is still weak)."""
    rbac.require_level(user, rbac.EXPENSE, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    if not body.corrections and not body.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "supply corrections and/or confirm=true")
    try:
        if body.corrections:
            service.submit_corrections(
                db, invoice,
                [(c.field_path, c.value) for c in body.corrections],
                actor_uid=user.firebase_uid,
            )
        if body.confirm:
            service.confirm_invoice(db, invoice, actor_uid=user.firebase_uid)
    except service.ExpenseError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


# ------------------------------------------------------------------- delete

@router.delete("/expense/invoices/{invoice_id}", response_model=DeleteOut)
def delete_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DeleteOut:
    """Delete an invoice. Deleting is a WRITE, so it needs at least OPERATE on the
    module (a read-only Viewer can never delete). Deleting a CONFIRMED (immutable)
    record additionally requires the `expense.delete` Manage action; an unconfirmed
    record only needs OPERATE (so a bad upload / hard-duplicate can be re-uploaded)."""
    rbac.require_level(user, rbac.EXPENSE, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    can_delete_confirmed = can(user, "expense.delete")
    if invoice.status == InvoiceStatus.CONFIRMED.value and not can_delete_confirmed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "deleting a confirmed invoice requires admin (expense.delete)")
    try:
        service.delete_invoice(
            db, invoice, actor_uid=user.firebase_uid,
            can_delete_confirmed=can_delete_confirmed)
    except service.ExpenseError as err:
        raise _map_service_error(err) from err
    return DeleteOut(id=invoice_id, deleted=True)
