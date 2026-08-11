"""Aggregation surface for the challan register: GET /challan/summary.

Rows are inserted directly (no render) for deterministic, value-exact
assertions. Covers the money rules: VOID never contributes value, value-free
(NULL total_paise) ISSUED rows count as issued but not valued and add 0, the
eway/valued counts are ISSUED-only, the by_series breakdown groups + orders
(fy DESC, series ASC) and respects the same filter, and an empty DB is all
zeros with total_value_paise == 0 (never NULL).
"""
from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.challan.models import Challan, ChallanStatus
from app.modules.challan.routes import router
from app.platform.auth import current_user
from app.platform.models import Role, User, UserModuleAccess

GSTIN = "27AAAAA0000A1Z5"
MIS = User(firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
           module_access=[UserModuleAccess(module_key="document_automation")])
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)

_seq = itertools.count(1)


@pytest.fixture
def client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: MIS
    app.state.TestSession = TestSession
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _add(
    session: Session,
    *,
    series: str = "L",
    fy: str = "26-27",
    status: str = ChallanStatus.ISSUED.value,
    total_paise: int | None = None,
    eway: bool = False,
) -> None:
    n = next(_seq)
    session.add(Challan(
        batch_id=1, allocation_id=n, number=f"GIF/DC/{fy}/{series}/{n:06d}",
        series=series, fy=fy, number_int=n, challan_date=date(2026, 5, 15),
        consignor_name="C", consignor_gstin=GSTIN, consignor_state="MH",
        consignee_brand="Deoleo", consignee_name="D", consignee_gstin=GSTIN,
        consignee_state="MH", ship_to_name="S", ship_to_address="A", ship_to_state="MH",
        eway_required=eway, total_paise=total_paise, status=status,
    ))


def _summary(client: TestClient, **params: str) -> dict[str, object]:
    r = client.get("/api/v1/challan/summary", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------- tests

def test_summary_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/challan/summary").status_code == 403
    _as(client, MIS)
    assert client.get("/api/v1/challan/summary").status_code == 200


def test_void_excluded_from_value_and_issued_count(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=10000)
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=5000)
    _add(db, status=ChallanStatus.VOID.value, total_paise=99999)  # never counts value
    db.commit()
    db.close()

    body = _summary(client)
    assert body["issued_count"] == 2
    assert body["void_count"] == 1
    assert body["total_value_paise"] == 15000  # VOID 99999 excluded
    assert body["valued_count"] == 2


def test_value_free_issued_counts_but_adds_no_value(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=7000)
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=None)  # value-free
    db.commit()
    db.close()

    body = _summary(client)
    assert body["issued_count"] == 2       # value-free row still an issued challan
    assert body["valued_count"] == 1       # ...but not a valued one
    assert body["total_value_paise"] == 7000  # contributes 0


def test_eway_count_is_issued_only(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=100, eway=True)
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=100, eway=True)
    _add(db, status=ChallanStatus.ISSUED.value, total_paise=100, eway=False)
    _add(db, status=ChallanStatus.VOID.value, total_paise=100, eway=True)  # VOID eway ignored
    db.commit()
    db.close()

    body = _summary(client)
    assert body["eway_count"] == 2


def test_by_series_groups_and_orders(client: TestClient) -> None:
    db = client.app.state.TestSession()
    # two FYs, two series; expect order fy DESC then series ASC
    _add(db, series="L", fy="25-26", total_paise=100)
    _add(db, series="M", fy="25-26", total_paise=200)
    _add(db, series="L", fy="26-27", total_paise=1000)
    _add(db, series="L", fy="26-27", total_paise=2000)
    _add(db, series="L", fy="26-27", status=ChallanStatus.VOID.value, total_paise=500)
    db.commit()
    db.close()

    body = _summary(client)
    rows = body["by_series"]
    keys = [(r["fy"], r["series"]) for r in rows]
    assert keys == [("26-27", "L"), ("25-26", "L"), ("25-26", "M")]

    top = rows[0]
    assert top == {"series": "L", "fy": "26-27", "issued": 2, "void": 1,
                   "total_value_paise": 3000}  # VOID 500 excluded from value


def test_filters_narrow_top_level_and_breakdown(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, series="L", fy="26-27", total_paise=1000)
    _add(db, series="M", fy="26-27", total_paise=2000)
    _add(db, series="L", fy="25-26", total_paise=4000)
    db.commit()
    db.close()

    # series filter is normalized strip().upper()
    body = _summary(client, series=" l ")
    assert body["issued_count"] == 2  # both L rows across FYs
    assert body["total_value_paise"] == 5000
    assert [(r["fy"], r["series"]) for r in body["by_series"]] == \
        [("26-27", "L"), ("25-26", "L")]

    # fy filter (verbatim)
    body = _summary(client, fy="26-27")
    assert body["issued_count"] == 2
    assert body["total_value_paise"] == 3000

    # combined
    body = _summary(client, series="L", fy="26-27")
    assert body["issued_count"] == 1
    assert body["total_value_paise"] == 1000
    assert len(body["by_series"]) == 1


def test_empty_db_all_zeros(client: TestClient) -> None:
    body = _summary(client)
    assert body == {
        "issued_count": 0,
        "void_count": 0,
        "eway_count": 0,
        "valued_count": 0,
        "total_value_paise": 0,
        "by_series": [],
    }
