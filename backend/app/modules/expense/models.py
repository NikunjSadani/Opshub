"""Expense/Invoice tables (prefixed `expense_`).

* `expense_batch`             — one upload run (N PDF files, one invoice each).
* `expense_invoice`           — one captured vendor invoice: snapshotted canonical
                                scalars (frozen once CONFIRMED), a UNIQUE `dedup_key`
                                (hard-block re-uploads; delete-and-re-upload to replace).
* `expense_invoice_line`      — first-class line items; money in BigInt paise.
* `expense_invoice_field`     — the per-field ENVELOPE (value/raw/confidence/status/
                                provenance), one row per field, for the review UI.
* `expense_invoice_correction`— audit trail of human corrections; feeds the eval gold set.

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import annotations`
(py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
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


class InvoiceBatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    EXTRACTING = "EXTRACTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InvoiceStatus(str, enum.Enum):
    UPLOADED = "UPLOADED"            # bytes stored, not yet extracted
    EXTRACTED = "EXTRACTED"          # engine ran, all required fields OK → auto-confirmable
    NEEDS_REVIEW = "NEEDS_REVIEW"    # extracted but review_needed (weak/missing/arithmetic)
    NEEDS_OCR = "NEEDS_OCR"          # no text layer, parked for the deferred OCR engine
    CONFIRMED = "CONFIRMED"          # a human accepted/corrected → immutable canonical record
    REJECTED = "REJECTED"            # quality-gate failure (terminal)


class FieldStatus(str, enum.Enum):
    OK = "OK"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    MISSING = "MISSING"
    CORRECTED = "CORRECTED"


_BATCH_STATUSES = ", ".join(f"'{s.value}'" for s in InvoiceBatchStatus)
_INVOICE_STATUSES = ", ".join(f"'{s.value}'" for s in InvoiceStatus)
_FIELD_STATUSES = ", ".join(f"'{s.value}'" for s in FieldStatus)


class InvoiceBatch(Base):
    __tablename__ = "expense_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(
        String(16), default=InvoiceBatchStatus.PENDING.value, index=True)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    invoices: Mapped[list["Invoice"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(f"status IN ({_BATCH_STATUSES})", name="ck_expense_batch_status"),
    )


class ExpensePaymentMethod(Base):
    """An admin-managed payment method (e.g. Bank Transfer, UPI, Cheque) that an
    invoice batch is tagged with at upload. Soft-deleted via `active` so historical
    invoices keep their method. Curated on the Expense module's Payment Methods tab
    (Manage level)."""

    __tablename__ = "expense_payment_method"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Invoice(Base):
    __tablename__ = "expense_invoice"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("expense_batch.id", ondelete="CASCADE"), index=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    schema_version: Mapped[str] = mapped_column(String(32), default="")
    source_engine: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(
        String(16), default=InvoiceStatus.UPLOADED.value, index=True)
    needs_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Dedup: HARD unique on the identity key (F2). `content_hash` is a belt-and-suspenders
    # fingerprint of the raw SOURCE bytes, catching a byte-identical re-upload even when
    # the identity key is NULL (an un-OCR'd scan) — see dedup.content_hash / service.F4.
    dedup_key: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # --- snapshotted canonical scalars (frozen once CONFIRMED) ---
    supplier_name: Mapped[str | None] = mapped_column(String(300))
    supplier_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    supplier_address: Mapped[str | None] = mapped_column(String(600))
    buyer_name: Mapped[str | None] = mapped_column(String(300))
    buyer_gstin: Mapped[str | None] = mapped_column(String(15))
    buyer_address: Mapped[str | None] = mapped_column(String(600))
    invoice_number: Mapped[str | None] = mapped_column(String(64))
    invoice_date: Mapped[date | None] = mapped_column(Date, index=True)
    place_of_supply: Mapped[str | None] = mapped_column(String(64))
    po_ref: Mapped[str | None] = mapped_column(String(64))

    # Cost allocation (inc 27) — set at upload, one value per batch. Nullable so pre-inc-27
    # rows survive; required by the upload endpoint + a guard blocks confirming without them.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("project.id"), index=True, default=None)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey("expense_payment_method.id"), index=True, default=None)
    # Same-module relationship is safe; the Project is resolved via explicit joins to keep
    # the expense/projects modules decoupled (no cross-module mapper dependency).
    payment_method: Mapped["ExpensePaymentMethod | None"] = relationship()

    total_taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    total_igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    round_off_paise: Mapped[int | None] = mapped_column(BigInteger)  # SIGNED
    grand_total_paise: Mapped[int | None] = mapped_column(BigInteger)
    amount_in_words: Mapped[str | None] = mapped_column(String(600))

    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    batch: Mapped["InvoiceBatch"] = relationship(back_populates="invoices")
    lines: Mapped[list["InvoiceLineItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan",
        order_by="InvoiceLineItem.line_no")
    fields: Mapped[list["InvoiceField"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan")

    __table_args__ = (
        # HARD dedup (F2): at most one live invoice per identity key. A re-upload with the
        # same key is rejected (409) → the operator deletes this row and re-uploads.
        UniqueConstraint("dedup_key", name="uq_expense_invoice_dedup_key"),
        CheckConstraint(f"status IN ({_INVOICE_STATUSES})", name="ck_expense_invoice_status"),
    )


class InvoiceLineItem(Base):
    __tablename__ = "expense_invoice_line"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("expense_invoice.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(600))
    hsn_sac: Mapped[str | None] = mapped_column(String(12))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    unit_rate_paise: Mapped[int | None] = mapped_column(BigInteger)
    taxable_paise: Mapped[int | None] = mapped_column(BigInteger)
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    cgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    sgst_paise: Mapped[int | None] = mapped_column(BigInteger)
    igst_paise: Mapped[int | None] = mapped_column(BigInteger)
    line_total_paise: Mapped[int | None] = mapped_column(BigInteger)

    invoice: Mapped["Invoice"] = relationship(back_populates="lines")


class InvoiceField(Base):
    """The per-field envelope — one row per canonical field, for the review UI + audit."""

    __tablename__ = "expense_invoice_field"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("expense_invoice.id", ondelete="CASCADE"), index=True)
    field_path: Mapped[str] = mapped_column(String(64))  # e.g. "header.supplier_gstin"
    value_normalized: Mapped[str | None] = mapped_column(String(600))  # paise→int-str, date→ISO
    value_raw: Mapped[str] = mapped_column(String(600), default="")
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0"))
    source_engine: Mapped[str] = mapped_column(String(32), default="")
    page: Mapped[int | None] = mapped_column(Integer)
    bbox: Mapped[list[float] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=FieldStatus.MISSING.value)

    invoice: Mapped["Invoice"] = relationship(back_populates="fields")

    __table_args__ = (
        UniqueConstraint("invoice_id", "field_path", name="uq_expense_field_path"),
        CheckConstraint(f"status IN ({_FIELD_STATUSES})", name="ck_expense_field_status"),
    )


class InvoiceCorrection(Base):
    """Audit trail of human field corrections; a CONFIRMED invoice's corrections can be
    promoted into the eval gold set so real fixes continuously harden the extractor."""

    __tablename__ = "expense_invoice_correction"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("expense_invoice.id", ondelete="CASCADE"), index=True)
    field_path: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(String(600))
    new_value: Mapped[str | None] = mapped_column(String(600))
    corrected_by: Mapped[str | None] = mapped_column(String(128))
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    promoted_to_gold: Mapped[bool] = mapped_column(Boolean, default=False)
