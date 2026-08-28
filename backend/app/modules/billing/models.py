"""Billing tables (prefixed `billing_`) — the OUTBOUND (sales) side.

Capture trio mirrors the expense pipeline (upload→extract→review→confirm) but with
SALES semantics (supplier=us, buyer=client) + a new MATCH stage tying each line to a
`po_line_item`, plus the AR tables (payments, advances, applications).

* `billing_batch`               — one upload run.
* `billing_invoice`             — one captured CLIENT invoice (number is the accounting
                                  software's; GST captured as printed, never derived).
                                  UNIQUE `dedup_key` keyed on the CLIENT (buyer) GSTIN.
* `billing_invoice_line`        — first-class lines; `po_line_item_id` ties a confirmed
                                  line to the PO (drives §6 per-line invoiced_qty). Money paise.
* `billing_invoice_field`       — per-field ENVELOPE (value/raw/confidence/status/provenance).
* `billing_invoice_correction`  — human-correction audit; feeds the eval gold set.
* `billing_credit_note` / `_line` — uploaded client credit notes (against an invoice).
* `billing_payment`             — a receipt against an invoice (AR).
* `billing_advance` / `_application` — client advances + their application to invoices.

Cross-module refs (project_client, purchase_order, po_line_item, files_stored_file) are
plain FK columns with NO ORM relationship — resolved by join (the module-boundary rule).

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import annotations`
(py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BillingBatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SalesInvoiceStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"          # blob stored, not yet extracted
    EXTRACTED = "EXTRACTED"        # extracted cleanly, no review flags
    NEEDS_REVIEW = "NEEDS_REVIEW"  # a required/low-confidence field needs a human
    NEEDS_OCR = "NEEDS_OCR"        # no text layer (scanned) → manual path
    NEEDS_MATCH = "NEEDS_MATCH"    # extracted but ≥1 line unmatched to a PO line
    MATCHED = "MATCHED"            # every line matched/mapped, ready to confirm
    CONFIRMED = "CONFIRMED"        # immutable; drives §6 invoiced_qty + AR
    REJECTED = "REJECTED"          # unreadable / quality-gate failure
    CANCELLED = "CANCELLED"        # explicitly cancelled (soft)


class LineMatchStatus(str, enum.Enum):
    UNMATCHED = "UNMATCHED"  # no PO line proposed/chosen yet
    MATCHED = "MATCHED"      # auto-matched to a PO line
    MANUAL = "MANUAL"        # operator mapped/overrode the PO line


class CreditNoteStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_OCR = "NEEDS_OCR"
    NEEDS_MATCH = "NEEDS_MATCH"
    MATCHED = "MATCHED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BillingBatch(Base):
    """One upload run (N client-invoice PDFs)."""

    __tablename__ = "billing_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=BillingBatchStatus.PENDING.value)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SalesInvoice(Base):
    """A captured client invoice (uploaded from accounting software)."""

    __tablename__ = "billing_invoice"
    __table_args__ = (
        # A client's invoice number is unique within that client (the number is the
        # accounting software's, so it's stable and non-null).
        UniqueConstraint("client_id", "invoice_number", name="uq_billing_invoice_client_number"),
        CheckConstraint(
            "status in ('UPLOADED','EXTRACTED','NEEDS_REVIEW','NEEDS_OCR','NEEDS_MATCH',"
            "'MATCHED','CONFIRMED','REJECTED','CANCELLED')",
            name="ck_billing_invoice_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("billing_batch.id"), index=True)
    # Cross-module FKs (no ORM relationship): the client + the PO this invoice bills.
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    # Optional DIRECT project attribution for a PO-less invoice (inc: PO-less P&L). When set,
    # finance resolves this invoice's revenue to this project as a FALLBACK behind the PO
    # (COALESCE(PurchaseOrder.project_id, SalesInvoice.project_id)) — a PO, when present, still
    # carries the project. Nullable + a plain FK (no cascade; projects are soft-managed): a
    # PO-less invoice with neither a PO nor a project is "unattributed" in the consolidated P&L.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"), index=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("files_stored_file.id"), index=True)

    invoice_number: Mapped[str] = mapped_column(String(120), index=True)  # accounting software's
    invoice_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)  # from client credit terms or entered
    # GSTINs captured as printed: supplier=us (self-check), buyer=client (dedup discriminator).
    supplier_gstin: Mapped[str | None] = mapped_column(String(15))
    buyer_gstin: Mapped[str | None] = mapped_column(String(15))
    # Money snapshots (as printed on the document; never derived). Paise, round_off SIGNED.
    total_taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    round_off_paise: Mapped[int | None] = mapped_column(BigInteger)
    grand_total_paise: Mapped[int | None] = mapped_column(BigInteger)

    source_engine: Mapped[str | None] = mapped_column(String(32))
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Identity (client GSTIN | invoice_number | date | grand_total) + raw-bytes hash.
    dedup_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    status: Mapped[str] = mapped_column(
        String(16), default=SalesInvoiceStatus.UPLOADED.value, index=True
    )
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["SalesInvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    fields: Mapped[list["SalesInvoiceField"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class SalesInvoiceLine(Base):
    """One line of a client invoice; ties to a PO line once matched/mapped."""

    __tablename__ = "billing_invoice_line"
    __table_args__ = (
        CheckConstraint(
            "match_status in ('UNMATCHED','MATCHED','MANUAL')", name="ck_billing_line_match"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("billing_invoice.id", ondelete="CASCADE"), index=True
    )
    # The matched PO line (cross-module FK, no ORM rel); NULL until matched/mapped.
    po_line_item_id: Mapped[int | None] = mapped_column(ForeignKey("po_line_item.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(500))
    hsn_sac: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    unit_rate_paise: Mapped[int | None] = mapped_column(BigInteger)
    taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    line_total_paise: Mapped[int | None] = mapped_column(BigInteger)
    match_status: Mapped[str] = mapped_column(String(12), default=LineMatchStatus.UNMATCHED.value)

    invoice: Mapped[SalesInvoice] = relationship(back_populates="lines")


class SalesInvoiceField(Base):
    """Per-field extraction envelope (value/raw/confidence/status/provenance) for review."""

    __tablename__ = "billing_invoice_field"
    __table_args__ = (
        UniqueConstraint("invoice_id", "field_path", name="uq_billing_invoice_field"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("billing_invoice.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(64))  # e.g. "header.invoice_number"
    value_raw: Mapped[str | None] = mapped_column(String(500))
    value_norm: Mapped[str | None] = mapped_column(String(500))  # normalized string form
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_engine: Mapped[str | None] = mapped_column(String(32))
    # OK / LOW_CONFIDENCE / MISSING / CORRECTED (mirrors the canonical FieldStatus).
    status: Mapped[str] = mapped_column(String(16), default="OK")

    invoice: Mapped[SalesInvoice] = relationship(back_populates="fields")


class SalesInvoiceCorrection(Base):
    """Audit trail of human corrections; `promoted_to_gold` feeds the eval gold set."""

    __tablename__ = "billing_invoice_correction"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("billing_invoice.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(String(500))
    new_value: Mapped[str | None] = mapped_column(String(500))
    promoted_to_gold: Mapped[bool] = mapped_column(Boolean, default=False)
    actor_uid: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CreditNote(Base):
    """A captured client credit note (uploaded), against a billing_invoice."""

    __tablename__ = "billing_credit_note"
    __table_args__ = (
        UniqueConstraint("client_id", "cn_number", name="uq_billing_cn_client_number"),
        CheckConstraint(
            "status in ('UPLOADED','EXTRACTED','NEEDS_REVIEW','NEEDS_OCR','NEEDS_MATCH',"
            "'MATCHED','CONFIRMED','REJECTED','CANCELLED')",
            name="ck_billing_cn_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("billing_invoice.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("files_stored_file.id"), index=True)
    cn_number: Mapped[str] = mapped_column(String(120), index=True)  # accounting software's
    cn_date: Mapped[date | None] = mapped_column(Date)
    total_taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    round_off_paise: Mapped[int | None] = mapped_column(BigInteger)
    grand_total_paise: Mapped[int | None] = mapped_column(BigInteger)
    source_engine: Mapped[str | None] = mapped_column(String(32))
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    dedup_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=CreditNoteStatus.UPLOADED.value)
    reason: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list["CreditNoteLine"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan"
    )
    fields: Mapped[list["CreditNoteField"]] = relationship(
        back_populates="credit_note", cascade="all, delete-orphan"
    )


class CreditNoteLine(Base):
    """One line of a client credit note; ties to a PO line to write its quantity back."""

    __tablename__ = "billing_credit_note_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    cn_id: Mapped[int] = mapped_column(
        ForeignKey("billing_credit_note.id", ondelete="CASCADE"), index=True
    )
    po_line_item_id: Mapped[int | None] = mapped_column(ForeignKey("po_line_item.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(500))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    line_total_paise: Mapped[int | None] = mapped_column(BigInteger)
    match_status: Mapped[str] = mapped_column(String(12), default=LineMatchStatus.UNMATCHED.value)

    credit_note: Mapped[CreditNote] = relationship(back_populates="lines")


class CreditNoteField(Base):
    """Per-field extraction envelope for a credit note (mirrors SalesInvoiceField) — drives
    the review field-editor's low-confidence / missing highlighting and CORRECTED status."""

    __tablename__ = "billing_credit_note_field"
    __table_args__ = (
        UniqueConstraint("cn_id", "field_path", name="uq_billing_cn_field"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cn_id: Mapped[int] = mapped_column(
        ForeignKey("billing_credit_note.id", ondelete="CASCADE"), index=True
    )
    field_path: Mapped[str] = mapped_column(String(64))  # the CN attr, e.g. "grand_total_paise"
    value_raw: Mapped[str | None] = mapped_column(String(500))
    value_norm: Mapped[str | None] = mapped_column(String(500))  # normalized string form
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source_engine: Mapped[str | None] = mapped_column(String(32))
    # OK / LOW_CONFIDENCE / MISSING / CORRECTED (mirrors the canonical FieldStatus).
    status: Mapped[str] = mapped_column(String(16), default="OK")

    credit_note: Mapped[CreditNote] = relationship(back_populates="fields")


class PaymentReceipt(Base):
    """A payment received against a client invoice (drives AR)."""

    __tablename__ = "billing_payment"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("billing_invoice.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    received_on: Mapped[date] = mapped_column(Date)
    mode: Mapped[str | None] = mapped_column(String(40))
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ClientAdvance(Base):
    """A client advance/deposit; offsets future invoices via advance applications."""

    __tablename__ = "billing_advance"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    received_on: Mapped[date] = mapped_column(Date)
    mode: Mapped[str | None] = mapped_column(String(40))
    reference: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    applications: Mapped[list["AdvanceApplication"]] = relationship(
        back_populates="advance", cascade="all, delete-orphan"
    )


class AdvanceApplication(Base):
    """Applies part of an advance to an invoice (advance remaining = amount − Σ applications)."""

    __tablename__ = "billing_advance_application"

    id: Mapped[int] = mapped_column(primary_key=True)
    advance_id: Mapped[int] = mapped_column(
        ForeignKey("billing_advance.id", ondelete="CASCADE"), index=True
    )
    invoice_id: Mapped[int] = mapped_column(ForeignKey("billing_invoice.id"), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    advance: Mapped[ClientAdvance] = relationship(back_populates="applications")
