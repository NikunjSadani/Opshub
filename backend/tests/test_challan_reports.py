"""Register date-range filtering + CSV export: GET /challan/challans(.csv).

Rows are inserted directly (no render) for deterministic assertions. Covers the
inclusive date_from/date_to bounds on the register, and the CSV export's shape
(content-type, attachment header, header row, one data row per in-scope challan),
its money formatting (2dp rupees vs an empty cell for a value-free challan), the
spreadsheet-injection guard on text-derived cells, and the module gate.
"""
from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date

import httpx
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
from app.platform.models import Level, User
from tests.rbac_util import make_role, make_user

GSTIN = "27AAAAA0000A1Z5"
MIS = make_user("mis", role=make_role(module_levels={"document_automation": Level.OPERATE}))
OUTSIDER = make_user("out", role=make_role())

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
    challan_date: date = date(2026, 5, 15),
    project_code: str = "BRI-001",
    consignee_name: str = "Deoleo MH",
    ship_to_state: str = "MH",
) -> int:
    n = next(_seq)
    session.add(Challan(
        batch_id=1, allocation_id=n, number=f"GIF/DC/{fy}/{series}/{n:06d}",
        series=series, fy=fy, number_int=n, challan_date=challan_date, project_code=project_code,
        consignor_name="C", consignor_gstin=GSTIN, consignor_state="MH",
        consignee_name=consignee_name, consignee_gstin=GSTIN,
        consignee_state="MH", ship_to_name="S", ship_to_address="A", ship_to_state=ship_to_state,
        eway_required=eway, total_paise=total_paise, status=status,
    ))
    return n


def _list(client: TestClient, **params: str) -> list[dict[str, object]]:
    r = client.get("/api/v1/challan/challans", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _csv(client: TestClient, **params: str) -> httpx.Response:
    return client.get("/api/v1/challan/challans.csv", params=params)


# --------------------------------------------------------------------- tests

def test_date_range_narrows_register_inclusive(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, challan_date=date(2026, 5, 1))
    _add(db, challan_date=date(2026, 5, 15))
    _add(db, challan_date=date(2026, 5, 31))
    db.commit()
    db.close()

    # inclusive bounds: both endpoints are IN range
    body = _list(client, date_from="2026-05-01", date_to="2026-05-15")
    dates = sorted(c["challan_date"] for c in body)
    assert dates == ["2026-05-01", "2026-05-15"]  # 05-31 excluded

    # lower bound only
    assert len(_list(client, date_from="2026-05-15")) == 2
    # upper bound only
    assert len(_list(client, date_to="2026-05-14")) == 1
    # a window that excludes the exact endpoint
    assert len(_list(client, date_from="2026-05-16", date_to="2026-05-30")) == 0


def test_csv_export_shape_and_filters(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, series="L", total_paise=10000)
    _add(db, series="M", total_paise=20000)
    db.commit()
    db.close()

    r = _csv(client)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="challan-register.csv"'

    lines = r.text.strip().split("\n")
    assert lines[0] == (
        "Number,Date,Project ID,Consignee Name,Ship-to State,E-way,Total (INR),Status")
    assert len(lines) == 3  # header + one row per challan

    # the series filter narrows the CSV just like the register
    r = _csv(client, series="M")
    assert len(r.text.strip().split("\n")) - 1 == 1  # one data row after the header


def test_csv_money_formatting(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, total_paise=142506)  # -> 1425.06
    _add(db, total_paise=None)    # value-free -> empty Total cell
    db.commit()
    db.close()

    rows = _csv(client).text.strip().split("\n")[1:]
    # newest-first: value-free row first, then the priced row
    totals = [row.split(",")[6] for row in rows]  # Total (INR) is the 7th column (index 6)
    assert '"1425.06"' in totals
    assert '""' in totals  # empty Total cell for the value-free challan


def test_csv_injection_guard(client: TestClient) -> None:
    db = client.app.state.TestSession()
    # a project id + ship-to state that start with '=' must be neutralized
    _add(db, total_paise=10000, project_code="=cmd()", ship_to_state="=A1")
    db.commit()
    db.close()

    row = _csv(client).text.strip().split("\n")[1]
    assert '"\'=cmd()"' in row    # leading '=' prefixed with a single quote
    assert '"\'=A1"' in row


def test_csv_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert _csv(client).status_code == 403
    _as(client, MIS)
    assert _csv(client).status_code == 200


def test_csv_truncation_is_signalled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An over-cap export must NOT read as complete: a trailing marker row + an
    # X-Truncated header both signal the cut.
    monkeypatch.setattr("app.modules.challan.routes.MAX_CSV_ROWS", 2)
    db = client.app.state.TestSession()
    _add(db, total_paise=10000)
    _add(db, total_paise=10000)
    _add(db, total_paise=10000)
    db.commit()
    db.close()

    r = _csv(client)
    assert r.status_code == 200
    assert r.headers["x-truncated"] == "true"
    lines = [ln for ln in r.text.strip().split("\n") if ln]
    assert len(lines) == 1 + 2 + 1  # header + 2 capped rows + truncation marker
    assert "truncated at 2 rows" in r.text

    # A within-cap export is not marked.
    monkeypatch.setattr("app.modules.challan.routes.MAX_CSV_ROWS", 50)
    r2 = _csv(client)
    assert r2.headers["x-truncated"] == "false"
    assert "truncated at" not in r2.text


def test_csv_quote_doubling_preserves_data(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _add(db, total_paise=10000, consignee_name='ABC "Best" Oils')
    db.commit()
    db.close()

    row = _csv(client).text.strip().split("\n")[1]
    # RFC-4180: an embedded quote is DOUBLED (faithful), not rewritten to a single '.
    assert '"ABC ""Best"" Oils"' in row
    assert "'Best'" not in row
