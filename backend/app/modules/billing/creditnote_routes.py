"""Client credit-note capture HTTP surface (mounted at `/api/v1`, module key `billing`).

  POST   /billing/credit-notes                          -> upload a CN PDF against an invoice
  GET    /billing/credit-notes[?status&client_id&invoice_id] -> register (VIEW)
  GET    /billing/credit-notes/{id}                      -> header + lines + refs (VIEW)
  PATCH  /billing/credit-notes/{id}/review               -> corrections and/or confirm (OPERATE)
  POST   /billing/credit-notes/{id}/match                -> re-run the auto-matcher (OPERATE)
  PATCH  /billing/credit-notes/{id}/lines/{lineId}/match -> manual map to a PO line (OPERATE)
  POST   /billing/credit-notes/{id}/cancel               -> soft-cancel (MANAGE)
  DELETE /billing/credit-notes/{id}                      -> delete (delete-and-re-upload) (MANAGE)

RBAC (`billing` module): VIEW to read, `creditnote.upload` (OPERATE) to upload/match/review,
`invoice.cancel` / `invoice.delete` (MANAGE) to cancel/delete. Every mutation is audited in the
service layer. A confirmed credit note drives the §6 invoiced-qty write-back.
"""
from __future__ import annotations

import re
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
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.billing import creditnote_service as service
from app.modules.billing.models import CreditNote, CreditNoteStatus, SalesInvoice
from app.modules.files.models import StoredFile
from app.modules.sales_orders.models import POLineItem, Product
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.storage import get_storage

router = APIRouter()

MODULE_KEY = "billing"
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_filename(name: str) -> str:
    return _CONTROL.sub("", name).strip()[:512] or "credit-note.pdf"


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


def _get_cn(db: Session, cn_id: int) -> CreditNote:
    try:
        return service.get_credit_note(db, cn_id)
    except service.BillingNotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credit note not found") from err


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

class OutcomeOut(BaseModel):
    file_id: int
    status: str
    cn_id: int | None
    cn_number: str | None


class UploadOut(BaseModel):
    outcomes: list[OutcomeOut]


class CNOut(BaseModel):
    """One register row (nested — the FE consumes these fields exactly)."""

    id: int
    cn_number: str
    client_id: int
    invoice_id: int
    invoice_number: str | None
    cn_date: Any
    grand_total_paise: int | None
    status: str


class SourceFileOut(BaseModel):
    id: int
    filename: str


class ReferencedInvoiceOut(BaseModel):
    id: int
    invoice_number: str | None
    po_id: int | None
    grand_total_paise: int | None
    status: str


class CNLineOut(BaseModel):
    id: int
    line_no: int
    description: str | None
    quantity: str | None
    taxable_paise: int | None
    line_total_paise: int | None
    po_line_item_id: int | None
    po_line_label: str | None
    match_status: str


class CNDetailOut(BaseModel):
    id: int
    cn_number: str
    client_id: int
    invoice_id: int
    cn_date: Any
    total_taxable_paise: int | None
    total_cgst_paise: int | None
    total_sgst_paise: int | None
    total_igst_paise: int | None
    round_off_paise: int | None
    grand_total_paise: int | None
    status: str
    reason: str | None
    source_file: SourceFileOut | None
    referenced_invoice: ReferencedInvoiceOut | None
    lines: list[CNLineOut]


class CorrectionItem(BaseModel):
    field: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=600)


class ReviewPatchBody(BaseModel):
    corrections: list[CorrectionItem] = Field(default_factory=list, max_length=100)
    confirm: bool = False


class ManualMatchBody(BaseModel):
    po_line_item_id: int


class DeleteOut(BaseModel):
    id: int
    deleted: bool


# ------------------------------------------------------------------- assembly

def _po_line_labels(db: Session, po_line_ids: set[int]) -> dict[int, str]:
    """Resolve each matched PO line to a "product name — line description" label."""
    if not po_line_ids:
        return {}
    rows = db.execute(
        select(POLineItem.id, Product.name, POLineItem.description)
        .join(Product, Product.id == POLineItem.product_id)
        .where(POLineItem.id.in_(po_line_ids))
    ).all()
    return {pid: f"{name} — {desc}" for pid, name, desc in rows}


