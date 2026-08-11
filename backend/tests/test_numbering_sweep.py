"""Secret-gated reconcile-sweep endpoint (POST /numbering/sweep).

The sweep is called by a scheduler, not a logged-in user, so it authenticates on
the `X-Sweep-Secret` shared secret instead of `current_user`. These tests prove
the fail-closed gate (503 when unconfigured, 403 on a bad header) and that the
right header voids only the stale reservation.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.db import Base, get_db
from app.modules.numbering import service
from app.modules.numbering.models import NumberingAllocation, NumberingCounter
from app.modules.numbering.routes import router
from app.platform.models import AuditLog


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("SWEEP_SECRET", raising=False)
    get_settings.cache_clear()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            NumberingCounter.__table__, NumberingAllocation.__table__, AuditLog.__table__
        ],
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.state.TestSession = TestSession
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_sweep_disabled_without_secret(client: TestClient) -> None:
    # No SWEEP_SECRET configured -> fail closed.
    r = client.post("/api/v1/numbering/sweep", headers={"X-Sweep-Secret": "anything"})
    assert r.status_code == 503, r.text


def test_sweep_rejects_wrong_or_missing_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    try:
        # Wrong header.
        r = client.post("/api/v1/numbering/sweep", headers={"X-Sweep-Secret": "nope"})
        assert r.status_code == 403, r.text
        # Missing header.
        r = client.post("/api/v1/numbering/sweep")
        assert r.status_code == 403, r.text
    finally:
        get_settings.cache_clear()


def test_sweep_voids_only_stale_reservation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    try:
        db = client.app.state.TestSession()
        service.seed_series(db, "L", fy="26-27", last_number=0)
        stale = service.allocate(db, "L", fy="26-27")
        fresh = service.allocate(db, "L", fy="26-27")
        # Backdate the first reservation well past the 24h cutoff.
        stale.reserved_at = datetime.now(UTC) - timedelta(hours=48)
        db.commit()
        stale_id, fresh_id = stale.id, fresh.id
        db.close()

        r = client.post("/api/v1/numbering/sweep", headers={"X-Sweep-Secret": "s3cret"})
        assert r.status_code == 200, r.text
        assert r.json() == {"swept": 1}

        db = client.app.state.TestSession()
        assert db.get(NumberingAllocation, stale_id).status == "VOID"
        assert db.get(NumberingAllocation, fresh_id).status == "RESERVED"
        db.close()
    finally:
        get_settings.cache_clear()


def test_sweep_respects_older_than_hours_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    try:
        db = client.app.state.TestSession()
        service.seed_series(db, "L", fy="26-27", last_number=0)
        alloc = service.allocate(db, "L", fy="26-27")
        alloc.reserved_at = datetime.now(UTC) - timedelta(hours=48)
        db.commit()
        alloc_id = alloc.id
        db.close()

        # A 72h window is wider than the 48h age -> nothing swept.
        r = client.post(
            "/api/v1/numbering/sweep",
            headers={"X-Sweep-Secret": "s3cret"},
            json={"older_than_hours": 72},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"swept": 0}

        db = client.app.state.TestSession()
        assert db.get(NumberingAllocation, alloc_id).status == "RESERVED"
        db.close()
    finally:
        get_settings.cache_clear()
