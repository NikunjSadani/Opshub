"""Async-jobs primitive — in-process background + polling contract + idempotency.

A `Job` row is the durable, pollable record of a long-running unit of work. A
handler creates a Job (optionally idempotent via `idempotency_key`), kicks the
real work onto FastAPI's in-process `BackgroundTasks`, and returns the job id;
the client then polls `GET /api/v1/jobs/{id}` until COMPLETED / FAILED.

The worker itself lives per-module (wired later). This module owns the state
machine (PENDING -> RUNNING -> COMPLETED|FAILED), progress accounting, and the
idempotency guarantee — create with the same key twice, get the same job once.

NOTE: this module DEFINES SQLAlchemy models, so it must NOT
`from __future__ import annotations` (py3.14 SQLAlchemy crash).
"""
import enum
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import BackgroundTasks
from sqlalchemy import JSON, DateTime, Enum, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Job(Base):
    """A pollable async job. `result`/`error` are populated on terminal states."""

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.PENDING)
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


_ACTIVE = (JobStatus.PENDING, JobStatus.RUNNING)


def _require(job: Job, allowed: tuple[JobStatus, ...]) -> None:
    if job.status not in allowed:
        raise ValueError(f"invalid job transition from {job.status.value}")


def create_job(
    db: Session,
    kind: str,
    *,
    idempotency_key: str | None = None,
    total: int = 0,
    created_by: str | None = None,
) -> tuple[Job, bool]:
    """Create a Job, or return the existing one for a repeated idempotency_key.

    Returns `(job, created)`. **The caller MUST only schedule the worker when
    `created` is True** — otherwise a repeated key runs the work twice against
    one row. A concurrent-insert race is contained with a SAVEPOINT so the
    caller's other uncommitted work (e.g. an audit row) is NOT rolled back.
    """
    if idempotency_key is not None:
        existing = db.execute(
            select(Job).where(Job.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

    job = Job(
        kind=kind,
        status=JobStatus.PENDING,
        total=total,
        done=0,
        idempotency_key=idempotency_key,
        result={},
        created_by=created_by,
    )
    try:
        # `with` releases the savepoint on success (a bare begin_nested() leaks one).
        with db.begin_nested():  # SAVEPOINT: only this insert is undone on a race
            db.add(job)
            db.flush()
    except IntegrityError:
        if idempotency_key is None:
            raise
        winner = db.execute(
            select(Job).where(Job.idempotency_key == idempotency_key)
        ).scalar_one()
        return winner, False
    return job, True


def set_progress(db: Session, job: Job, done: int) -> Job:
    """Record incremental progress (`done` out of `job.total`)."""
    _require(job, _ACTIVE)
    job.done = done
    job.updated_at = _utcnow()
    db.add(job)
    db.flush()
    return job


def mark_running(db: Session, job: Job) -> Job:
    """Transition PENDING -> RUNNING as the worker picks the job up."""
    _require(job, (JobStatus.PENDING,))
    job.status = JobStatus.RUNNING
    job.updated_at = _utcnow()
    db.add(job)
    db.flush()
    return job


def complete(db: Session, job: Job, result: dict[str, Any]) -> Job:
    """Terminal success: store `result`, mark COMPLETED, snap done->total.
    Guarded so a terminal job can't be re-completed (double-execution corruption)."""
    _require(job, _ACTIVE)
    job.status = JobStatus.COMPLETED
    job.result = result
    if job.total:
        job.done = job.total
    job.error = None
    job.updated_at = _utcnow()
    db.add(job)
    db.flush()
    return job


def fail(db: Session, job: Job, error: str) -> Job:
    """Terminal failure: store `error`, mark FAILED. Guarded against re-terminating."""
    _require(job, _ACTIVE)
    job.status = JobStatus.FAILED
    job.error = error
    job.updated_at = _utcnow()
    db.add(job)
    db.flush()
    return job


def run_in_background(
    background_tasks: BackgroundTasks, fn: Callable[..., Any], *args: Any
) -> None:
    """In-process background pattern.

    Schedule `fn(*args)` to run after the response is sent, on FastAPI's
    in-process `BackgroundTasks` (same worker, no external queue). The typical
    handler shape:

        job, created = create_job(db, "challan.bulk", idempotency_key=key, total=n)
        if created:                       # only run the work ONCE per idempotency key
            run_in_background(background_tasks, worker, job.id)
        return {"id": job.id}

    The per-module `worker` opens its OWN DB session, calls `mark_running`,
    `set_progress`, then `complete`/`fail`. This helper just documents and wires
    that pattern; the concrete worker is supplied by each module.
    """
    background_tasks.add_task(fn, *args)
