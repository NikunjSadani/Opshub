"""Master-data tables (prefixed `md_`).

Reference/config data maintained by admins and consumed by the challan
generator:

* `md_consignor`      — a dispatching party (Gifsy's own GSTIN-holding entity).
* `md_consignee`      — the Brand -> State -> {GSTIN, address} registry. The
                         consignee on a challan is resolved by (brand, ship-to
                         state) and SNAPSHOTTED onto the document, so later edits
                         here never rewrite an issued challan.
* `md_hsn`            — HSN code + description + GST rate (rate used to validate
                         uploaded line items).
* `md_series`         — a challan numbering series (letter + label); ties to the
                         numbering engine's `series`.

GST rate is a Numeric percent (e.g. 18.00). Amounts do NOT live here — money is
transactional and lives on the challan/line-item rows in BigInt-paise.

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import
annotations` (py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MdKind(str, enum.Enum):
    """The master-data entity kinds (used by the generic CRUD surface)."""

    CONSIGNOR = "consignor"
    CONSIGNEE = "consignee"
    HSN = "hsn"
    SERIES = "series"


class Consignor(Base):
    """A dispatching party — the 'from' block on a challan."""

    __tablename__ = "md_consignor"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    gstin: Mapped[str] = mapped_column(String(15), index=True)
    state: Mapped[str] = mapped_column(String(60))
    address: Mapped[str] = mapped_column(String(600), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Consignee(Base):
    """Brand -> State -> {GSTIN, address}. One row per (brand, state)."""

    __tablename__ = "md_consignee"
    __table_args__ = (
        UniqueConstraint("brand", "state", name="uq_md_consignee_brand_state"),
        # Case-insensitive backstop: "Deoleo"/"deoleo" (or trailing-space) variants
        # can't both persist, so consignee resolution can never be ambiguous.
        Index(
            "uq_md_consignee_brand_state_ci",
            text("lower(trim(brand))"),
            text("lower(trim(state))"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(120), index=True)
    state: Mapped[str] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(200))  # legal name for this brand+state
    gstin: Mapped[str] = mapped_column(String(15))
    address: Mapped[str] = mapped_column(String(600), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    active: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ConsigneeParty(Base):
    """A consignee party golden record, keyed on GSTIN (the golden key).

    Distinct from `Consignee` (the brand->state challan-lookup registry): this is
    a party repository resolved by GSTIN. Uploads auto-create a new GSTIN and, for
    an existing GSTIN with differing details, return the STORED record plus a
    deviation report — never a silent overwrite. Admins may also create/edit rows.
    """

    __tablename__ = "md_consignee_party"

    id: Mapped[int] = mapped_column(primary_key=True)
    gstin: Mapped[str] = mapped_column(String(15), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    address_line1: Mapped[str] = mapped_column(String(300), default="")
    address_line2: Mapped[str] = mapped_column(String(300), default="")
    pincode: Mapped[str] = mapped_column(String(10), default="")
    state: Mapped[str] = mapped_column(String(60), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    source: Mapped[str] = mapped_column(String(16), default="MANUAL")  # MANUAL | UPLOAD
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class HsnCode(Base):
    """HSN/SAC code + GST rate (percent) used to validate uploaded line items."""

    __tablename__ = "md_hsn"

    id: Mapped[int] = mapped_column(primary_key=True)
    hsn: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    active: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Series(Base):
    """A challan numbering series (letter + label). Ties to numbering `series`."""

    __tablename__ = "md_series"

    id: Mapped[int] = mapped_column(primary_key=True)
    letter: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(default=True)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
