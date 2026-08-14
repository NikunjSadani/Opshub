"""Challan tables (prefixed `challan_`).

* `challan_batch`     — one upload run: source file, status, and the generated
                        artifacts (error report / ZIP / merged PDF).
* `challan`           — one issued delivery challan. Consignor + consignee are
                        SNAPSHOTTED here (copied, not FK'd) so later master-data
                        edits never rewrite an issued statutory document. Bound
                        to a numbering allocation.
* `challan_line_item` — first-class line items; money in BigInt paise.

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import
annotations` (py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    VALIDATED = "VALIDATED"      # parsed + validated OK; ready to generate
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ChallanStatus(str, enum.Enum):
    ISSUED = "ISSUED"
    VOID = "VOID"


class ChallanBatch(Base):
    __tablename__ = "challan_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    status: Mapped[str] = mapped_column(String(24), default=BatchStatus.PENDING.value, index=True)
    error_report_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    zip_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    merged_pdf_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    challan_count: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    challans: Mapped[list["Challan"]] = relationship(back_populates="batch")


class Challan(Base):
    __tablename__ = "challan"
    __table_args__ = (
        CheckConstraint("status in ('ISSUED', 'VOID')", name="ck_challan_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("challan_batch.id"), index=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("numbering_allocation.id"), unique=True)

    number: Mapped[str] = mapped_column(String(64), index=True)  # GIF/DC/26-27/L/000189
    series: Mapped[str] = mapped_column(String(8))
    fy: Mapped[str] = mapped_column(String(7), index=True)
    number_int: Mapped[int] = mapped_column(Integer)
    challan_date: Mapped[date] = mapped_column(Date)
    # The referenced project's human id (e.g. "BRI-001"), validated ACTIVE at
    # generation and snapshotted here so the register/print never re-resolve it.
    project_code: Mapped[str] = mapped_column(String(24), default="", index=True)

    # consignor snapshot (the single fixed dispatching entity)
    consignor_name: Mapped[str] = mapped_column(String(200))
    consignor_gstin: Mapped[str] = mapped_column(String(15))
    consignor_state: Mapped[str] = mapped_column(String(60))
    consignor_address: Mapped[str] = mapped_column(String(600), default="")
    consignor_phone: Mapped[str] = mapped_column(String(40), default="")

    # consignee snapshot (resolved from the GSTIN-keyed golden record, inc 15;
    # `consignee_brand` is retained NULLABLE for pre-inc-15 issued rows and is no
    # longer written — the inline GSTIN identifies the party now).
    consignee_brand: Mapped[str | None] = mapped_column(String(120), default="")
    consignee_name: Mapped[str] = mapped_column(String(200))
    consignee_gstin: Mapped[str] = mapped_column(String(15))
    consignee_state: Mapped[str] = mapped_column(String(60))
    consignee_address: Mapped[str] = mapped_column(String(600), default="")  # joined
    consignee_phone: Mapped[str] = mapped_column(String(40), default="")

    # ship-to ("Detail of Shipment to"). Captured SPLIT (line1/line2/city/pincode)
    # and also stored joined in `ship_to_address` for faithful printing.
    ship_to_name: Mapped[str] = mapped_column(String(200))
    ship_to_address: Mapped[str] = mapped_column(String(600))  # joined block (print)
    ship_to_address_line1: Mapped[str] = mapped_column(String(300), default="")
    ship_to_address_line2: Mapped[str] = mapped_column(String(300), default="")
    ship_to_city: Mapped[str] = mapped_column(String(120), default="")
    ship_to_pincode: Mapped[str] = mapped_column(String(10), default="")
    ship_to_state: Mapped[str] = mapped_column(String(60))
    ship_to_enterprise: Mapped[str] = mapped_column(String(200), default="")
    ship_to_number: Mapped[str] = mapped_column(String(40), default="")  # phone
    ship_to_contact: Mapped[str] = mapped_column(String(200), default="")  # legacy, unused

    po_number: Mapped[str] = mapped_column(String(60), default="")
    invoice_number: Mapped[str] = mapped_column(String(60), default="")
    eway_required: Mapped[bool] = mapped_column(Boolean, default=False)
    total_paise: Mapped[int | None] = mapped_column(BigInteger)  # None = value-free challan

    status: Mapped[str] = mapped_column(String(16), default=ChallanStatus.ISSUED.value, index=True)
    void_reason: Mapped[str | None] = mapped_column(String(300))
    pdf_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    batch: Mapped[ChallanBatch] = relationship(back_populates="challans")
    lines: Mapped[list["ChallanLineItem"]] = relationship(
        back_populates="challan", cascade="all, delete-orphan", order_by="ChallanLineItem.line_no"
    )


class ChallanLineItem(Base):
    __tablename__ = "challan_line_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    challan_id: Mapped[int] = mapped_column(
        ForeignKey("challan.id", ondelete="CASCADE"), index=True
    )
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(600))
    hsn: Mapped[str] = mapped_column(String(12))
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    rate_text: Mapped[str] = mapped_column(String(60), default="")
    rate_paise: Mapped[int | None] = mapped_column(BigInteger)
    amount_paise: Mapped[int | None] = mapped_column(BigInteger)  # None = value-free line
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    challan: Mapped[Challan] = relationship(back_populates="lines")