def _line_out(line: Any, labels: dict[int, str]) -> CNLineOut:
    return CNLineOut(
        id=line.id,
        line_no=line.line_no,
        description=line.description,
        quantity=None if line.quantity is None else str(line.quantity),
        taxable_paise=line.taxable_paise,
        line_total_paise=line.line_total_paise,
        po_line_item_id=line.po_line_item_id,
        po_line_label=(labels.get(line.po_line_item_id)
                       if line.po_line_item_id is not None else None),
        match_status=line.match_status,
    )


def _detail_out(db: Session, cn: CreditNote) -> CNDetailOut:
    invoice = db.get(SalesInvoice, cn.invoice_id)
    referenced = None if invoice is None else ReferencedInvoiceOut(
        id=invoice.id, invoice_number=invoice.invoice_number, po_id=invoice.po_id,
        grand_total_paise=invoice.grand_total_paise, status=invoice.status)
    source = None
    if cn.source_file_id is not None:
        sf = db.get(StoredFile, cn.source_file_id)
        if sf is not None:
            source = SourceFileOut(id=sf.id, filename=sf.filename)
    labels = _po_line_labels(
        db, {ln.po_line_item_id for ln in cn.lines if ln.po_line_item_id is not None})
    return CNDetailOut(
        id=cn.id, cn_number=cn.cn_number, client_id=cn.client_id, invoice_id=cn.invoice_id,
        cn_date=cn.cn_date, total_taxable_paise=cn.total_taxable_paise,
        total_cgst_paise=cn.total_cgst_paise, total_sgst_paise=cn.total_sgst_paise,
        total_igst_paise=cn.total_igst_paise, round_off_paise=cn.round_off_paise,
        grand_total_paise=cn.grand_total_paise, status=cn.status, reason=cn.reason,
        source_file=source, referenced_invoice=referenced,
        lines=[_line_out(ln, labels) for ln in sorted(cn.lines, key=lambda x: x.line_no)],
    )


# ------------------------------------------------------------------- upload

