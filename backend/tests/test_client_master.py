"""Client master (inc 28) over HTTP: the promoted `project_client` and its child
GSTINs / addresses / contacts.

Proves: the extended `ClientOut` (pan / credit_terms_days), client PATCH, add/list
of each child, GSTIN 15-char validation (422) + per-client uniqueness (409), the
"at most one default per client per type" invariant, soft-delete (active=False,
never a hard delete), and that writes need `client.manage` (VIEW is read-only).

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
from app.modules.projects.models import (
    ClientAddress,
    ClientContact,
    ClientGstin,
    Project,
    ProjectClient,
)
from app.modules.projects.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Level, User
from tests.rbac_util import make_role, make_user

ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))
# MANAGE grants client.manage (writes). VIEW is read-only. No grant = no read.
MANAGER = make_user("mgr", role=make_role(module_levels={"projects": Level.MANAGE}))
VIEWER = make_user("viw", role=make_role(module_levels={"projects": Level.VIEW}))
OUTSIDER = make_user("out", role=make_role())

VALID_GSTIN = "27ABCDE1234F1Z5"  # 15 alphanumeric chars; state_code derives to "27"
VALID_GSTIN_2 = "29ABCDE1234F1Z5"


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            ProjectClient.__table__,
            Project.__table__,
            ClientGstin.__table__,
            ClientAddress.__table__,
            ClientContact.__table__,
            AuditLog.__table__,
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


def _new_client(client: TestClient, name: str = "Britannia", code: str = "BRI") -> int:
    r = client.post("/api/v1/projects/clients", json={"name": name, "code": code})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


# ------------------------------------------------------- extended ClientOut

def test_client_out_exposes_pan_and_terms_after_patch(client: TestClient) -> None:
    cid = _new_client(client)
    # freshly-created client: new fields present and null
    created = client.get(f"/api/v1/projects/clients/{cid}").json()
    assert created["pan"] is None and created["credit_terms_days"] is None

    r = client.patch(
        f"/api/v1/projects/clients/{cid}",
        json={"pan": "aaapf1234c", "credit_terms_days": 30},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pan"] == "AAAPF1234C"  # normalized upper
    assert r.json()["credit_terms_days"] == 30

    # list surface exposes the new fields too
    _as(client, VIEWER)
    rows = client.get("/api/v1/projects/clients").json()
    assert rows[0]["pan"] == "AAAPF1234C"
    assert rows[0]["credit_terms_days"] == 30


# --------------------------------------------------- add + list children

def test_add_and_list_gstin_address_contact(client: TestClient) -> None:
    cid = _new_client(client)
    g = client.post(
        f"/api/v1/projects/clients/{cid}/gstins",
        json={"gstin": VALID_GSTIN, "legal_name": "Britannia Industries"},
    )
    assert g.status_code == 201, g.text
    assert g.json()["gstin"] == VALID_GSTIN
    assert g.json()["state_code"] == "27"  # derived from the first 2 digits
    gid = g.json()["id"]

    a = client.post(
        f"/api/v1/projects/clients/{cid}/addresses",
        json={"gstin_id": gid, "label": "HO", "line1": "5 MG Road", "city": "Mumbai"},
    )
    assert a.status_code == 201, a.text
    assert a.json()["gstin_id"] == gid

    c = client.post(
        f"/api/v1/projects/clients/{cid}/contacts",
        json={"name": "Asha Rao", "email": "asha@example.com"},
    )
    assert c.status_code == 201, c.text

    _as(client, VIEWER)  # detail is a module-gated read
    detail = client.get(f"/api/v1/projects/clients/{cid}").json()
    assert [x["gstin"] for x in detail["gstins"]] == [VALID_GSTIN]
    assert [x["line1"] for x in detail["addresses"]] == ["5 MG Road"]
    assert [x["name"] for x in detail["contacts"]] == ["Asha Rao"]


def test_gstin_lowercase_normalized(client: TestClient) -> None:
    cid = _new_client(client)
    r = client.post(
        f"/api/v1/projects/clients/{cid}/gstins",
        json={"gstin": VALID_GSTIN.lower()},
    )
    assert r.status_code == 201, r.text
    assert r.json()["gstin"] == VALID_GSTIN  # upper-cased


# --------------------------------------------------------- validation

def test_gstin_must_be_15_chars(client: TestClient) -> None:
    cid = _new_client(client)
    r = client.post(
        f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": "27ABCDE1234F1Z"}
    )  # 14 chars
    assert r.status_code == 422, r.text


def test_duplicate_gstin_conflicts(client: TestClient) -> None:
    cid = _new_client(client)
    first = client.post(f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN})
    assert first.status_code == 201
    dup = client.post(f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN})
    assert dup.status_code == 409, dup.text


def test_add_child_to_unknown_client_404(client: TestClient) -> None:
    r = client.post("/api/v1/projects/clients/9999/contacts", json={"name": "X"})
    assert r.status_code == 404


# ----------------------------------------------------- one-default rule

def test_one_default_gstin_per_client(client: TestClient) -> None:
    cid = _new_client(client)
    g1 = client.post(
        f"/api/v1/projects/clients/{cid}/gstins",
        json={"gstin": VALID_GSTIN, "is_default": True},
    ).json()
    g2 = client.post(
        f"/api/v1/projects/clients/{cid}/gstins",
        json={"gstin": VALID_GSTIN_2, "is_default": True},
    ).json()

    detail = client.get(f"/api/v1/projects/clients/{cid}").json()
    defaults = {x["id"]: x["is_default"] for x in detail["gstins"]}
    assert defaults[g2["id"]] is True
    assert defaults[g1["id"]] is False  # the earlier default was demoted
    # exactly one default overall
    assert sum(1 for v in defaults.values() if v) == 1


# ------------------------------------------------------------ soft-delete

def test_delete_gstin_is_soft(client: TestClient) -> None:
    cid = _new_client(client)
    gid = client.post(
        f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN}
    ).json()["id"]

    d = client.delete(f"/api/v1/projects/clients/gstins/{gid}")
    assert d.status_code == 200, d.text
    assert d.json()["active"] is False

    # hidden from the detail view ...
    detail = client.get(f"/api/v1/projects/clients/{cid}").json()
    assert detail["gstins"] == []

    # ... but the row still physically exists (soft-delete, not a hard delete)
    db = client.app.state.TestSession()
    row = db.execute(select(ClientGstin).where(ClientGstin.id == gid)).scalar_one()
    db.close()
    assert row.active is False


# ------------------------------------------------------------------ RBAC

def test_viewer_cannot_write_but_can_read(client: TestClient) -> None:
    cid = _new_client(client)
    _as(client, VIEWER)
    # reads OK
    assert client.get(f"/api/v1/projects/clients/{cid}").status_code == 200
    # every write is forbidden for VIEW
    assert client.patch(
        f"/api/v1/projects/clients/{cid}", json={"pan": "AAAPF1234C"}
    ).status_code == 403
    assert client.post(
        f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN}
    ).status_code == 403
    assert client.post(
        f"/api/v1/projects/clients/{cid}/contacts", json={"name": "X"}
    ).status_code == 403


def test_outsider_cannot_read_detail(client: TestClient) -> None:
    cid = _new_client(client)
    _as(client, OUTSIDER)
    assert client.get(f"/api/v1/projects/clients/{cid}").status_code == 403


# --------------------------------------------------------------- audit

def test_client_master_writes_are_audited(client: TestClient) -> None:
    cid = _new_client(client)
    client.patch(f"/api/v1/projects/clients/{cid}", json={"credit_terms_days": 45})
    gid = client.post(
        f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN}
    ).json()["id"]
    client.delete(f"/api/v1/projects/clients/gstins/{gid}")

    db = client.app.state.TestSession()
    actions = {a.action for a in db.execute(select(AuditLog)).scalars()}
    db.close()
    assert {"client.updated", "client.gstin_added", "client.gstin_deactivated"} <= actions


# ------------------------------------------- MANAGER (non-admin) may write

def test_manager_can_manage_children(client: TestClient) -> None:
    cid = _new_client(client)
    _as(client, MANAGER)  # PROJECTS MANAGE, not Administrator
    g = client.post(f"/api/v1/projects/clients/{cid}/gstins", json={"gstin": VALID_GSTIN})
    assert g.status_code == 201, g.text
    a = client.post(
        f"/api/v1/projects/clients/{cid}/addresses", json={"line1": "1 Road"}
    )
    assert a.status_code == 201, a.text
