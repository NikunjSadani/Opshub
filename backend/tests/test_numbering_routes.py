"""Numbering operator surface over HTTP: RBAC gates + seed/register/void flow.

Auth + DB are dependency-overridden (no Firebase / Postgres). Proves the gates
that keep the sequence safe: only Admin may seed or void; reads need the module
grant; every mutation lands in the audit log.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.numbering.models import NumberingAllocation, NumberingCounter
from app.modules.numbering.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MIS = User(
    firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
    module_access=[UserModuleAccess(module_key="document_automation")],
)
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)


@pytest.fixture
def client() -> Iterator[TestClient]:
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
    app.dependency_overrides[current_user] = lambda: ADMIN
    app.state.TestSession = TestSession
    yield TestClient(app)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


# --------------------------------------------------------------------- seed

def test_admin_can_seed_then_register_reflects_it(client: TestClient) -> None:
    r = client.post(
        "/api/v1/numbering/seed", json={"series": "L", "fy": "26-27", "last_number": 188}
    )
    assert r.status_code == 200, r.text
    assert r.json()["last_number"] == 188

    counters = client.get("/api/v1/numbering/counters").json()
    assert {"series": "L", "fy": "26-27", "last_number": 188} in counters
    # The seed was audited.
    db = client.app.state.TestSession()
    actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
    db.close()
    assert "numbering.seed" in actions


def test_non_admin_cannot_seed(client: TestClient) -> None:
    _as(client, MIS)
    r = client.post("/api/v1/numbering/seed", json={"series": "L", "last_number": 5})
    assert r.status_code == 403


def test_seed_below_floor_conflicts(client: TestClient) -> None:
    base = {"series": "L", "fy": "26-27"}
    client.post("/api/v1/numbering/seed", json={**base, "last_number": 100})
    r = client.post("/api/v1/numbering/seed", json={**base, "last_number": 50})
    assert r.status_code == 409


# --------------------------------------------------------------------- reads

def test_reads_require_module_access(client: TestClient) -> None:
    _as(client, OUTSIDER)  # OPERATIONS, no document_automation grant
    assert client.get("/api/v1/numbering/counters").status_code == 403
    assert client.get("/api/v1/numbering/allocations").status_code == 403
    _as(client, MIS)  # has the grant
    assert client.get("/api/v1/numbering/counters").status_code == 200


# --------------------------------------------------------------------- void

def test_admin_void_marks_and_audits(client: TestClient) -> None:
    # Seed an allocation directly, then void it via the API.
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(
        series="L", fy="26-27", number=1, formatted="GIF/DC/26-27/L/000001", status="ISSUED",
    )
    db.add(alloc)
    db.commit()
    alloc_id = alloc.id
    db.close()

    r = client.post(
        f"/api/v1/numbering/allocations/{alloc_id}/void",
        json={"reason": "wrong consignee"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "VOID"
    assert r.json()["void_reason"] == "wrong consignee"


def test_non_admin_cannot_void(client: TestClient) -> None:
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(
        series="L", fy="26-27", number=2, formatted="GIF/DC/26-27/L/000002", status="ISSUED",
    )
    db.add(alloc)
    db.commit()
    alloc_id = alloc.id
    db.close()

    _as(client, MIS)
    r = client.post(f"/api/v1/numbering/allocations/{alloc_id}/void", json={"reason": "x"})
    assert r.status_code == 403


def test_void_missing_is_404(client: TestClient) -> None:
    r = client.post("/api/v1/numbering/allocations/99999/void", json={"reason": "x"})
    assert r.status_code == 404
