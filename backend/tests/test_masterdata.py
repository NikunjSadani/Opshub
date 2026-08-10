"""Master-data CRUD over HTTP: RBAC matrix + uniqueness + validation + audit.

Auth + DB are dependency-overridden (no Firebase / Postgres). Proves the gates:
Admin-only writes (masterdata.edit), module-gated reads, unique keys, GSTIN
shape, soft-disable, and that every write is audited.
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
from app.modules.masterdata.models import Consignee, Consignor, HsnCode, Series
from app.modules.masterdata.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MIS = User(
    firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
    module_access=[UserModuleAccess(module_key="document_automation")],
)
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)

GSTIN_A = "27AAAAA0000A1Z5"
GSTIN_B = "29BBBBB0000B1Z5"


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Consignor.__table__, Consignee.__table__, HsnCode.__table__,
            Series.__table__, AuditLog.__table__,
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


# ------------------------------------------------------------------ consignor

def test_admin_create_and_list_consignor(client: TestClient) -> None:
    r = client.post(
        "/api/v1/masterdata/consignor",
        json={"name": "Gifsy Depot", "gstin": GSTIN_A, "state": "West Bengal"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["active"] is True

    _as(client, MIS)  # module-gated read
    rows = client.get("/api/v1/masterdata/consignor").json()
    assert [x["name"] for x in rows] == ["Gifsy Depot"]


def test_non_admin_cannot_write(client: TestClient) -> None:
    _as(client, MIS)
    r = client.post(
        "/api/v1/masterdata/consignor",
        json={"name": "X", "gstin": GSTIN_A, "state": "WB"},
    )
    assert r.status_code == 403


def test_reads_require_module_access(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/masterdata/consignor").status_code == 403
    assert client.get("/api/v1/masterdata/hsn").status_code == 403


def test_gstin_shape_validated(client: TestClient) -> None:
    r = client.post(
        "/api/v1/masterdata/consignor",
        json={"name": "Bad", "gstin": "NOTAGSTIN", "state": "WB"},
    )
    assert r.status_code == 422


def test_writes_are_audited(client: TestClient) -> None:
    client.post(
        "/api/v1/masterdata/consignor",
        json={"name": "Gifsy Depot", "gstin": GSTIN_A, "state": "WB"},
    )
    db = client.app.state.TestSession()
    actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
    db.close()
    assert "masterdata.create" in actions


def test_soft_disable_consignor(client: TestClient) -> None:
    rid = client.post(
        "/api/v1/masterdata/consignor",
        json={"name": "Gifsy Depot", "gstin": GSTIN_A, "state": "WB"},
    ).json()["id"]
    r = client.post(f"/api/v1/masterdata/consignor/{rid}/active", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    active_rows = client.get("/api/v1/masterdata/consignor?active=true").json()
    assert active_rows == []


# ------------------------------------------------------------------ consignee

def test_consignee_brand_state_unique(client: TestClient) -> None:
    body = {"brand": "Deoleo", "state": "Maharashtra", "name": "Deoleo MH",
            "gstin": GSTIN_A, "address": "Mumbai"}
    assert client.post("/api/v1/masterdata/consignee", json=body).status_code == 201
    dup = client.post("/api/v1/masterdata/consignee", json=body)
    assert dup.status_code == 409
    # Same brand, different state is allowed.
    body2 = {**body, "state": "Karnataka", "gstin": GSTIN_B}
    assert client.post("/api/v1/masterdata/consignee", json=body2).status_code == 201


def test_consignee_case_and_space_variants_rejected(client: TestClient) -> None:
    """Regression: case/whitespace variants of one logical key can't both persist,
    so consignee resolution (and the snapshotted GSTIN) is never ambiguous."""
    base = {"name": "Deoleo MH", "gstin": GSTIN_A}
    assert client.post("/api/v1/masterdata/consignee",
                       json={**base, "brand": "Deoleo", "state": "Maharashtra"}).status_code == 201
    variants = [("deoleo", "maharashtra"), ("Deoleo", "Maharashtra "), ("DEOLEO", "MAHARASHTRA")]
    for brand, state in variants:
        r = client.post("/api/v1/masterdata/consignee",
                        json={**base, "brand": brand, "state": state})
        assert r.status_code == 409, f"{brand}/{state} -> {r.status_code}"


def test_gstin_state_code_validated(client: TestClient) -> None:
    # State code "00" is not a real GST state code.
    r = client.post("/api/v1/masterdata/consignor",
                    json={"name": "X", "gstin": "00AAAAA0000A1Z5", "state": "WB"})
    assert r.status_code == 422


def test_gst_rate_scale_rejected(client: TestClient) -> None:
    r = client.post("/api/v1/masterdata/hsn", json={"hsn": "1509", "gst_rate": "12.999"})
    assert r.status_code == 422


def test_consignee_filter_by_brand_state(client: TestClient) -> None:
    for st, g in [("Maharashtra", GSTIN_A), ("Karnataka", GSTIN_B)]:
        client.post("/api/v1/masterdata/consignee", json={
            "brand": "Deoleo", "state": st, "name": f"Deoleo {st}", "gstin": g})
    rows = client.get("/api/v1/masterdata/consignee?brand=Deoleo&state=Karnataka").json()
    assert len(rows) == 1 and rows[0]["state"] == "Karnataka"


# ----------------------------------------------------------------------- hsn

def test_hsn_unique_and_rate(client: TestClient) -> None:
    r = client.post("/api/v1/masterdata/hsn", json={"hsn": "1509", "gst_rate": "5"})
    assert r.status_code == 201
    assert r.json()["gst_rate"] == "5.00"
    dup = client.post("/api/v1/masterdata/hsn", json={"hsn": "1509", "gst_rate": "5"})
    assert dup.status_code == 409


def test_hsn_rate_bounds(client: TestClient) -> None:
    over = client.post("/api/v1/masterdata/hsn", json={"hsn": "1509", "gst_rate": "150"})
    assert over.status_code == 422
    bad = client.post("/api/v1/masterdata/hsn", json={"hsn": "abc", "gst_rate": "5"})
    assert bad.status_code == 422


# -------------------------------------------------------------------- series

def test_series_upper_normalized_and_unique(client: TestClient) -> None:
    r = client.post("/api/v1/masterdata/series", json={"letter": "l", "label": "Legacy"})
    assert r.status_code == 201 and r.json()["letter"] == "L"
    assert client.post("/api/v1/masterdata/series", json={"letter": "L"}).status_code == 409
