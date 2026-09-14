"""Sales Orders tables — the revenue-side foundation of the Project Spine.

Tables:
* `product`         — the Product Master: a normalized identity (name/brand/model/
                      category/UOM) that PO line items reference, so quote history
                      aggregates cleanly instead of fragmenting on free text.
* `purchase_order`  — a client's PO. `po_number` is the CLIENT's own reference
                      (received from the client — NOT minted by us), unique within
                      a client. Belongs to one project; a project may have many POs.
* `po_line_item`    — one priced line on a PO. The atomic unit of the whole spine.
* `po_amendment`    — an immutable, versioned snapshot of a PO change (never overwrite).

Money is integer PAISE in BigInteger columns (never float); quantities are Numeric.
Convention: `cost_price_paise` / `sell_price_paise` are PER-UNIT; `freight/packaging/
handling/other_paise` are per-LINE totals. So line revenue = ordered_qty*sell_price;
line cost = ordered_qty*cost_price + freight + packaging + handling + other.

Module-boundary rule: cross-module references (project, client, client GSTIN, stored
file) are plain FK columns with NO ORM relationship — resolved by join at the service
layer (mirrors expense↔project). Only same-module relationships are mapped.

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
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class POStatus(str, enum.Enum):
    """Lifecycle of a purchase order."""

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class LineStatus(str, enum.Enum):
    """Financial state of a PO line (derived from balances; SHORT_CLOSED is a manual
    audited override that retires the remaining open-to-invoice quantity)."""

    OPEN = "OPEN"
    SHORT_CLOSED = "SHORT_CLOSED"
    CLOSED = "CLOSED"


class Product(Base):
    """Product Master — a normalized identity PO line items reference for quote search."""

    __tablename__ = "product"
    __table_args__ = (
        # Case-insensitive identity: (name, brand, model_number) must be unique
        # ignoring case, with NULL brand/model treated as '' so two rows that
        # differ only by case/whitespace can't both persist and fragment history.
        # Applied by hand (identically) in the migration; alembic check confirms.
        Index(
            "uq_product_identity",
            text("lower(name)"),
            text("lower(coalesce(brand, ''))"),
            text("lower(coalesce(model_number, ''))"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Optional SKU/code (unique when present); the identity index is the real dedup key.
    code: Mapped[str | None] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(120))
    model_number: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(120))
    uom: Mapped[str] = mapped_column(String(20), default="PCS")  # unit of measure
    hsn: Mapped[str | None] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProjectProduct(Base):
    """Tags a Product to a Project (M:N) — curates that project's PO product picker.

    A product is a SHARED master reused across clients/projects, so this is a tag link,
    never a re-parenting: one product can be tagged to many projects and vice-versa.
    Cross-module: `project_id` references `project.id` by id (no ORM relationship, per the
    module-boundary rule); the tagged product IS same-module, so it maps a relationship.
    Both FKs cascade on delete so a hard-deleted project/product drops its tag rows."""

    __tablename__ = "project_product"
    __table_args__ = (
        UniqueConstraint("project_id", "product_id", name="uq_project_product"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("product.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    product: Mapped[Product] = relationship()


class PurchaseOrder(Base):
    """A client's purchase order. `po_number` is the client's own reference."""

    __tablename__ = "purchase_order"
    __table_args__ = (
        # A client's PO number is unique within that client (two clients may reuse
        # the same external number; one client may not) — but a PO number is now
        # OPTIONAL, so the uniqueness is a PARTIAL unique index scoped to rows that
        # actually carry a number. Two NULL-number POs for one client both persist
        # (NULLs are excluded from the index); a duplicate NON-null number still
        # collides. Declared here identically to the migration so `alembic check` agrees.
        Index(
            "uq_po_client_number_present", "client_id", "po_number", unique=True,
            sqlite_where=text("po_number IS NOT NULL"),
            postgresql_where=text("po_number IS NOT NULL"),
        ),
        CheckConstraint(
            "status in ('DRAFT', 'CONFIRMED', 'IN_PROGRESS', 'CLOSED', 'CANCELLED')",
            name="ck_purchase_order_status",
        ),
        CheckConstraint(
            "agency_fee_type in ('NONE', 'PERCENT', 'FIXED')",
            name="ck_purchase_order_agency_fee_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Client-supplied — now OPTIONAL (a PO may be captured before its number arrives).
    po_number: Mapped[str | None] = mapped_column(String(64), index=True)
    # Cross-module FKs (projects/files modules) — NO ORM relationship; resolve by join.
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    client_gstin_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_gstin.id"), index=True
    )
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    soft_copy_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("files_stored_file.id"), index=True
    )
    po_date: Mapped[date] = mapped_column(Date)
    expected_procurement_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=POStatus.DRAFT.value, index=True)
    notes: Mapped[str | None] = mapped_column(String(1000))
    close_reason: Mapped[str | None] = mapped_column(String(500))
    closed_by: Mapped[str | None] = mapped_column(String(128))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # PO-level agency fee: NONE (no fee), PERCENT (agency_fee_percent, 0..100) or FIXED
    # (agency_fee_amount_paise). The CHECK above pins the type; the service enforces the
    # matching field is set and the other is null.
    agency_fee_type: Mapped[str] = mapped_column(
        String(8), default="NONE", server_default=text("'NONE'")
    )
    agency_fee_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    agency_fee_amount_paise: Mapped[int | None] = mapped_column(BigInteger)

    lines: Mapped[list["POLineItem"]] = relationship(
        back_populates="po", cascade="all, delete-orphan"
    )
    amendments: Mapped[list["POAmendment"]] = relationship(
        back_populates="po", cascade="all, delete-orphan"
    )


class POLineItem(Base):
    """One priced line on a PO — the atomic unit of the Project Spine.

    Prices are PER-UNIT paise; freight/packaging/handling/other are per-LINE paise.
    """

    __tablename__ = "po_line_item"
    __table_args__ = (
        CheckConstraint(
            "line_status in ('OPEN', 'SHORT_CLOSED', 'CLOSED')",
            name="ck_po_line_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(ForeignKey("product.id"), index=True)
    description: Mapped[str] = mapped_column(String(500))  # snapshot at PO time
    uom: Mapped[str] = mapped_column(String(20), default="PCS")
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    # --- cost tier (per unit) ---
    cost_price_paise: Mapped[int] = mapped_column(BigInteger)   # Our CP (billed to us), visible
    # Original CP, visible, optional
    original_cost_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    # --- sell tiers (per unit) ---
    # client-quoted sell (visible, required at validation); vendor sell (visible, optional)
    client_sell_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    vendor_sell_price_paise: Mapped[int | None] = mapped_column(BigInteger)
    sell_price_paise: Mapped[int] = mapped_column(BigInteger)   # ACTUAL sell — ADMIN-ONLY
    # --- freight tiers (per line) ---
    # client freight (visible, default 0); vendor freight (visible, optional)
    client_freight_paise: Mapped[int | None] = mapped_column(BigInteger, default=0)
    vendor_freight_paise: Mapped[int | None] = mapped_column(BigInteger)
    freight_paise: Mapped[int] = mapped_column(BigInteger, default=0)  # ACTUAL freight — ADMIN-ONLY
    packaging_paise: Mapped[int] = mapped_column(BigInteger, default=0)    # per line
    handling_paise: Mapped[int] = mapped_column(BigInteger, default=0)     # per line
    other_paise: Mapped[int] = mapped_column(BigInteger, default=0)        # per line
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))  # GST %
    line_status: Mapped[str] = mapped_column(String(16), default=LineStatus.OPEN.value)
    # Recorded when an operator manually short-closes the remaining open quantity.
    short_closed_qty: Mapped[Decimal] = mapped_column(Numeric(18, 3), default=Decimal("0"))
    short_close_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()


class POAmendment(Base):
    """Immutable versioned record of a PO change (never overwrite a PO's history)."""

    __tablename__ = "po_amendment"
    __table_args__ = (
        UniqueConstraint("po_id", "version", name="uq_po_amendment_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_order.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(String(500))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # full PO+lines snapshot
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    po: Mapped[PurchaseOrder] = relationship(back_populates="amendments")
