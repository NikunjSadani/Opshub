"""Project creation idempotency (LOW-4): a double-submit / retry with an
identical (client_id, name) must NOT mint a second project and burn a sequence
number. Distinct names still create distinct, sequential projects.

Auth + DB are dependency-overridden (no Firebase / Postgres).
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
from app.modules.projects import service
from app.modules.projects.models import Project, ProjectClient
from app.modules.projects.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MODULE_USER = User(
    firebase_uid="mod", email="m@x.com", name="M", role=Role.MIS, active=True,
    module_access=[UserModuleAccess(module_key="projects")],
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[ProjectClient.__table__, Project.__table__, AuditLog.__table__],
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


def _new_client(client: TestClient, name: str, code: str) -> int:
    r = client.post("/api/v1/projects/clients", json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_double_submit_returns_same_project(client: TestClient) -> None:
    """Two identical create calls => one project, sequence not double-burned."""
    bri = _new_client(client, "Britannia", "BRI")
    _as(client, MODULE_USER)

    r1 = client.post("/api/v1/projects", json={"client_id": bri, "name": "Summer Campaign"})
    r2 = client.post("/api/v1/projects", json={"client_id": bri, "name": "Summer Campaign"})
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)

    # Same row, same code — the second call did NOT mint BRI-002.
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["code"] == r2.json()["code"] == "BRI-001"

    # Exactly one project row persisted for this client.
    db = client.app.state.TestSession()
    rows = list(db.execute(select(Project).where(Project.client_id == bri)).scalars())
    db.close()
    assert len(rows) == 1
    assert rows[0].seq == 1  # sequence not burned

    # A subsequent DISTINCT name mints the next number (proves seq wasn't advanced).
    r3 = client.post("/api/v1/projects", json={"client_id": bri, "name": "Winter Campaign"})
    assert r3.status_code == 201, r3.text
    assert r3.json()["code"] == "BRI-002"


def test_distinct_names_create_distinct_projects(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    _as(client, MODULE_USER)
    a = client.post("/api/v1/projects", json={"client_id": bri, "name": "Alpha"})
    b = client.post("/api/v1/projects", json={"client_id": bri, "name": "Beta"})
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["code"] == "BRI-001"
    assert b.json()["code"] == "BRI-002"
    assert a.json()["id"] != b.json()["id"]


def test_same_name_dedupes_per_client_not_across_clients(client: TestClient) -> None:
    """Dedup is scoped to (client_id, name): the same name under a DIFFERENT
    client is a distinct project."""
    bri = _new_client(client, "Britannia", "BRI")
    tat = _new_client(client, "Tata", "TAT")
    _as(client, MODULE_USER)
    r_bri = client.post("/api/v1/projects", json={"client_id": bri, "name": "Launch"})
    r_tat = client.post("/api/v1/projects", json={"client_id": tat, "name": "Launch"})
    assert r_bri.json()["code"] == "BRI-001"
    assert r_tat.json()["code"] == "TAT-001"
    assert r_bri.json()["id"] != r_tat.json()["id"]


def test_service_level_idempotency(client: TestClient) -> None:
    """Direct service call is idempotent too (defense-in-depth for internal callers)."""
    db = client.app.state.TestSession()
    c = service.create_client(db, name="Britannia", code="BRI", actor_uid="adm")
    db.flush()
    p1 = service.create_project(db, client_id=c.id, name="One", actor_uid="adm")
    p2 = service.create_project(db, client_id=c.id, name="One", actor_uid="adm")
    db.commit()
    assert p1.id == p2.id
    assert p1.code == p2.code == "BRI-001"
    count = len(list(db.execute(select(Project).where(Project.client_id == c.id)).scalars()))
    db.close()
    assert count == 1
