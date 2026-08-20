"""RBAC v2 level ladder enforced end-to-end at the route layer.

Proves the View < Operate < Manage ladder on the real mounted routers:
  * a VIEW-only user is 403 on OPERATE actions (generate / create-project / upload);
  * an OPERATE user is 403 on MANAGE actions (void / masterdata-edit);
  * a MANAGE user succeeds (and, being >= Operate, also clears operate actions).

Every user holds the SAME level across all three modules, so each test isolates the
level check itself — not which module was granted.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.modules.challan.routes import router as challan_router
from app.modules.expense.routes import router as expense_router
from app.modules.masterdata.routes import router as masterdata_router
from app.modules.projects import service as projects_service
from app.modules.projects.routes import router as projects_router
from app.platform.auth import current_user
from app.platform.models import Level, User
from tests.rbac_util import make_role, make_user

_MODULES = ("document_automation", "projects", "expense_invoice")


def _user_at(level: Level) -> User:
    levels = dict.fromkeys(_MODULES, level)
    return make_user(f"lvl-{level.value}", role=make_role(module_levels=levels))


VIEW = _user_at(Level.VIEW)
OPERATE = _user_at(Level.OPERATE)
MANAGE = _user_at(Level.MANAGE)


@pytest.fixture
def client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    seed = TestSession()
    proj_client = projects_service.create_client(
        seed, name="Britannia", code="BRI", actor_uid="seed")
    seed.commit()
    client_id = proj_client.id
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    for router in (challan_router, projects_router, expense_router, masterdata_router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: VIEW
    app.state.client_id = client_id
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


# ------------------------------------------------- VIEW is 403 on OPERATE actions

def test_view_forbidden_on_generate(client: TestClient) -> None:
    _as(client, VIEW)
    r = client.post("/api/v1/challan/batches/1/generate", json={"series": "L"})
    assert r.status_code == 403, r.text


def test_view_forbidden_on_create_project(client: TestClient) -> None:
    _as(client, VIEW)
    r = client.post("/api/v1/projects",
                    json={"client_id": client.app.state.client_id, "name": "P1"})
    assert r.status_code == 403, r.text


def test_view_forbidden_on_expense_upload(client: TestClient) -> None:
    _as(client, VIEW)
    r = client.post("/api/v1/expense/invoices",
                    files={"files": ("a.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 403, r.text


# ---------------------------------------------- OPERATE is 403 on MANAGE actions

def test_operate_forbidden_on_void(client: TestClient) -> None:
    _as(client, OPERATE)
    r = client.post("/api/v1/challan/challans/1/void", json={"reason": "x"})
    assert r.status_code == 403, r.text


def test_operate_forbidden_on_masterdata_edit(client: TestClient) -> None:
    _as(client, OPERATE)
    r = client.post("/api/v1/masterdata/series", json={"letter": "Z", "label": "Zed"})
    assert r.status_code == 403, r.text


# --------------------------------------------- OPERATE clears its own operate action

def test_operate_can_create_project(client: TestClient) -> None:
    _as(client, OPERATE)
    r = client.post("/api/v1/projects",
                    json={"client_id": client.app.state.client_id, "name": "Ops Project"})
    assert r.status_code == 201, r.text


# ------------------------------------------------------------- MANAGE succeeds

def test_manage_can_edit_masterdata(client: TestClient) -> None:
    _as(client, MANAGE)
    r = client.post("/api/v1/masterdata/series", json={"letter": "Z", "label": "Zed"})
    assert r.status_code == 201, r.text
    assert r.json()["letter"] == "Z"


def test_manage_can_create_project(client: TestClient) -> None:
    # MANAGE >= OPERATE, so an operate action is also allowed.
    _as(client, MANAGE)
    r = client.post("/api/v1/projects",
                    json={"client_id": client.app.state.client_id, "name": "Mgr Project"})
    assert r.status_code == 201, r.text
