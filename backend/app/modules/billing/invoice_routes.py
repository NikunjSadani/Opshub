"""Client-invoice capture HTTP surface (mounted at `/api/v1`, module key `billing`).

  POST   /billing/invoices                       -> bulk upload N PDFs -> extract -> match
  GET    /billing/invoices[?filters]             -> searchable register (VIEW)
  GET    /billing/invoices/{id}                   -> header + lines + fields + match state (VIEW)
  PATCH  /billing/invoices/{id}/review            -> apply corrections and/or confirm (OPERATE)
  PATCH  /billing/invoices/{id}/project           -> set/change/clear project attribution (OPERATE)
  POST   /billing/invoices/{id}/match             -> re-run the auto-matcher (OPERATE)
  PATCH  /billing/invoices/{id}/lines/{lineId}/match -> manual map to a PO line (OPERATE)
  POST   /billing/invoices/{id}/lines             -> add a manual line item (OPERATE)
  PATCH  /billing/invoices/{id}/lines/{lineId}     -> edit a line item (OPERATE)
  DELETE /billing/invoices/{id}/lines/{lineId}     -> delete a line item (OPERATE)
  POST   /billing/invoices/{id}/cancel            -> soft-cancel (MANAGE)
  DELETE /billing/invoices/{id}                   -> delete (supports delete-and-re-upload) (MANAGE)

Every route is RBAC-gated (`billing` module: VIEW to read, OPERATE to upload/match/review,
MANAGE to cancel/delete); every mutation is audited in the service layer.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
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
from app.modules.billing import invoice_service as service
from app.modules.billing.models import SalesInvoice, SalesInvoiceStatus
from app.modules.files.models import StoredFile
from app.modules.projects.models import ProjectClient
from app.modules.sales_orders.models import PurchaseOrder
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.storage import get_storage

router = APIRouter()

MODULE_KEY = "billing"
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_UPLOAD_FILES = 100


def _sanitize_filename(name: str) -> str:
    return _CONTROL.sub("", name).strip()[:512] or "invoice.pdf"


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


def _get_invoice(db: Session, invoice_id: int) -> SalesInvoice:
    try:
        return service.get_invoice(db, invoice_id)
    except service.BillingNotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found") from err


def _map_service_error(err: service.BillingError) -> HTTPException:
    if isinstance(err, service.BillingNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(err))
    if isinstance(err, service.BillingBadRequest):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))
    if isinstance(err, service.BillingForbidden):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(err))
    if isinstance(err, service.BillingConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(err))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# ------------------------------------------------------------------- schemas

class FileOutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    file_id: int
    filename: str
    status: str
    invoice_id: int | None
    duplicate_of: int | None
    buyer_gstin: str | None
    invoice_number: str | None
    grand_total_paise: int | None
    review_reasons: list[str]
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
    client_id: int
    po_id: int | None
    project_id: int | None
    supplier_gstin: str | None
    buyer_gstin: str | None
    invoice_number: str | None
    invoice_date: Any
    total_taxable_paise: int | None
    grand_total_paise: int | None
    created_at: Any


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    field_path: str
    value_norm: str | None
    value_raw: str | None
    confidence: float
    source_engine: str | None
    status: str


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    line_no: int
    po_line_item_id: int | None
    match_status: str
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
    batch_id: int | None
    status: str
    needs_ocr: bool
    review_reasons: list[str]
    client_id: int
    po_id: int | None
    project_id: int | None
    source_file_id: int | None
    supplier_gstin: str | None
    buyer_gstin: str | None
    invoice_number: str | None
    invoice_date: Any
    due_date: Any
    total_taxable_paise: int | None
    total_cgst_paise: int | None
    total_sgst_paise: int | None
    total_igst_paise: int | None
    round_off_paise: int | None
    grand_total_paise: int | None
    confirmed_by: str | None
    confirmed_at: Any
    fields: list[FieldOut]
    lines: list[LineOut]


class CorrectionItem(BaseModel):
    field_path: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=600)


class ReviewPatchBody(BaseModel):
    corrections: list[CorrectionItem] = Field(default_factory=list, max_length=200)
    confirm: bool = False


class ManualMatchBody(BaseModel):
    po_line_item_id: int


class LineWriteBody(BaseModel):
    """A manual line-item write (add or partial edit). Every field optional; paise on the
    wire (mirrors ``LineOut``). For an edit only the SUPPLIED fields change; on add an unset
    field defaults to None. line_no / match_status / po_line_item_id are NOT writable here."""

    description: str | None = Field(default=None, max_length=500)
    hsn_sac: str | None = Field(default=None, max_length=10)
    quantity: Decimal | None = None
    unit: str | None = Field(default=None, max_length=20)
    unit_rate_paise: int | None = None
    taxable_paise: int | None = None
    gst_rate: Decimal | None = None
    cgst_paise: int | None = None
    sgst_paise: int | None = None
    igst_paise: int | None = None
    line_total_paise: int | None = None


class SetProjectBody(BaseModel):
    # ``None`` clears the direct attribution (falls back to the PO, or unattributed).
    project_id: int | None = None


class DeleteOut(BaseModel):
    id: int
    deleted: bool


# ------------------------------------------------------------------- upload

@router.post("/billing/invoices", response_model=UploadOut, status_code=201)
def upload_invoices(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    files: Annotated[list[UploadFile], File()],
    client_id: Annotated[int, Form()],
    po_id: Annotated[int | None, Form()] = None,
    project_id: Annotated[int | None, Form()] = None,
) -> UploadOut:
    """Bulk-upload N client-invoice PDFs: store each blob, then extract + match + persist
    into one batch. ``client_id`` is required; ``po_id`` is optional but, when given, must
    belong to the client (validated BEFORE any blob is stored — a bad ref persists nothing).

    ``project_id`` is an OPTIONAL direct attribution for a PO-less invoice: when given it must
    be an ACTIVE project of the same client. It is stamped on every invoice in the batch so
    finance can resolve the invoice's revenue to that project (a PO, when present, still
    carries the project). Validated BEFORE any blob is stored.

    Returns a per-file outcome list. A file whose identity collides a stored invoice comes
    back as DUPLICATE carrying the existing id. When the WHOLE request is duplicates the HTTP
    status is 409; otherwise 201.
    """
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    if not files:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no files uploaded")
    # Validate the client + PO + project BEFORE storing any bytes (nothing persists on a bad ref).
    client = db.get(ProjectClient, client_id)
    if client is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "client not found")
    if po_id is not None:
        po = db.get(PurchaseOrder, po_id)
        if po is None or po.client_id != client_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "purchase order not found or does not belong to this client")
    try:
        service.validate_attribution_project(db, project_id, client_id)
    except service.BillingError as err:
        raise _map_service_error(err) from err
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
        ref = storage.save(f"sales-source/{uuid4().hex}{suffix}", bytes(data))
        sf = StoredFile(
            kind="sales-source",
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

    result = service.upload_invoices(
        db, file_ids, client_id=client_id, po_id=po_id, project_id=project_id,
        actor_uid=user.firebase_uid)
    persisted = [o for o in result.outcomes
                 if o.invoice_id is not None and o.status != "DUPLICATE"]
    dups = [o for o in result.outcomes if o.status == "DUPLICATE"]
    if not persisted and dups:
        response.status_code = status.HTTP_409_CONFLICT
    for o in result.outcomes:
        o.filename = filename_by_file.get(o.file_id, "")
    return UploadOut(
        batch_id=result.batch.id,
        invoice_count=result.batch.file_count,
        outcomes=[FileOutcomeOut.model_validate(o) for o in result.outcomes],
    )


# ------------------------------------------------------------------- register

@router.get("/billing/invoices", response_model=list[InvoiceOut])
def list_invoices(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=300)] = None,
    status_filter: Annotated[SalesInvoiceStatus | None, Query(alias="status")] = None,
    client_id: Annotated[int | None, Query()] = None,
    po_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SalesInvoice]:
    """The searchable client-invoice register (VIEW)."""
    _require_module(user)
    return service.list_invoices(
        db, q=q,
        status=status_filter.value if status_filter is not None else None,
        client_id=client_id, po_id=po_id,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


@router.get("/billing/invoices/{invoice_id}", response_model=InvoiceDetailOut)
def get_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """One invoice: header + fields + lines with their PO-match state (VIEW)."""
    _require_module(user)
    return _get_invoice(db, invoice_id)


# ------------------------------------------------------------------- review

@router.patch("/billing/invoices/{invoice_id}/review", response_model=InvoiceDetailOut)
def submit_review(
    invoice_id: int,
    body: ReviewPatchBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Apply human corrections and/or confirm the invoice (OPERATE). Corrections run first,
    then — if `confirm` is set — the invoice is frozen to CONFIRMED (blocked while a required
    field is weak or any line is unmatched)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
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
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


# ------------------------------------------------------------------- project attribution

@router.patch("/billing/invoices/{invoice_id}/project", response_model=InvoiceDetailOut)
def set_project(
    invoice_id: int,
    body: SetProjectBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Assign / change / clear the direct project attribution on an in-review invoice (OPERATE).
    ``project_id`` must be an ACTIVE project of this invoice's client (or null to clear); a
    CONFIRMED invoice's attribution is immutable (409)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.set_project(db, invoice, body.project_id, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


# ------------------------------------------------------------------- match

@router.post("/billing/invoices/{invoice_id}/match", response_model=InvoiceDetailOut)
def run_match(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Re-run the auto-matcher over the invoice's still-unmatched lines (OPERATE)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.run_match(db, invoice, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


@router.patch(
    "/billing/invoices/{invoice_id}/lines/{line_id}/match",
    response_model=InvoiceDetailOut,
)
def manual_match(
    invoice_id: int,
    line_id: int,
    body: ManualMatchBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Manually map one invoice line to a PO line item (OPERATE, status → MANUAL)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.apply_manual_match(
            db, invoice, line_id, body.po_line_item_id, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


# ------------------------------------------------------------------- manual line editing
# NOTE: the `/lines/{line_id}/match` route above is a DISTINCT path from the bare
# `/lines/{line_id}` below (no collision) — it is registered first to be safe.

@router.post("/billing/invoices/{invoice_id}/lines", response_model=InvoiceDetailOut)
def add_line(
    invoice_id: int,
    body: LineWriteBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Add a manual line item to an in-review invoice (OPERATE). A line needs a description;
    the status is re-derived (MATCHED for a PO-less invoice, NEEDS_MATCH for a PO-linked one).
    """
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.add_line(db, invoice, body.model_dump(), actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


@router.patch(
    "/billing/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceDetailOut
)
def update_line(
    invoice_id: int,
    line_id: int,
    body: LineWriteBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Edit an existing invoice line (OPERATE, partial update — only supplied fields change).
    Does not touch the line's PO-match state; the status is re-derived."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.update_line(
            db, invoice, line_id, body.model_dump(exclude_unset=True),
            actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


@router.delete(
    "/billing/invoices/{invoice_id}/lines/{line_id}", response_model=InvoiceDetailOut
)
def delete_line(
    invoice_id: int,
    line_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Delete one invoice line (OPERATE). Remaining lines keep their line_no; deleting the
    last line drops the invoice back to EXTRACTED. The status is re-derived."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.delete_line(db, invoice, line_id, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


# ------------------------------------------------------------------- cancel / delete

@router.post("/billing/invoices/{invoice_id}/cancel", response_model=InvoiceDetailOut)
def cancel_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> SalesInvoice:
    """Soft-cancel an in-review invoice (MANAGE). A CONFIRMED record is immutable (409)."""
    rbac.require_level(user, rbac.BILLING, Level.MANAGE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.cancel_invoice(db, invoice, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(invoice)
    return invoice


@router.delete("/billing/invoices/{invoice_id}", response_model=DeleteOut)
def delete_invoice(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DeleteOut:
    """Delete an invoice + its source blob (MANAGE). Supports delete-and-re-upload."""
    rbac.require_level(user, rbac.BILLING, Level.MANAGE)
    invoice = _get_invoice(db, invoice_id)
    try:
        service.delete_invoice(db, invoice, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    return DeleteOut(id=invoice_id, deleted=True)
