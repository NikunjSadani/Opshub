"""Async-jobs primitive: idempotency, state transitions, IDOR scoping, polling.

In-memory sqlite (StaticPool so sessions share one connection) + a throwaway app
with auth + db overridden. No Firebase / Postgres required.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.modules.jobs.routes import router
from app.platform.auth import current_user
from app.platform.jobs import (
    Job,
    JobStatus,
    complete,
    create_job,
    mark_running,
    set_progress,
)
from app.platform.models import User
from tests.rbac_util import make_role, make_user

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db() -> Iterator[Session]:
    session = TestSession()
    try:
        yield session
    finally:
        session.close()


def _admin() -> User:
    return make_user("u1", role=make_role("Administrator", is_system=True))


def _make_client(db: Session, user: User) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    return TestClient(app)


def test_create_job_returns_a_job(db: Session) -> None:
    job, created = create_job(db, "challan.bulk", total=5, created_by="u1")
    assert created is True
    assert job.id is not None
    assert job.status is JobStatus.PENDING
    assert job.total == 5


def test_idempotency_key_returns_same_job(db: Session) -> None:
    first, c1 = create_job(db, "challan.bulk", idempotency_key="k-1", total=3)
    db.commit()
    second, c2 = create_job(db, "challan.bulk", idempotency_key="k-1", total=3)
    assert c1 is True and c2 is False  # only the first submission is a real create
    assert second.id == first.id
    count = db.execute(
        select(func.count()).select_from(Job).where(Job.idempotency_key == "k-1")
    ).scalar_one()
    assert count == 1


def test_no_idempotency_key_creates_distinct_jobs(db: Session) -> None:
    a, _ = create_job(db, "challan.bulk")
    b, _ = create_job(db, "challan.bulk")
    assert a.id != b.id


def test_progress_then_complete_transitions(db: Session) -> None:
    job, _ = create_job(db, "challan.bulk", total=10)
    mark_running(db, job)
    set_progress(db, job, 4)
    assert job.done == 4
    complete(db, job, {"ok": True})
    assert job.status is JobStatus.COMPLETED
    assert job.done == job.total == 10


def test_terminal_job_cannot_be_recompleted(db: Session) -> None:
    """The transition guard blocks double-completion (double-execution corruption)."""
    job, _ = create_job(db, "challan.bulk", total=1)
    complete(db, job, {"first": True})
    with pytest.raises(ValueError, match="invalid job transition"):
        complete(db, job, {"second": True})
    with pytest.raises(ValueError):
        mark_running(db, job)


def test_get_job_scoped_to_creator_or_admin(db: Session) -> None:
    job, _ = create_job(db, "challan.bulk", total=7, created_by="owner-uid")
    db.commit()

    # Admin sees any job.
    admin_res = _make_client(db, _admin()).get(f"/api/v1/jobs/{job.id}")
    assert admin_res.status_code == 200
    assert admin_res.json()["status"] == "PENDING"

    # A different, non-admin user cannot read it — 404 (not 403), no existence leak.
    other = make_user("u2", role=make_role())
    assert _make_client(db, other).get(f"/api/v1/jobs/{job.id}").status_code == 404

    # The creator can read their own job.
    owner = make_user("owner-uid", role=make_role())
    assert _make_client(db, owner).get(f"/api/v1/jobs/{job.id}").status_code == 200


def test_get_job_404_when_missing(db: Session) -> None:
    assert _make_client(db, _admin()).get("/api/v1/jobs/999999").status_code == 404
