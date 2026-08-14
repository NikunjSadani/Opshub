"""Consignee-party golden-record master: service + route behaviour.

Service: resolve_or_create auto-creates an unknown GSTIN (source UPLOAD, no
deviations); a known GSTIN with cosmetic-only diffs (case/whitespace text,
differently-formatted phone) yields NO deviations; genuine name/address/state
diffs are reported (stored vs incoming) without overwriting; a bad-checksum
GSTIN raises; lowercase input is normalized to stored upper.

Routes: admin-only writes (403 for module user), module-gated reads (403
outsider), 409 duplicate GSTIN, 422 bad GSTIN, `q` search.
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
from app.modules.masterdata.consignee_master import (
    ConsigneeMasterError,
    create_party,
    resolve_or_create,
    update_party,
)
from app.modules.masterdata.models import (
    Consignee,
    ConsigneeParty,
    Consignor,
    HsnCode,
    Series,
)
from app.modules.masterdata.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MIS = User(
    firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
    module_access=[UserModuleAccess(module_key="document_automation")],
)
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)

GSTIN_A = "27AAAAA0000A1Z2"  # valid checksum, state 27 = Maharashtra
GSTIN_B = "29AAAAA0000A1ZY"  # valid checksum, state 29 = Karnataka
GSTIN_BADSUM = "27AAAAA0000A1Z9"  # valid state 27 + shape, wrong final check digit


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine, tables=[ConsigneeParty.__table__, AuditLog.__table__]
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Consignor.__table__, Consignee.__table__, ConsigneeParty.__table__,
            HsnCode.__table__, Series.__table__, AuditLog.__table__,
        ],
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _override_db() -> Iterator[Session]:
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

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


# -------------------------------------------------------------- service tests

def test_resolve_unknown_gstin_creates(db: Session) -> None:
    res = resolve_or_create(
        db, gstin=GSTIN_A, name="Acme Foods", state="Maharashtra", actor_uid="u1"
    )
    db.commit()
    assert res.created is True
    assert res.deviations == []
    assert res.party.source == "UPLOAD"
    assert res.party.gstin == GSTIN_A
    actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
    assert "consignee_party.created" in actions


def test_resolve_gstin_normalized_lowercase_to_upper(db: Session) -> None:
    res = resolve_or_create(db, gstin=GSTIN_A.lower(), name="Acme", actor_uid="u1")
    db.commit()
    assert res.party.gstin == GSTIN_A  # stored upper


def test_resolve_known_identical_no_deviations(db: Session) -> None:
    resolve_or_create(
        db, gstin=GSTIN_A, name="Acme Foods", address_line1="12 Main Rd",
        state="Maharashtra", phone="98300-11252", actor_uid="u1",
    )
    db.commit()
    # Case/whitespace text variants + differently formatted phone == NO deviations.
    res = resolve_or_create(
        db, gstin=GSTIN_A, name="  acme   foods ", address_line1="12 MAIN RD",
        state="maharashtra", phone="9830011252", actor_uid="u2",
    )
    assert res.created is False
    assert res.deviations == []


def test_resolve_known_genuine_diffs_reported_not_overwritten(db: Session) -> None:
    resolve_or_create(
        db, gstin=GSTIN_A, name="Acme Foods", address_line1="12 Main Rd",
        state="Maharashtra", actor_uid="u1",
    )
    db.commit()
    res = resolve_or_create(
        db, gstin=GSTIN_A, name="Beta Traders", address_line1="99 Other Ave",
        state="Gujarat", actor_uid="u2",
    )
    assert res.created is False
    fields = {d.field for d in res.deviations}
    assert fields == {"name", "address_line1", "state"}
    by_field = {d.field: d for d in res.deviations}
    assert by_field["name"].stored == "Acme Foods"
    assert by_field["name"].incoming == "Beta Traders"
    # Stored record is NOT overwritten.
    assert res.party.name == "Acme Foods"
    assert res.party.state == "Maharashtra"


def test_resolve_empty_incoming_not_flagged(db: Session) -> None:
    resolve_or_create(
        db, gstin=GSTIN_A, name="Acme Foods", state="Maharashtra", actor_uid="u1"
    )
    db.commit()
    res = resolve_or_create(db, gstin=GSTIN_A, name="", state="", actor_uid="u2")
    assert res.created is False
    assert res.deviations == []


def test_resolve_invalid_checksum_raises(db: Session) -> None:
    with pytest.raises(ConsigneeMasterError):
        resolve_or_create(db, gstin=GSTIN_BADSUM, name="Acme", actor_uid="u1")


# ---------------------------------------------------------------- route tests

def test_admin_create_and_get(client: TestClient) -> None:
    r = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme Foods", "state": "Maharashtra"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"] == "MANUAL"
    assert body["gstin"] == GSTIN_A
    got = client.get(f"/api/v1/masterdata/consignee-parties/{body['id']}")
    assert got.status_code == 200 and got.json()["name"] == "Acme Foods"


def test_create_requires_admin(client: TestClient) -> None:
    _as(client, MIS)
    r = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme"},
    )
    assert r.status_code == 403


def test_reads_require_module_access(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/masterdata/consignee-parties").status_code == 403


def test_module_user_can_read(client: TestClient) -> None:
    client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme Foods"},
    )
    _as(client, MIS)
    rows = client.get("/api/v1/masterdata/consignee-parties").json()
    assert [x["gstin"] for x in rows] == [GSTIN_A]


def test_duplicate_gstin_409(client: TestClient) -> None:
    body = {"gstin": GSTIN_A, "name": "Acme"}
    assert client.post("/api/v1/masterdata/consignee-parties", json=body).status_code == 201
    dup = client.post("/api/v1/masterdata/consignee-parties", json=body)
    assert dup.status_code == 409


def test_bad_gstin_422(client: TestClient) -> None:
    r = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_BADSUM, "name": "Acme"},
    )
    assert r.status_code == 422


def test_patch_updates_fields(client: TestClient) -> None:
    rid = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme", "state": "Maharashtra"},
    ).json()["id"]
    r = client.patch(
        f"/api/v1/masterdata/consignee-parties/{rid}",
        json={"name": "Acme Renamed"},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Acme Renamed"
    assert r.json()["gstin"] == GSTIN_A  # unchanged


def test_patch_requires_admin(client: TestClient) -> None:
    rid = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme"},
    ).json()["id"]
    _as(client, MIS)
    r = client.patch(
        f"/api/v1/masterdata/consignee-parties/{rid}", json={"name": "X"}
    )
    assert r.status_code == 403


def test_q_search(client: TestClient) -> None:
    client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme Foods"},
    )
    client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_B, "name": "Beta Traders"},
    )
    by_name = client.get("/api/v1/masterdata/consignee-parties?q=beta").json()
    assert [x["gstin"] for x in by_name] == [GSTIN_B]
    by_gstin = client.get("/api/v1/masterdata/consignee-parties?q=27aaaaa").json()
    assert [x["name"] for x in by_gstin] == ["Acme Foods"]


# ------------------------------------------- audit fixes (gstin<->state, no-op)

def test_state_mismatch_rejected_on_create(db: Session) -> None:
    # GSTIN_A state code 27 = Maharashtra; a RECOGNIZED different state is rejected
    # (a golden record can't store a self-contradictory GSTIN/state pair).
    with pytest.raises(ConsigneeMasterError):
        resolve_or_create(db, gstin=GSTIN_A, name="Acme", state="Gujarat", actor_uid="u1")
    with pytest.raises(ConsigneeMasterError):
        create_party(db, gstin=GSTIN_A, name="Acme", state="Karnataka", actor_uid="u1")
    # Lenient: an UNRECOGNIZED/abbreviated state name passes.
    res = resolve_or_create(db, gstin=GSTIN_A, name="Acme", state="MH", actor_uid="u1")
    assert res.created is True


def test_state_mismatch_on_known_gstin_is_a_deviation_not_an_error(db: Session) -> None:
    # A mismatched state on an EXISTING gstin is surfaced as a deviation, not raised.
    resolve_or_create(db, gstin=GSTIN_A, name="Acme", state="Maharashtra", actor_uid="u1")
    res = resolve_or_create(db, gstin=GSTIN_A, name="Acme", state="Gujarat", actor_uid="u2")
    assert res.created is False
    assert any(d.field == "state" for d in res.deviations)


def test_post_state_mismatch_422(client: TestClient) -> None:
    r = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme", "state": "Gujarat"},
    )
    assert r.status_code == 422


def test_noop_update_does_not_audit_or_bump(db: Session) -> None:
    party = create_party(db, gstin=GSTIN_A, name="Acme Foods", state="Maharashtra",
                         phone="9900000000", actor_uid="u1")
    db.flush()
    before = len(list(db.execute(select(AuditLog)).scalars()))
    # Re-submit identical values (collapse/format-equivalent) -> no change.
    update_party(db, party=party, name="  Acme   Foods ", state="Maharashtra",
                 phone="9900000000", actor_uid="u2")
    after = len(list(db.execute(select(AuditLog)).scalars()))
    assert after == before  # no consignee_party.updated row for a no-op
    assert party.updated_by == "u1"  # updated_by not bumped by the no-op


def test_update_party_rejects_gstin_state_contradiction(db: Session) -> None:
    # A golden record whose state contradicts its GSTIN state code would be
    # snapshotted onto a statutory challan (e.g. "Gujarat (27)"); the update path
    # must enforce the same GSTIN<->state consistency as create.
    party = create_party(db, gstin=GSTIN_A, name="Acme", state="Maharashtra", actor_uid="u1")
    db.flush()
    with pytest.raises(ConsigneeMasterError):
        update_party(db, party=party, state="Gujarat", actor_uid="u2")  # 27 != Gujarat(24)


def test_update_party_allows_matching_state(db: Session) -> None:
    party = create_party(db, gstin=GSTIN_A, name="Acme", state="Maharashtra", actor_uid="u1")
    db.flush()
    update_party(db, party=party, state="Maharashtra", name="Acme Two", actor_uid="u2")
    assert party.name == "Acme Two" and party.state == "Maharashtra"


def test_patch_state_contradiction_422(client: TestClient) -> None:
    created = client.post(
        "/api/v1/masterdata/consignee-parties",
        json={"gstin": GSTIN_A, "name": "Acme", "state": "Maharashtra"},
    ).json()
    r = client.patch(
        f"/api/v1/masterdata/consignee-parties/{created['id']}",
        json={"state": "Gujarat"},
    )
    assert r.status_code == 422
