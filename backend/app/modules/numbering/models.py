"""Numbering tables.

Two tables, prefixed `numbering_`:

* `numbering_counter` — one row per (series, fy); `last_number` is the monotonic
  high-water mark. This is the row we LOCK (`with_for_update`) to serialize
  allocation. It never rolls back, so numbers are gap-free-monotonic and never
  reused.
* `numbering_allocation` — the durable ledger: one row per reserved / issued /
  void number. A **unique partial index on non-void `(series, fy, number)`**
  (added by hand in the migration) is the hard backstop: at most one ACTIVE
  (RESERVED or ISSUED) allocation can ever exist for a number, regardless of any
  locking bug. VOID rows are exempt because a void is terminal history.

`status` is a plain String (not a DB Enum) ON PURPOSE: the partial-index
predicate `status <> 'VOID'` is then byte-identical on sqlite and Postgres, and
we dodge the cross-dialect Enum-DDL divergence flagged in earlier increments.
The Python `AllocationStatus` enum gives type-safety in code.

NOTE: this module DEFINES SQLAlchemy models, so it must NOT
`from __future__ import annotations` (py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AllocationStatus(str, enum.Enum):
    RESERVED = "RESERVED"  # number claimed, document not yet generated
    ISSUED = "ISSUED"      # bound to a generated document
    VOID = "VOID"          # cancelled/orphaned — terminal, retained as history


# Non-void = the "active" states the partial unique index guards.
ACTIVE_STATUSES: tuple[AllocationStatus, ...] = (
    AllocationStatus.RESERVED,
    AllocationStatus.ISSUED,
)


class NumberingCounter(Base):
    """High-water mark per (series, fy). Locked during allocation."""

    __tablename__ = "numbering_counter"
    __table_args__ = (UniqueConstraint("series", "fy", name="uq_numbering_counter"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(8), index=True)
    fy: Mapped[str] = mapped_column(String(7), index=True)  # "26-27"
    last_number: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class NumberingAllocation(Base):
    """One reserved / issued / void number. The ledger + duplicate backstop."""

    __tablename__ = "numbering_allocation"
    __table_args__ = (
        # Invariant I1 (the statutory duplicate backstop): at most ONE active
        # (non-VOID) allocation per (series, fy, number). Partial so a cancelled
        # number's history row never blocks the sequence. Because `status` is a
        # plain String, the SAME predicate works on sqlite and Postgres (no Enum
        # cast). The migration applies this identically by hand for real DBs.
        Index(
            "uq_numbering_active",
            "series",
            "fy",
            "number",
            unique=True,
            sqlite_where=text("status <> 'VOID'"),
            postgresql_where=text("status <> 'VOID'"),
        ),
        # Defense-in-depth: only the three canonical states are storable, so no
        # raw-SQL / future-bug write of e.g. 'DELETED' can occupy an active index
        # slot the predicate (status <> 'VOID') would otherwise treat as live.
        CheckConstraint(
            "status in ('RESERVED', 'ISSUED', 'VOID')", name="ck_numbering_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(8), index=True)
    fy: Mapped[str] = mapped_column(String(7), index=True)
    number: Mapped[int] = mapped_column(Integer)
    formatted: Mapped[str] = mapped_column(String(64), index=True)  # GIF/DC/26-27/L/000189
    status: Mapped[str] = mapped_column(
        String(16), default=AllocationStatus.RESERVED.value, index=True
    )

    # Repeated allocation with the same key RESUMES the existing reservation
    # instead of burning a fresh number (safe retries). Unique so two concurrent
    # retries can't both create a reservation.
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)

    # What the number is bound to once issued (e.g. a challan row).
    entity: Mapped[str | None] = mapped_column(String(120))
    entity_id: Mapped[str | None] = mapped_column(String(120))

    reserved_by: Mapped[str | None] = mapped_column(String(128))
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(String(300))
