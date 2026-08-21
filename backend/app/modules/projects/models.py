"""Projects tables (prefixed `project`).

* `project_client` — a client/brand registered by an admin, keyed by a unique
                     3-letter uppercase code (e.g. "BRI"). The code is the stable
                     prefix every project id is built from.
* `project`        — one project under a client. Its human id `code` is
                     `<CLIENT_CODE>-<seq>` (e.g. "BRI-001"), where `seq` is a
                     per-client running number. Integrity mirrors the numbering
                     engine's discipline: allocation serializes on a row-lock of
                     the parent client, and TWO unique constraints are the DB
                     backstop — `code` globally and `(client_id, seq)` per client
                     — so a race can never mint a duplicate id.

NOTE: defines SQLAlchemy models, so it must NOT `from __future__ import
annotations` (py3.14 SQLAlchemy crash).
"""
import enum
from datetime import UTC, date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Client-master child tables (`client_gstin` / `client_address` / `client_contact`)
# live in THIS module because they belong to `project_client` — the promoted client
# master. A client may hold MANY GSTINs (state-wise registrations) and MANY billing
# addresses/contacts. Edits are gated by `projects` MANAGE (`client.manage`). Other
# modules (e.g. sales_orders.purchase_order) reference a client_gstin by ID via a
# plain FK and resolve by join — no cross-module ORM relationship.


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ProjectStatus(str, enum.Enum):
    """The lifecycle states a project may hold."""

    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    CLOSED = "CLOSED"


class ProjectClient(Base):
    """A client — the owner of a family of projects, keyed by a 3-letter code."""

    __tablename__ = "project_client"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Exactly 3 uppercase A-Z (validated in the service); unique so a code maps to
    # exactly one client and the generated project ids are unambiguous.
    code: Mapped[str] = mapped_column(String(3), unique=True, index=True)
    # Promoted client-master fields (inc 28). PAN is the org's golden key; credit
    # terms (days) drive invoice due-date + AR aging downstream. Both nullable so
    # existing clients (incl. the GEN overhead client) are unaffected.
    pan: Mapped[str | None] = mapped_column(String(10))
    credit_terms_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="client")
    gstins: Mapped[list["ClientGstin"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    addresses: Mapped[list["ClientAddress"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    contacts: Mapped[list["ClientContact"]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )


class ClientGstin(Base):
    """One GST registration held by a client (state-wise). A client may hold many."""

    __tablename__ = "client_gstin"
    __table_args__ = (
        # A GSTIN appears at most once per client (normalized upper-case in the service).
        UniqueConstraint("client_id", "gstin", name="uq_client_gstin"),
        # At most ONE default GSTIN per client, DB-enforced (a partial unique index) so a
        # concurrent "set default" can never leave two. The service demotes the old default
        # BEFORE writing the new one so the index never momentarily sees two.
        Index(
            "uq_client_gstin_one_default", "client_id", unique=True,
            sqlite_where=text("is_default = 1"), postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("project_client.id", ondelete="CASCADE"), index=True
    )
    gstin: Mapped[str] = mapped_column(String(15))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    state_code: Mapped[str | None] = mapped_column(String(2))  # first 2 digits of GSTIN
    is_default: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    client: Mapped[ProjectClient] = relationship(back_populates="gstins")


class ClientAddress(Base):
    """A billing/shipping address for a client, optionally tied to one GST registration."""

    __tablename__ = "client_address"
    __table_args__ = (
        Index(
            "uq_client_address_one_default", "client_id", unique=True,
            sqlite_where=text("is_default = 1"), postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("project_client.id", ondelete="CASCADE"), index=True
    )
    # Optional link to the GSTIN this address registers under (state-wise). SET NULL
    # so removing a GSTIN never cascades away an address the client still uses.
    gstin_id: Mapped[int | None] = mapped_column(
        ForeignKey("client_gstin.id", ondelete="SET NULL"), index=True
    )
    label: Mapped[str | None] = mapped_column(String(120))  # e.g. "Head Office"
    line1: Mapped[str] = mapped_column(String(300))
    line2: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(120))
    pincode: Mapped[str | None] = mapped_column(String(10))
    is_default: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    client: Mapped[ProjectClient] = relationship(back_populates="addresses")


class ClientContact(Base):
    """A point-of-contact person at a client."""

    __tablename__ = "client_contact"
    __table_args__ = (
        Index(
            "uq_client_contact_one_default", "client_id", unique=True,
            sqlite_where=text("is_default = 1"), postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("project_client.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(20))
    designation: Mapped[str | None] = mapped_column(String(120))
    is_default: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    client: Mapped[ProjectClient] = relationship(back_populates="contacts")


class Project(Base):
    """One project under a client. `code` = `<CLIENT_CODE>-<seq:03d>` (e.g. BRI-001)."""

    __tablename__ = "project"
    __table_args__ = (
        # Per-client running number: the sequence is dense + unique within a
        # client, so two projects under the same client can never share a seq
        # (the backstop for the row-locked allocation in the service).
        UniqueConstraint("client_id", "seq", name="uq_project_client_seq"),
        CheckConstraint(
            "status in ('ACTIVE', 'ON_HOLD', 'CLOSED')", name="ck_project_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("project_client.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    # The system-assigned human id, e.g. "BRI-001". Globally unique backstop.
    code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=ProjectStatus.ACTIVE.value, index=True)
    description: Mapped[str | None] = mapped_column(String(1000))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    client: Mapped[ProjectClient] = relationship(back_populates="projects")
