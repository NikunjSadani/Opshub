"""Logistics tables (prefixed `logistics_`).

* `logistics_shipment` — one tracked delivery, keyed on the challan number (a soft
  join to `challan.number`; `challan_id` is resolved when the number matches an issued
  challan). Captures the partner/tracking/status + consignee + POD file. Bulk Excel
  upload (partner dump) or manual entry both land here.

Cross-module refs (challan, purchase_order, stored file) are plain FK columns with NO
ORM relationship — resolved by join (the module-boundary rule).

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import annotations`
(py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeliveryStatus(str, enum.Enum):
    """Lifecycle of a shipment (partner-reported, free to update on re-upload)."""

    PENDING = "PENDING"          # created, not yet dispatched
    DISPATCHED = "DISPATCHED"    # handed to the partner
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"      # POD available
    RETURNED = "RETURNED"        # RTO
    FAILED = "FAILED"            # delivery attempt failed / cancelled


class Shipment(Base):
    """One tracked delivery against a challan."""

    __tablename__ = "logistics_shipment"
    __table_args__ = (
        # One shipment row per challan number (a re-uploaded partner dump UPDATES the row
        # rather than duplicating it — the bulk importer upserts on this key).
        UniqueConstraint("challan_number", name="uq_logistics_shipment_challan_number"),
        CheckConstraint(
            "status in ('PENDING','DISPATCHED','IN_TRANSIT','DELIVERED','RETURNED','FAILED')",
            name="ck_logistics_shipment_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # The join key: the challan's formatted number (e.g. GIF/DC/26-27/L/000189). `challan_id`
    # is resolved to the challan row when the number matches an issued challan (else NULL —
    # a partner dump may reference a number we can't yet resolve; never dropped).
    challan_number: Mapped[str] = mapped_column(String(64), index=True)
    challan_id: Mapped[int | None] = mapped_column(ForeignKey("challan.id"), index=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order.id"), index=True)

    tracking_id: Mapped[str | None] = mapped_column(String(120), index=True)
    delivery_partner: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(
        String(24), default=DeliveryStatus.PENDING.value, index=True)

    consignee_name: Mapped[str | None] = mapped_column(String(200))
    address: Mapped[str | None] = mapped_column(String(600))
    phone: Mapped[str | None] = mapped_column(String(40))
    pincode: Mapped[str | None] = mapped_column(String(10))

    dispatched_on: Mapped[date | None] = mapped_column(Date)
    delivered_on: Mapped[date | None] = mapped_column(Date)
    pod_file_id: Mapped[int | None] = mapped_column(ForeignKey("files_stored_file.id"))
    notes: Mapped[str | None] = mapped_column(String(1000))

    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
