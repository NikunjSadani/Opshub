"""Accounts-Receivable operator surface (mounted at `/api/v1`).

  POST   /billing/payments                          -> record a payment (payment.record)
  GET    /billing/ar                                 -> AR tracker (VIEW; per-invoice
                                                        outstanding/status/aging + filters)
  GET    /billing/invoices/{id}/ar                   -> one invoice's AR detail (VIEW;
                                                        incl. its payments + applied advances)
  POST   /billing/advances                           -> record a client advance (advance.record)
  GET    /billing/advances?client_id=                -> advances with remaining (VIEW)
  GET    /billing/invoices/{id}/advance-suggestion   -> FIFO advance suggestion (VIEW)
  POST   /billing/advance-applications               -> apply an advance (advance.apply)
  DELETE /billing/advance-applications/{id}          -> reverse an application (advance.apply)

Reads need the `billing` module (VIEW). Records/applications need OPERATE via the
discrete actions payment.record / advance.record / advance.apply. Every write is
audited by the service.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.billing import ar_service
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User
from app.platform.rbac import can

router = APIRouter()


# ------------------------------------------------------------------- gating

def _require_view(user: User) -> None:
    rbac.require_module(user, rbac.BILLING)


def _require_action(user: User, action: str) -> None:
    if not can(user, action):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{action} required")


# --------------------------------------------------------------- error map

def _map_error(exc: ar_service.ARError) -> HTTPException:
    """Translate a typed AR error to the right HTTP status."""
    if isinstance(
        exc,
        ar_service.InvoiceNotFound
        | ar_service.AdvanceNotFound
        | ar_service.ApplicationNotFound
        | ar_service.ClientNotFound,
    ):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    # InvalidAmount / ClientMismatch / OverRemaining / OverOutstanding -> 422
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# ------------------------------------------------------------------- schemas

class PaymentIn(BaseModel):
    invoice_id: int
    amount_paise: int = Field(gt=0)
    received_on: date | None = None
    mode: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    client_id: int
    invoice_id: int
    amount_paise: int
    received_on: date
    mode: str | None
    reference: str | None
    note: str | None
    created_at: datetime


class AdvanceIn(BaseModel):
    client_id: int
    amount_paise: int = Field(gt=0)
    received_on: date | None = None
    po_id: int | None = None
    mode: str | None = Field(default=None, max_length=40)
    reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class AdvanceOut(BaseModel):
    advance_id: int
    client_id: int
    po_id: int | None
    amount_paise: int
    applied_paise: int
    remaining_paise: int
    received_on: date
    mode: str | None
    reference: str | None
    note: str | None


class ApplicationIn(BaseModel):
    advance_id: int
    invoice_id: int
    amount_paise: int = Field(gt=0)


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    advance_id: int
    invoice_id: int
    amount_paise: int
    created_at: datetime


class InvoiceAROut(BaseModel):
    invoice_id: int
    client_id: int
    invoice_number: str
    invoice_date: date | None
    due_date: date | None
    grand_total_paise: int
    credited_paise: int
    paid_paise: int
    applied_paise: int
    outstanding_paise: int
    status: str
    overdue: bool
    aging_bucket: str | None


class InvoiceARDetailOut(InvoiceAROut):
    payments: list[PaymentOut]
    applied_advances: list[ApplicationOut]


class SuggestionOut(BaseModel):
    advance_id: int
    amount_paise: int
    advance_remaining_paise: int
    received_on: date
    reference: str | None


def _ar_out(state: ar_service.InvoiceAR) -> InvoiceAROut:
    return InvoiceAROut(
        invoice_id=state.invoice_id,
        client_id=state.client_id,
        invoice_number=state.invoice_number,
        invoice_date=state.invoice_date,
        due_date=state.due_date,
        grand_total_paise=state.grand_total_paise,
        credited_paise=state.credited_paise,
        paid_paise=state.paid_paise,
        applied_paise=state.applied_paise,
        outstanding_paise=state.outstanding_paise,
        status=state.status,
        overdue=state.overdue,
        aging_bucket=state.aging_bucket,
    )


# -------------------------------------------------------------------- payments

@router.post("/billing/payments", response_model=PaymentOut, status_code=201)
def record_payment(
    body: PaymentIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PaymentOut:
    _require_action(user, "payment.record")
    try:
        row = ar_service.record_payment(
            db,
            invoice_id=body.invoice_id,
            amount_paise=body.amount_paise,
            received_on=body.received_on,
            mode=body.mode,
            reference=body.reference,
            note=body.note,
            actor_uid=user.firebase_uid,
        )
    except ar_service.ARError as exc:
        raise _map_error(exc) from exc
    db.commit()
    db.refresh(row)
    return PaymentOut.model_validate(row)


# --------------------------------------------------------------- AR tracker

@router.get("/billing/ar", response_model=list[InvoiceAROut])
def ar_tracker(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(PAID|PART_PAID|UNPAID)$")
    ] = None,
    overdue: Annotated[bool | None, Query()] = None,
) -> list[InvoiceAROut]:
    _require_view(user)
    rows = ar_service.ar_register(
        db, client_id=client_id, status=status_filter, overdue=overdue
    )
    return [_ar_out(r) for r in rows]


@router.get("/billing/invoices/{invoice_id}/ar", response_model=InvoiceARDetailOut)
def invoice_ar_detail(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> InvoiceARDetailOut:
    _require_view(user)
    invoice = ar_service.get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")
    state = ar_service.invoice_ar(db, invoice)
    base = _ar_out(state)
    return InvoiceARDetailOut(
        **base.model_dump(),
        payments=[
            PaymentOut.model_validate(p)
            for p in ar_service.invoice_payments(db, invoice_id)
        ],
        applied_advances=[
            ApplicationOut.model_validate(a)
            for a in ar_service.invoice_applications(db, invoice_id)
        ],
    )


# -------------------------------------------------------------------- advances

@router.post("/billing/advances", response_model=AdvanceOut, status_code=201)
def record_advance(
    body: AdvanceIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> AdvanceOut:
    _require_action(user, "advance.record")
    try:
        row = ar_service.record_advance(
            db,
            client_id=body.client_id,
            amount_paise=body.amount_paise,
            received_on=body.received_on,
            po_id=body.po_id,
            mode=body.mode,
            reference=body.reference,
            note=body.note,
            actor_uid=user.firebase_uid,
        )
    except ar_service.ARError as exc:
        raise _map_error(exc) from exc
    db.commit()
    db.refresh(row)
    # amount just recorded, nothing applied yet
    return AdvanceOut(
        advance_id=row.id,
        client_id=row.client_id,
        po_id=row.po_id,
        amount_paise=int(row.amount_paise),
        applied_paise=0,
        remaining_paise=int(row.amount_paise),
        received_on=row.received_on,
        mode=row.mode,
        reference=row.reference,
        note=row.note,
    )


@router.get("/billing/advances", response_model=list[AdvanceOut])
def list_advances(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
) -> list[AdvanceOut]:
    _require_view(user)
    return [
        AdvanceOut(
            advance_id=s.advance_id,
            client_id=s.client_id,
            po_id=s.po_id,
            amount_paise=s.amount_paise,
            applied_paise=s.applied_paise,
            remaining_paise=s.remaining_paise,
            received_on=s.received_on,
            mode=s.mode,
            reference=s.reference,
            note=s.note,
        )
        for s in ar_service.client_advances(db, client_id)
    ]


@router.get(
    "/billing/invoices/{invoice_id}/advance-suggestion",
    response_model=list[SuggestionOut],
)
def advance_suggestion(
    invoice_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[SuggestionOut]:
    _require_view(user)
    invoice = ar_service.get_invoice(db, invoice_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invoice not found")
    return [
        SuggestionOut(
            advance_id=s.advance_id,
            amount_paise=s.amount_paise,
            advance_remaining_paise=s.advance_remaining_paise,
            received_on=s.received_on,
            reference=s.reference,
        )
        for s in ar_service.suggest_application(db, invoice)
    ]


# ----------------------------------------------------------- applications

@router.post(
    "/billing/advance-applications", response_model=ApplicationOut, status_code=201
)
def apply_advance(
    body: ApplicationIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ApplicationOut:
    _require_action(user, "advance.apply")
    try:
        row = ar_service.apply_advance(
            db,
            advance_id=body.advance_id,
            invoice_id=body.invoice_id,
            amount_paise=body.amount_paise,
            actor_uid=user.firebase_uid,
        )
    except ar_service.ARError as exc:
        raise _map_error(exc) from exc
    db.commit()
    db.refresh(row)
    return ApplicationOut.model_validate(row)


@router.delete("/billing/advance-applications/{application_id}")
def unapply_advance(
    application_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    _require_action(user, "advance.apply")
    try:
        ar_service.unapply_advance(
            db, application_id=application_id, actor_uid=user.firebase_uid
        )
    except ar_service.ARError as exc:
        raise _map_error(exc) from exc
    db.commit()
    return {"ok": True}
