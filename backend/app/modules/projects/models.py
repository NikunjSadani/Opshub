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
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


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
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    projects: Mapped[list["Project"]] = relationship(back_populates="client")


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
