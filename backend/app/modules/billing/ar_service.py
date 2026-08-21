"""Accounts-Receivable service — payments, advances, advance application, and the
AR tracker (per-invoice outstanding / derived status / aging).

Money is INTEGER PAISE everywhere; no float ever touches a rupee amount.

The single money identity every read derives from:

    outstanding = grand_total
                  − Σ credit_note.grand_total (this invoice)
                  − Σ payment_receipt.amount   (this invoice)
                  − Σ advance_application.amount(this invoice)

Derived AR status:
    PAID       if outstanding <= 0
    PART_PAID  if 0 < settled  < total          (settled = total − outstanding)
    UNPAID     if nothing has been settled

`overdue` is outstanding > 0 AND due_date < today.

An advance's remaining = amount − Σ its applications. Applying an advance is guarded
on BOTH sides: never more than the advance's remaining, never more than the invoice's
outstanding (each raises a typed 422). Application is a FIFO-hybrid: `suggest_application`
proposes oldest-advance-first up to the invoice's outstanding, and the operator confirms
(or overrides) with an explicit `apply_advance`.

Every mutation is audited inside the caller's transaction (commits atomically).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.modules.billing.models import (
    AdvanceApplication,
    ClientAdvance,
    CreditNote,
    CreditNoteStatus,
    PaymentReceipt,
    SalesInvoice,
)
from app.modules.projects.models import ProjectClient
from app.platform import audit

# Derived AR status values (distinct from the invoice LIFECYCLE status).
STATUS_PAID = "PAID"
STATUS_PART_PAID = "PART_PAID"
STATUS_UNPAID = "UNPAID"


# --------------------------------------------------------------------- errors

class ARError(Exception):
    """Base for AR errors."""


class ClientNotFound(ARError):
    """The referenced client does not exist (route -> 404)."""


class InvoiceNotFound(ARError):
    """The referenced invoice does not exist (route -> 404)."""


class AdvanceNotFound(ARError):
    """The referenced advance does not exist (route -> 404)."""


class ApplicationNotFound(ARError):
    """The referenced advance application does not exist (route -> 404)."""


class InvalidAmount(ARError):
    """A money amount is missing/non-positive (route -> 422)."""


class ClientMismatch(ARError):
    """An advance and the target invoice belong to different clients (route -> 422)."""


class OverRemaining(ARError):
    """Applying more than the advance's remaining balance (route -> 422)."""


class OverOutstanding(ARError):
    """Applying/paying more than the invoice's outstanding balance (route -> 422)."""


# -------------------------------------------------------------- AR read model

@dataclass(frozen=True)
class InvoiceAR:
    """The computed AR state of one invoice."""

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


@dataclass(frozen=True)
class AdvanceState:
    """A client advance with its remaining (unapplied) balance."""

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


@dataclass(frozen=True)
class SuggestedApplication:
    """One proposed advance→invoice application (operator confirms/overrides)."""

    advance_id: int
    amount_paise: int
    advance_remaining_paise: int
    received_on: date
    reference: str | None


# ----------------------------------------------------------------- internals

def _sum(db: Session, column: Any, *conditions: ColumnElement[bool]) -> int:
    """Σ of `column` over rows matching `conditions`, coalesced to 0 (never None).

    `column` is a mapped money attribute (e.g. `PaymentReceipt.amount_paise`); typed
    `Any` because SQLAlchemy's InstrumentedAttribute doesn't unify with ColumnElement.
    """
    total = db.execute(
        select(func.coalesce(func.sum(column), 0)).where(*conditions)
    ).scalar_one()
    return int(total or 0)


def _grand_total(invoice: SalesInvoice) -> int:
    """The invoice grand total in paise (a not-yet-priced upload is treated as 0)."""
    return int(invoice.grand_total_paise or 0)


def credited_paise(db: Session, invoice_id: int) -> int:
    """Σ CONFIRMED credit-note grand totals raised against this invoice.

    Only a CONFIRMED credit note reduces receivables — a draft/uploaded/rejected CN must
    not (mirrors the finance P&L, which counts CONFIRMED docs only)."""
    return _sum(
        db,
        CreditNote.grand_total_paise,
        CreditNote.invoice_id == invoice_id,
        CreditNote.status == CreditNoteStatus.CONFIRMED.value,
    )


