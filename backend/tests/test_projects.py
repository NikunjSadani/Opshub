"""Projects module over HTTP: RBAC matrix + per-client sequential id generation.

Auth + DB are dependency-overridden (no Firebase / Postgres). Proves the gates:
Admin-only client registration + status change, module-gated project CRUD, unique
3-letter client codes (case-insensitive), per-client sequential project ids
(BRI-001, BRI-002, TAT-001), filters, and that writes are audited.
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
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)


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


# ------------------------------------------------------------------- clients

def test_client_code_normalized_and_listed(client: TestClient) -> None:
    r = client.post("/api/v1/projects/clients", json={"name": "Britannia", "code": "bri"})
    assert r.status_code == 201, r.text
    assert r.json()["code"] == "BRI"  # normalized to upper
    assert r.json()["active"] is True

    _as(client, MODULE_USER)  # module-gated read
    rows = client.get("/api/v1/projects/clients").json()
    assert [x["code"] for x in rows] == ["BRI"]


def test_client_code_must_be_three_letters(client: TestClient) -> None:
    for bad in ["BR", "BRIX", "12A", "B1I", "b r"]:
        r = client.post("/api/v1/projects/clients", json={"name": "X", "code": bad})
        assert r.status_code == 422, f"{bad} -> {r.status_code}"


def test_client_code_unique_case_insensitive(client: TestClient) -> None:
    assert client.post(
        "/api/v1/projects/clients", json={"name": "Britannia", "code": "BRI"}
    ).status_code == 201
    # 'bri' normalizes to 'BRI' -> duplicate -> 409 (not a fresh row).
    dup = client.post("/api/v1/projects/clients", json={"name": "Other", "code": "bri"})
    assert dup.status_code == 409, dup.text
    dup2 = client.post("/api/v1/projects/clients", json={"name": "Other", "code": "BRI"})
    assert dup2.status_code == 409


def test_create_client_is_admin_only(client: TestClient) -> None:
    _as(client, MODULE_USER)  # has the module grant but is not ADMIN
    r = client.post("/api/v1/projects/clients", json={"name": "Britannia", "code": "BRI"})
    assert r.status_code == 403


def test_list_clients_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/projects/clients").status_code == 403


# ------------------------------------------------------------------ projects

def test_project_codes_are_per_client_sequential(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    tat = _new_client(client, "Tata", "TAT")

    _as(client, MODULE_USER)  # project creation is module-gated, not admin-only
    c1 = client.post("/api/v1/projects", json={"client_id": bri, "name": "Summer Campaign"})
    c2 = client.post("/api/v1/projects", json={"client_id": bri, "name": "Winter Campaign"})
    t1 = client.post("/api/v1/projects", json={"client_id": tat, "name": "Salt Rewards"})
    assert c1.status_code == 201 and c2.status_code == 201 and t1.status_code == 201
    assert c1.json()["code"] == "BRI-001"
    assert c2.json()["code"] == "BRI-002"
    assert t1.json()["code"] == "TAT-001"  # a second client restarts its own sequence
    # denormalized client fields are resolved for the FE
    assert c1.json()["client_code"] == "BRI" and c1.json()["client_name"] == "Britannia"


def test_project_creation_requires_module(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    _as(client, OUTSIDER)
    r = client.post("/api/v1/projects", json={"client_id": bri, "name": "X"})
    assert r.status_code == 403
    assert client.get("/api/v1/projects").status_code == 403


def test_project_unknown_client_404(client: TestClient) -> None:
    r = client.post("/api/v1/projects", json={"client_id": 999, "name": "X"})
    assert r.status_code == 404


def test_project_full_fields_and_get(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    r = client.post("/api/v1/projects", json={
        "client_id": bri, "name": "Q1 Drive", "start_date": "2026-04-01",
        "description": "first quarter",
    })
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    got = client.get(f"/api/v1/projects/{pid}").json()
    assert got["start_date"] == "2026-04-01"
    assert got["description"] == "first quarter"
    assert got["status"] == "ACTIVE"
    assert client.get("/api/v1/projects/999999").status_code == 404


# --------------------------------------------------------------- status patch

def test_status_patch_admin_only_and_validated(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    pid = client.post("/api/v1/projects", json={"client_id": bri, "name": "X"}).json()["id"]

    # ADMIN can change status
    r = client.patch(f"/api/v1/projects/{pid}", json={"status": "ON_HOLD"})
    assert r.status_code == 200 and r.json()["status"] == "ON_HOLD"

    # invalid status -> 422
    bad = client.patch(f"/api/v1/projects/{pid}", json={"status": "PAUSED"})
    assert bad.status_code == 422

    # module user (non-admin) is forbidden
    _as(client, MODULE_USER)
    forbidden = client.patch(f"/api/v1/projects/{pid}", json={"status": "CLOSED"})
    assert forbidden.status_code == 403


# ------------------------------------------------------------------- filters

def test_project_filters(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    tat = _new_client(client, "Tata", "TAT")
    p_bri = client.post(
        "/api/v1/projects", json={"client_id": bri, "name": "Alpha Launch"}
    ).json()["id"]
    client.post("/api/v1/projects", json={"client_id": tat, "name": "Beta Rollout"})
    client.patch(f"/api/v1/projects/{p_bri}", json={"status": "CLOSED"})

    # by client_id
    rows = client.get(f"/api/v1/projects?client_id={bri}").json()
    assert [x["code"] for x in rows] == ["BRI-001"]
    # by status
    closed = client.get("/api/v1/projects?status=CLOSED").json()
    assert [x["code"] for x in closed] == ["BRI-001"]
    # by q (name/code contains, case-insensitive)
    hits = client.get("/api/v1/projects?q=beta").json()
    assert [x["name"] for x in hits] == ["Beta Rollout"]
    by_code = client.get("/api/v1/projects?q=tat-001").json()
    assert [x["code"] for x in by_code] == ["TAT-001"]
    # newest first
    allrows = client.get("/api/v1/projects").json()
    assert [x["code"] for x in allrows] == ["TAT-001", "BRI-001"]


def test_project_q_escapes_like_wildcards(client: TestClient) -> None:
    """F6: a literal `%`/`_` in `q` is matched LITERALLY, not as a LIKE wildcard —
    a bare `%` must NOT match every project."""
    bri = _new_client(client, "Britannia", "BRI")
    client.post("/api/v1/projects", json={"client_id": bri, "name": "Alpha Launch"})
    client.post("/api/v1/projects", json={"client_id": bri, "name": "Beta Rollout"})
    # No name/code contains a literal '%', so an escaped '%' matches nothing.
    assert client.get("/api/v1/projects?q=%25").json() == []
    # '_' is likewise literal, not a single-char wildcard.
    assert client.get("/api/v1/projects?q=_").json() == []


# --------------------------------------------------------------------- audit

def test_writes_are_audited(client: TestClient) -> None:
    bri = _new_client(client, "Britannia", "BRI")
    pid = client.post("/api/v1/projects", json={"client_id": bri, "name": "X"}).json()["id"]
    client.patch(f"/api/v1/projects/{pid}", json={"status": "CLOSED"})
    db = client.app.state.TestSession()
    actions = {a.action for a in db.execute(select(AuditLog)).scalars()}
    db.close()
    assert {"project.client_created", "project.created", "project.status_changed"} <= actions


# ------------------------------------------------------- sequential @ service

def test_service_sequential_distinct_codes(client: TestClient) -> None:
    """Two projects for one client get distinct, sequential codes (backstop check)."""
    db = client.app.state.TestSession()
    c = service.create_client(db, name="Britannia", code="BRI", actor_uid="adm")
    db.flush()
    p1 = service.create_project(db, client_id=c.id, name="One", actor_uid="adm")
    p2 = service.create_project(db, client_id=c.id, name="Two", actor_uid="adm")
    db.commit()
    assert p1.code == "BRI-001"
    assert p2.code == "BRI-002"
    assert p1.seq != p2.seq
    db.close()


def test_service_rejects_overlong_name(client: TestClient) -> None:
    """Service-level name cap (defense-in-depth beyond the route's pydantic 200)."""
    db = client.app.state.TestSession()
    c = service.create_client(db, name="Britannia", code="BRI", actor_uid="adm")
    db.flush()
    with pytest.raises(service.ProjectError):
        service.create_client(db, name="X" * 201, code="ZZZ", actor_uid="adm")
    with pytest.raises(service.ProjectError):
        service.create_project(db, client_id=c.id, name="Y" * 201, actor_uid="adm")
    db.close()
