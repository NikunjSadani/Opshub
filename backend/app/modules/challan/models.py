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
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    NEEDS_REVIEW = "NEEDS_REVIEW"  # valid, but has consignee contradictions to decide
    VALIDATED = "VALIDATED"      # parsed + validated OK; ready to generate
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DecisionChoice(str, enum.Enum):
    """How the operator resolves one consignee-field contradiction vs the golden record."""

    PENDING = "PENDING"              # not yet decided (blocks generation)
    UPDATE_MASTER = "UPDATE_MASTER"  # golden record takes the uploaded value + prints it
    THIS_UPLOAD = "THIS_UPLOAD"      # print the uploaded value on this batch; master unchanged
    REJECT = "REJECT"               # keep + print the stored golden-record value


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
    decisions: Mapped[list["ChallanBatchDecision"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ChallanBatchDecision(Base):
    """One consignee-field contradiction (uploaded value vs the stored golden
    record) that the operator must resolve before a NEEDS_REVIEW batch can
    generate. One row per (batch, gstin, field); `choice` starts PENDING.

    `stored_value`/`uploaded_value` are snapshotted at detection so the review UI +
    the downloadable Excel report show exactly what was compared, and generation
    applies the recorded choice without re-deriving it."""

    __tablename__ = "challan_batch_decision"
    __table_args__ = (
        UniqueConstraint("batch_id", "gstin", "field", name="uq_challan_decision"),
        CheckConstraint(
            "choice in ('PENDING', 'UPDATE_MASTER', 'THIS_UPLOAD', 'REJECT')",
            name="ck_challan_decision_choice",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("challan_batch.id", ondelete="CASCADE"), index=True
    )
    gstin: Mapped[str] = mapped_column(String(15), index=True)
    consignee_name: Mapped[str] = mapped_column(String(200), default="")  # uploaded, for display
    field: Mapped[str] = mapped_column(String(40))       # name/address_line1/.../phone
    stored_value: Mapped[str] = mapped_column(String(600), default="")
    uploaded_value: Mapped[str] = mapped_column(String(600), default="")
    choice: Mapped[str] = mapped_column(String(16), default=DecisionChoice.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    batch: Mapped[ChallanBatch] = relationship(back_populates="decisions")


class Challan(Base):
    __tablename__ = "challan"
    # The composite serves the duplicate-register warning query (consignee_gstin IN
    # (...) AND challan_date IN (...) AND status='ISSUED') on the synchronous upload
    # path; the standalone `challan_date` index (declared inline below) serves the
    # date-range register filter + /challan/summary. Without these the register
    # full-scans as it grows.
    __table_args__ = (
        CheckConstraint("status in ('ISSUED', 'VOID')", name="ck_challan_status"),
        Index("ix_challan_consignee_gstin_challan_date", "consignee_gstin", "challan_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("challan_batch.id"), index=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("numbering_allocation.id"), unique=True)

    number: Mapped[str] = mapped_column(String(64), index=True)  # GIF/DC/26-27/L/000189
    series: Mapped[str] = mapped_column(String(8))
    fy: Mapped[str] = mapped_column(String(7), index=True)
    number_int: Mapped[int] = mapped_column(Integer)
    challan_date: Mapped[date] = mapped_column(Date, index=True)
    # The referenced project's human id (e.g. "BRI-001"), validated ACTIVE at
    # generation and snapshotted here so the register/print never re-resolve it.
    project_code: Mapped[str] = mapped_column(String(24), default="", index=True)
    # The upload's `Challan Group` label this challan came from. Lets a RESUME of a
    # partially-generated batch tell which groups are already issued (done), so a
    # later master-data change to a completed group can't wedge the retry.
    group_key: Mapped[str] = mapped_column(String(120), default="")

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
    # Opaque token the challan QR encodes (…/d/{access_token}); the public viewer resolves it
    # back to this challan → its invoice_number + client → the uploaded invoice (late-bind).
    # NULL until minted at generation; unique so it can't collide or be enumerated.
    access_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
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


class AccessOutcome:
    """The mutually-exclusive outcomes of one PIN submission on the public
    challan-QR invoice viewer (stored verbatim in `challan_invoice_access.outcome`).

    Plain string constants (not an Enum) so callers write `AccessOutcome.VIEWED`
    and the column stores the bare string. `FAILED` groups the three brute-force /
    misconfiguration outcomes for reporting (WRONG_PIN + RATE_LIMITED + NO_PIN)."""

    VIEWED = "VIEWED"                # correct PIN + invoice served (PDF streamed)
    WRONG_PIN = "WRONG_PIN"         # PIN mismatch (constant-time compare failed)
    NOT_AVAILABLE = "NOT_AVAILABLE"  # correct PIN but no confirmed/servable invoice
    RATE_LIMITED = "RATE_LIMITED"   # token locked out (too many failures)
    NO_PIN = "NO_PIN"               # no client / no access PIN configured

    ALL = (VIEWED, WRONG_PIN, NOT_AVAILABLE, RATE_LIMITED, NO_PIN)
    FAILED = (WRONG_PIN, RATE_LIMITED, NO_PIN)


class ChallanInvoiceAccess(Base):
    """One PIN submission on the PUBLIC challan-QR invoice viewer (POST /d/{token}).

    An append-only audit log powering the operator "Invoice Access" dashboard. Rows
    are written best-effort from the public route (a logging failure never changes the
    visitor's response) and only for RESOLVED tokens — unknown/unresolved tokens log
    nothing (abuse-safe, mirrors the rate limiter's "only resolved tokens get state").

    `viewer_hash` is a salted, truncated SHA-256 of the client IP for approx-distinct
    viewer counting + coarse privacy (NOT security); the raw IP is never stored.
    """

    __tablename__ = "challan_invoice_access"
    __table_args__ = (
        CheckConstraint(
            "outcome in ('VIEWED', 'WRONG_PIN', 'NOT_AVAILABLE', 'RATE_LIMITED', 'NO_PIN')",
            name="ck_challan_invoice_access_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    challan_id: Mapped[int] = mapped_column(ForeignKey("challan.id"), index=True)
    # Resolved best-effort from challan -> project -> client; NULL when the challan's
    # project/client can't be resolved (kept plain, not FK'd, so the log never blocks
    # on referential coupling to the client master).
    client_id: Mapped[int | None] = mapped_column(Integer, index=True)
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    outcome: Mapped[str] = mapped_column(String(20))
    # Salted+truncated SHA-256 of the client IP (approx-distinct viewers + coarse
    # privacy, NOT security). NULL when the IP is unavailable.
    viewer_hash: Mapped[str | None] = mapped_column(String(64))


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