def paid_paise(db: Session, invoice_id: int) -> int:
    """Σ payment receipts recorded against this invoice."""
    return _sum(db, PaymentReceipt.amount_paise, PaymentReceipt.invoice_id == invoice_id)


def applied_paise(db: Session, invoice_id: int) -> int:
    """Σ advance applications posted against this invoice."""
    return _sum(
        db, AdvanceApplication.amount_paise, AdvanceApplication.invoice_id == invoice_id
    )


def outstanding_paise(db: Session, invoice: SalesInvoice) -> int:
    """The invoice's outstanding balance (may be negative if over-settled)."""
    return (
        _grand_total(invoice)
        - credited_paise(db, invoice.id)
        - paid_paise(db, invoice.id)
        - applied_paise(db, invoice.id)
    )


def _aging_bucket(due_date: date | None, outstanding: int, *, today: date) -> str | None:
    """Aging bucket of the outstanding balance, keyed on days past `due_date`.

    A settled invoice (outstanding <= 0) or one with no due date has no bucket. A
    not-yet-due / <=30-days-past-due balance sits in "0-30" (only four buckets exist).
    """
    if outstanding <= 0 or due_date is None:
        return None
    days_overdue = (today - due_date).days
    if days_overdue <= 30:
        return "0-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


# --------------------------------------------------------------- AR read API

def invoice_ar(db: Session, invoice: SalesInvoice, *, today: date | None = None) -> InvoiceAR:
    """Compute the full AR state of one invoice (outstanding/paid/status/overdue/aging)."""
    today = today or date.today()
    total = _grand_total(invoice)
    credited = credited_paise(db, invoice.id)
    paid = paid_paise(db, invoice.id)
    applied = applied_paise(db, invoice.id)
    outstanding = total - credited - paid - applied
    settled = credited + paid + applied

    if outstanding <= 0:
        status = STATUS_PAID
    elif settled > 0:
        status = STATUS_PART_PAID
    else:
        status = STATUS_UNPAID

    overdue = outstanding > 0 and invoice.due_date is not None and invoice.due_date < today

    return InvoiceAR(
        invoice_id=invoice.id,
        client_id=invoice.client_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        grand_total_paise=total,
        credited_paise=credited,
        paid_paise=paid,
        applied_paise=applied,
        outstanding_paise=outstanding,
        status=status,
        overdue=overdue,
        aging_bucket=_aging_bucket(invoice.due_date, outstanding, today=today),
    )


def get_invoice(db: Session, invoice_id: int) -> SalesInvoice | None:
    """Fetch one invoice by id, or None."""
    return db.execute(
        select(SalesInvoice).where(SalesInvoice.id == invoice_id)
    ).scalar_one_or_none()


def invoice_payments(db: Session, invoice_id: int) -> list[PaymentReceipt]:
    """Payment receipts against an invoice, newest-received first."""
    return list(
        db.execute(
            select(PaymentReceipt)
            .where(PaymentReceipt.invoice_id == invoice_id)
            .order_by(PaymentReceipt.received_on.desc(), PaymentReceipt.id.desc())
        ).scalars()
    )


def invoice_applications(db: Session, invoice_id: int) -> list[AdvanceApplication]:
    """Advance applications posted against an invoice, newest first."""
    return list(
        db.execute(
            select(AdvanceApplication)
            .where(AdvanceApplication.invoice_id == invoice_id)
            .order_by(AdvanceApplication.id.desc())
        ).scalars()
    )


def ar_register(
    db: Session,
    *,
    client_id: int | None = None,
    status: str | None = None,
    overdue: bool | None = None,
    today: date | None = None,
) -> list[InvoiceAR]:
    """The AR tracker: every invoice with its computed AR state, filtered by client,
    derived status (PAID/PART_PAID/UNPAID) and/or overdue. Ordered oldest-due first
    (nulls last) so the most-aged receivables surface at the top."""
    today = today or date.today()
    stmt = select(SalesInvoice)
    if client_id is not None:
        stmt = stmt.where(SalesInvoice.client_id == client_id)
    stmt = stmt.order_by(
        SalesInvoice.due_date.is_(None), SalesInvoice.due_date.asc(), SalesInvoice.id.asc()
    )
    rows = [invoice_ar(db, inv, today=today) for inv in db.execute(stmt).scalars()]
    if status is not None:
        rows = [r for r in rows if r.status == status]
    if overdue is not None:
        rows = [r for r in rows if r.overdue == overdue]
    return rows