@router.post("/billing/credit-notes", response_model=UploadOut, status_code=201)
def upload_credit_note(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
    file: Annotated[UploadFile, File()],
    invoice_id: Annotated[int, Form()],
) -> UploadOut:
    """Upload a client credit-note PDF against an existing invoice: store the blob, then extract
    + match + persist. The credited invoice must exist (the CN's client is taken from it).

    A document whose identity collides a stored CN comes back as DUPLICATE; when the request is
    wholly duplicates the HTTP status is 409, otherwise 201."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    invoice = db.get(SalesInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "credited invoice not found")

    max_bytes = get_settings().max_upload_bytes
    storage = get_storage()
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):  # cap BEFORE appending
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)
    filename = _sanitize_filename(file.filename or "credit-note.pdf")
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix
    ref = storage.save(f"sales-cn-source/{uuid4().hex}{suffix}", bytes(data))
    sf = StoredFile(
        kind="sales-cn-source", filename=filename, content_type=file.content_type,
        size=len(data), storage_ref=ref, uploaded_by=user.firebase_uid, module_key=MODULE_KEY)
    db.add(sf)
    db.flush()

    outcomes = service.upload_credit_notes(
        db, [sf.id], invoice_id=invoice_id, client_id=invoice.client_id,
        actor_uid=user.firebase_uid)
    persisted = [o for o in outcomes if o.cn_id is not None and o.status != "DUPLICATE"]
    dups = [o for o in outcomes if o.status == "DUPLICATE"]
    if not persisted and dups:
        response.status_code = status.HTTP_409_CONFLICT
    return UploadOut(outcomes=[
        OutcomeOut(file_id=o.file_id, status=o.status, cn_id=o.cn_id, cn_number=o.cn_number)
        for o in outcomes
    ])


# ------------------------------------------------------------------- register / read

@router.get("/billing/credit-notes", response_model=list[CNOut])
def list_credit_notes(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[CreditNoteStatus | None, Query(alias="status")] = None,
    client_id: Annotated[int | None, Query()] = None,
    invoice_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CNOut]:
    """The credit-note register (VIEW)."""
    _require_module(user)
    rows = service.list_credit_notes(
        db, status=status_filter.value if status_filter is not None else None,
        client_id=client_id, invoice_id=invoice_id, limit=limit, offset=offset)
    inv_ids = {cn.invoice_id for cn in rows}
    numbers: dict[int, str] = {}
    if inv_ids:
        for iid, num in db.execute(
            select(SalesInvoice.id, SalesInvoice.invoice_number)
            .where(SalesInvoice.id.in_(inv_ids))
        ).all():
            numbers[iid] = num
    return [
        CNOut(
            id=cn.id, cn_number=cn.cn_number, client_id=cn.client_id,
            invoice_id=cn.invoice_id, invoice_number=numbers.get(cn.invoice_id),
            cn_date=cn.cn_date, grand_total_paise=cn.grand_total_paise, status=cn.status)
        for cn in rows
    ]


@router.get("/billing/credit-notes/{cn_id}", response_model=CNDetailOut)
def get_credit_note(
    cn_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CNDetailOut:
    """One credit note: header + lines + the credited invoice + source file (VIEW)."""
    _require_module(user)
    return _detail_out(db, _get_cn(db, cn_id))


# ------------------------------------------------------------------- review

@router.patch("/billing/credit-notes/{cn_id}/review", response_model=CNDetailOut)
def submit_review(
    cn_id: int,
    body: ReviewPatchBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CNDetailOut:
    """Apply header corrections and/or confirm the credit note (OPERATE). Corrections run first,
    then — if `confirm` is set — the CN is frozen to CONFIRMED (blocked unless the credited
    invoice is CONFIRMED, every line is matched, and required fields are present)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    cn = _get_cn(db, cn_id)
    if not body.corrections and not body.confirm:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "supply corrections and/or confirm=true")
    try:
        if body.corrections:
            service.submit_cn_corrections(
                db, cn, [(c.field, c.value) for c in body.corrections],
                actor_uid=user.firebase_uid)
        if body.confirm:
            service.confirm_credit_note(db, cn, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(cn)
    return _detail_out(db, cn)


# ------------------------------------------------------------------- match

@router.post("/billing/credit-notes/{cn_id}/match", response_model=CNDetailOut)
def run_match(
    cn_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CNDetailOut:
    """Re-run the auto-matcher over the CN's still-unmatched lines (OPERATE)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    cn = _get_cn(db, cn_id)
    try:
        service.run_cn_match(db, cn, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(cn)
    return _detail_out(db, cn)


@router.patch(
    "/billing/credit-notes/{cn_id}/lines/{line_id}/match", response_model=CNDetailOut)
def manual_match(
    cn_id: int,
    line_id: int,
    body: ManualMatchBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CNDetailOut:
    """Manually map one CN line to a PO line item on the credited invoice's PO (OPERATE)."""
    rbac.require_level(user, rbac.BILLING, Level.OPERATE)
    cn = _get_cn(db, cn_id)
    try:
        service.apply_manual_cn_match(
            db, cn, line_id, body.po_line_item_id, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(cn)
    return _detail_out(db, cn)


# ------------------------------------------------------------------- cancel / delete

@router.post("/billing/credit-notes/{cn_id}/cancel", response_model=CNDetailOut)
def cancel_credit_note(
    cn_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> CNDetailOut:
    """Soft-cancel an in-review credit note (MANAGE). A CONFIRMED record is immutable (409)."""
    rbac.require_level(user, rbac.BILLING, Level.MANAGE)
    cn = _get_cn(db, cn_id)
    try:
        service.cancel_credit_note(db, cn, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    db.refresh(cn)
    return _detail_out(db, cn)


@router.delete("/billing/credit-notes/{cn_id}", response_model=DeleteOut)
def delete_credit_note(
    cn_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> DeleteOut:
    """Delete a credit note + its source blob (MANAGE). Supports delete-and-re-upload."""
    rbac.require_level(user, rbac.BILLING, Level.MANAGE)
    cn = _get_cn(db, cn_id)
    try:
        service.delete_credit_note(db, cn, actor_uid=user.firebase_uid)
    except service.BillingError as err:
        raise _map_service_error(err) from err
    return DeleteOut(id=cn_id, deleted=True)