# ------------------------------------------------------------- advances read

def advance_remaining(db: Session, advance: ClientAdvance) -> int:
    """The advance's unapplied balance = amount − Σ its applications."""
    applied = _sum(
        db, AdvanceApplication.amount_paise, AdvanceApplication.advance_id == advance.id
    )
    return int(advance.amount_paise) - applied


def get_advance(db: Session, advance_id: int) -> ClientAdvance | None:
    """Fetch one advance by id, or None."""
    return db.execute(
        select(ClientAdvance).where(ClientAdvance.id == advance_id)
    ).scalar_one_or_none()


def _advance_state(db: Session, advance: ClientAdvance) -> AdvanceState:
    applied = _sum(
        db, AdvanceApplication.amount_paise, AdvanceApplication.advance_id == advance.id
    )
    amount = int(advance.amount_paise)
    return AdvanceState(
        advance_id=advance.id,
        client_id=advance.client_id,
        po_id=advance.po_id,
        amount_paise=amount,
        applied_paise=applied,
        remaining_paise=amount - applied,
        received_on=advance.received_on,
        mode=advance.mode,
        reference=advance.reference,
        note=advance.note,
    )


def client_advances(db: Session, client_id: int | None = None) -> list[AdvanceState]:
    """List advances (optionally for one client) with each one's remaining balance,
    oldest-received first (FIFO order)."""
    stmt = select(ClientAdvance)
    if client_id is not None:
        stmt = stmt.where(ClientAdvance.client_id == client_id)
    stmt = stmt.order_by(ClientAdvance.received_on.asc(), ClientAdvance.id.asc())
    return [_advance_state(db, adv) for adv in db.execute(stmt).scalars()]


def suggest_application(
    db: Session, invoice: SalesInvoice
) -> list[SuggestedApplication]:
    """FIFO-hybrid proposal: draw the invoice's outstanding from this client's advances
    oldest-first, one proposal per advance, stopping once the outstanding is covered.
    The operator accepts these or picks manually via `apply_advance`."""
    outstanding = outstanding_paise(db, invoice)
    if outstanding <= 0:
        return []
    proposals: list[SuggestedApplication] = []
    remaining_need = outstanding
    advances = db.execute(
        select(ClientAdvance)
        .where(ClientAdvance.client_id == invoice.client_id)
        .order_by(ClientAdvance.received_on.asc(), ClientAdvance.id.asc())
    ).scalars()
    for adv in advances:
        if remaining_need <= 0:
            break
        adv_remaining = advance_remaining(db, adv)
        if adv_remaining <= 0:
            continue
        take = min(adv_remaining, remaining_need)
        proposals.append(
            SuggestedApplication(
                advance_id=adv.id,
                amount_paise=take,
                advance_remaining_paise=adv_remaining,
                received_on=adv.received_on,
                reference=adv.reference,
            )
        )
        remaining_need -= take
    return proposals


# ----------------------------------------------------------------- mutations

def record_payment(
    db: Session,
    *,
    invoice_id: int,
    amount_paise: int,
    received_on: date | None = None,
    mode: str | None = None,
    reference: str | None = None,
    note: str | None = None,
    actor_uid: str | None = None,
) -> PaymentReceipt:
    """Record a payment received against an invoice. Amount must be positive and must
    not exceed the invoice's current outstanding (over-payment is an advance, not a
    payment). `client_id` is derived from the invoice. Audited."""
    if amount_paise <= 0:
        raise InvalidAmount("payment amount must be a positive number of paise")
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise InvoiceNotFound(f"invoice {invoice_id} not found")
    outstanding = outstanding_paise(db, invoice)
    if amount_paise > outstanding:
        raise OverOutstanding(
            f"payment {amount_paise} exceeds invoice outstanding {outstanding}"
        )
    row = PaymentReceipt(
        client_id=invoice.client_id,
        invoice_id=invoice.id,
        amount_paise=amount_paise,
        received_on=received_on or date.today(),
        mode=mode,
        reference=reference,
        note=note,
        created_by=actor_uid,
    )
    db.add(row)
    db.flush()
    audit.log(
        db,
        action="billing.payment_recorded",
        actor_uid=actor_uid,
        entity="billing_payment",
        entity_id=str(row.id),
        detail={
            "invoice_id": invoice.id,
            "client_id": invoice.client_id,
            "amount_paise": amount_paise,
        },
    )
    return row


def record_advance(
    db: Session,
    *,
    client_id: int,
    amount_paise: int,
    received_on: date | None = None,
    po_id: int | None = None,
    mode: str | None = None,
    reference: str | None = None,
    note: str | None = None,
    actor_uid: str | None = None,
) -> ClientAdvance:
    """Record a client advance/deposit (offsets future invoices via applications).
    Amount must be positive; the client must exist. Audited."""
    if amount_paise <= 0:
        raise InvalidAmount("advance amount must be a positive number of paise")
    client = db.execute(
        select(ProjectClient).where(ProjectClient.id == client_id)
    ).scalar_one_or_none()
    if client is None:
        raise ClientNotFound(f"client {client_id} not found")
    row = ClientAdvance(
        client_id=client_id,
        po_id=po_id,
        amount_paise=amount_paise,
        received_on=received_on or date.today(),
        mode=mode,
        reference=reference,
        note=note,
        created_by=actor_uid,
    )
    db.add(row)
    db.flush()
    audit.log(
        db,
        action="billing.advance_recorded",
        actor_uid=actor_uid,
        entity="billing_advance",
        entity_id=str(row.id),
        detail={"client_id": client_id, "amount_paise": amount_paise},
    )
    return row


def apply_advance(
    db: Session,
    *,
    advance_id: int,
    invoice_id: int,
    amount_paise: int,
    actor_uid: str | None = None,
) -> AdvanceApplication:
    """Apply part of an advance to an invoice. Guards BOTH sides: the amount may not
    exceed the advance's remaining, nor the invoice's outstanding. The advance and the
    invoice must belong to the same client. Audited."""
    if amount_paise <= 0:
        raise InvalidAmount("application amount must be a positive number of paise")
    advance = get_advance(db, advance_id)
    if advance is None:
        raise AdvanceNotFound(f"advance {advance_id} not found")
    invoice = get_invoice(db, invoice_id)
    if invoice is None:
        raise InvoiceNotFound(f"invoice {invoice_id} not found")
    if advance.client_id != invoice.client_id:
        raise ClientMismatch(
            "advance and invoice belong to different clients — cannot apply"
        )
    remaining = advance_remaining(db, advance)
    if amount_paise > remaining:
        raise OverRemaining(
            f"cannot apply {amount_paise}: advance has only {remaining} remaining"
        )
    outstanding = outstanding_paise(db, invoice)
    if amount_paise > outstanding:
        raise OverOutstanding(
            f"cannot apply {amount_paise}: invoice outstanding is only {outstanding}"
        )
    row = AdvanceApplication(
        advance_id=advance.id,
        invoice_id=invoice.id,
        amount_paise=amount_paise,
        created_by=actor_uid,
    )
    db.add(row)
    db.flush()
    audit.log(
        db,
        action="billing.advance_applied",
        actor_uid=actor_uid,
        entity="billing_advance_application",
        entity_id=str(row.id),
        detail={
            "advance_id": advance.id,
            "invoice_id": invoice.id,
            "amount_paise": amount_paise,
        },
    )
    return row


def unapply_advance(
    db: Session, *, application_id: int, actor_uid: str | None = None
) -> None:
    """Reverse an advance application (frees the advance's remaining + re-opens the
    invoice's outstanding). Hard-deletes the application row. Audited."""
    row = db.execute(
        select(AdvanceApplication).where(AdvanceApplication.id == application_id)
    ).scalar_one_or_none()
    if row is None:
        raise ApplicationNotFound(f"advance application {application_id} not found")
    detail = {
        "advance_id": row.advance_id,
        "invoice_id": row.invoice_id,
        "amount_paise": int(row.amount_paise),
    }
    db.delete(row)
    db.flush()
    audit.log(
        db,
        action="billing.advance_unapplied",
        actor_uid=actor_uid,
        entity="billing_advance_application",
        entity_id=str(application_id),
        detail=detail,
    )
