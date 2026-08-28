"""Challan invoice-access audit log: hashing, aggregation, viewer-side logging,
and the MANAGE-gated dashboard routes.

In-memory sqlite (StaticPool, FK-pragma ON). Three concerns:
  * `hash_ip` / `client_ip_from_request` — None-safe, deterministic, header priority.
  * `summary` / `recent` — aggregation correctness over a hand-seeded mix of outcomes
    across two clients + a null-client row, with a date-range + client filter.
  * viewer logging — POST /d/{token} drives each outcome and records exactly one row;
    an unknown token logs nothing; a logging failure never breaks the response.
  * routes — MANAGE required; summary/recent shapes; recent never leaks viewer_hash.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.billing import models as _billing_models  # noqa: F401  (register tables)
from app.modules.challan import invoice_access
from app.modules.challan.models import (
    AccessOutcome,
    Challan,
    ChallanBatch,
    ChallanInvoiceAccess,
)
from app.modules.challan.routes import router as challan_router
from app.modules.files.models import StoredFile
from app.modules.numbering.models import NumberingAllocation
from app.modules.projects import service as projects_service
from app.modules.public_docs import routes as pub_routes
from app.modules.public_docs import service as pub_service
from app.modules.sales_orders import models as _so_models  # noqa: F401  (register tables)
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.storage import get_storage
from tests.rbac_util import make_role, make_user

PDF_BYTES = b"%PDF-1.4 fake invoice bytes \n%%EOF"

ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))
MANAGER = make_user("mgr", role=make_role(module_levels={"document_automation": Level.MANAGE}))
VIEWER_USER = make_user("vw", role=make_role(module_levels={"document_automation": Level.VIEW}))
OUTSIDER = make_user("out", role=make_role())


# --------------------------------------------------------------------- helpers

def _mk_engine() -> Any:  # noqa: ANN401
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn: Any, _rec: Any) -> None:  # noqa: ANN401
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_challan(
    db: Session, *, token: str, number: str, num_int: int, project_code: str,
    invoice_number: str = "",
) -> Challan:
    batch = ChallanBatch()
    db.add(batch)
    db.flush()
    alloc = NumberingAllocation(
        series="L", fy="26-27", number=num_int, formatted=number, status="ISSUED"
    )
    db.add(alloc)
    db.flush()
    challan = Challan(
        batch_id=batch.id, allocation_id=alloc.id, number=number, series="L",
        fy="26-27", number_int=num_int, challan_date=date(2026, 6, 15),
        project_code=project_code, invoice_number=invoice_number, access_token=token,
        consignor_name="Gifsy", consignor_gstin="27AAACG1234A1Z5",
        consignor_state="Maharashtra", consignee_name="Client Co",
        consignee_gstin="29AABCC1111C1Z0", consignee_state="Karnataka",
        ship_to_name="Client Co", ship_to_address="2 Client Ave", ship_to_state="Karnataka",
    )
    db.add(challan)
    db.flush()
    return challan


def _row(
    challan_id: int, client_id: int | None, outcome: str, when: datetime,
    viewer_hash: str | None,
) -> ChallanInvoiceAccess:
    return ChallanInvoiceAccess(
        challan_id=challan_id, client_id=client_id, outcome=outcome,
        accessed_at=when, viewer_hash=viewer_hash,
    )


# =================================================================== hash_ip

def test_hash_ip_none_and_empty_are_none() -> None:
    assert invoice_access.hash_ip(None) is None
    assert invoice_access.hash_ip("") is None


def test_hash_ip_deterministic_and_distinct() -> None:
    h1 = invoice_access.hash_ip("203.0.113.7")
    assert h1 == invoice_access.hash_ip("203.0.113.7")   # equal IPs -> equal hash
    assert h1 != invoice_access.hash_ip("203.0.113.8")   # different IPs -> different
    assert h1 is not None and len(h1) == 32              # truncated hex


def test_client_ip_prefers_x_client_ip_over_forwarded_over_peer() -> None:
    class _Req:
        def __init__(self, headers: dict[str, str], host: str | None) -> None:
            self.headers = headers
            self.client = type("C", (), {"host": host})() if host else None

    # x-client-ip wins outright.
    req = _Req({"x-client-ip": "1.1.1.1", "x-forwarded-for": "2.2.2.2, 3.3.3.3"}, "9.9.9.9")
    assert invoice_access.client_ip_from_request(req) == "1.1.1.1"
    # Falls back to the FIRST hop of x-forwarded-for.
    req = _Req({"x-forwarded-for": "2.2.2.2, 3.3.3.3"}, "9.9.9.9")
    assert invoice_access.client_ip_from_request(req) == "2.2.2.2"
    # Falls back to the socket peer.
    req = _Req({}, "9.9.9.9")
    assert invoice_access.client_ip_from_request(req) == "9.9.9.9"
    # Nothing available -> None.
    req = _Req({}, None)
    assert invoice_access.client_ip_from_request(req) is None


# =========================================================== aggregation

@pytest.fixture
def agg(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = _mk_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = TestSession()

    ca = projects_service.create_client(db, name="Alpha Ltd", code="AAA", actor_uid="s")
    cb = projects_service.create_client(db, name="Beta Ltd", code="BBB", actor_uid="s")
    db.flush()
    pa = projects_service.create_project(db, client_id=ca.id, name="PA", actor_uid="s")
    pb = projects_service.create_project(db, client_id=cb.id, name="PB", actor_uid="s")
    db.flush()
    cha = _seed_challan(db, token="tA", number="GIF/DC/26-27/L/000001", num_int=1,
                        project_code=pa.code)
    chb = _seed_challan(db, token="tB", number="GIF/DC/26-27/L/000002", num_int=2,
                        project_code=pb.code)
    db.flush()

    d1 = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    d2 = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
    rows = [
        # Day 1, client A
        _row(cha.id, ca.id, AccessOutcome.VIEWED, d1, "h1"),
        _row(cha.id, ca.id, AccessOutcome.VIEWED, d1, "h1"),          # same viewer
        _row(cha.id, ca.id, AccessOutcome.WRONG_PIN, d1, "h2"),
        # Day 2, client B
        _row(chb.id, cb.id, AccessOutcome.VIEWED, d2, "h3"),
        _row(chb.id, cb.id, AccessOutcome.RATE_LIMITED, d2, None),    # null hash
        _row(chb.id, cb.id, AccessOutcome.NO_PIN, d2, "h4"),
        # Day 2, null-client bucket (challan exists, client unresolved)
        _row(cha.id, None, AccessOutcome.NOT_AVAILABLE, d2, "h5"),
        # OUT OF RANGE — must be excluded
        _row(cha.id, ca.id, AccessOutcome.VIEWED, datetime(2026, 7, 1, tzinfo=UTC), "hX"),
        _row(chb.id, cb.id, AccessOutcome.VIEWED, datetime(2026, 9, 5, tzinfo=UTC), "hY"),
    ]
    db.add_all(rows)
    db.commit()
    yield {"db": db, "a_id": ca.id, "b_id": cb.id,
           "a_number": cha.number, "b_number": chb.number}
    db.close()
    engine.dispose()


def test_summary_totals_and_by_outcome(agg: dict[str, Any]) -> None:
    s = invoice_access.summary(
        agg["db"], date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    assert s["total_pin_entries"] == 7            # the two out-of-range rows excluded
    assert s["total_views"] == 3
    assert s["total_not_available"] == 1
    assert s["total_failed"] == 3                 # WRONG_PIN + RATE_LIMITED + NO_PIN
    assert s["approx_viewers"] == 5               # {h1,h2,h3,h4,h5}, null excluded, h1 once
    assert s["by_outcome"] == {
        "VIEWED": 3, "WRONG_PIN": 1, "NOT_AVAILABLE": 1, "RATE_LIMITED": 1, "NO_PIN": 1,
    }


def test_summary_by_client_grouping(agg: dict[str, Any]) -> None:
    s = invoice_access.summary(
        agg["db"], date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    by_client = {row["client_id"]: row for row in s["by_client"]}
    assert set(by_client) == {agg["a_id"], agg["b_id"], None}

    a = by_client[agg["a_id"]]
    assert (a["client_name"], a["client_code"]) == ("Alpha Ltd", "AAA")
    assert (a["pin_entries"], a["views"], a["failed"], a["approx_viewers"]) == (3, 2, 1, 2)

    b = by_client[agg["b_id"]]
    assert (b["client_name"], b["client_code"]) == ("Beta Ltd", "BBB")
    # failed = RATE_LIMITED + NO_PIN = 2; approx_viewers = {h3,h4} (null excluded) = 2
    assert (b["pin_entries"], b["views"], b["failed"], b["approx_viewers"]) == (3, 1, 2, 2)

    nul = by_client[None]
    assert nul["client_name"] is None and nul["client_code"] is None
    assert (nul["pin_entries"], nul["views"], nul["failed"], nul["approx_viewers"]) == (1, 0, 0, 1)


def test_summary_trend_by_day(agg: dict[str, Any]) -> None:
    s = invoice_access.summary(
        agg["db"], date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
    assert s["trend"] == [
        {"date": "2026-08-10", "views": 2, "failed": 1},
        {"date": "2026-08-11", "views": 1, "failed": 2},
    ]


def test_summary_client_filter(agg: dict[str, Any]) -> None:
    s = invoice_access.summary(
        agg["db"], date_from=date(2026, 8, 1), date_to=date(2026, 8, 31),
        client_id=agg["a_id"])
    assert s["total_pin_entries"] == 3
    assert s["total_views"] == 2
    assert s["total_failed"] == 1
    assert s["approx_viewers"] == 2
    assert [r["client_id"] for r in s["by_client"]] == [agg["a_id"]]


def test_summary_narrow_range_excludes_other_day(agg: dict[str, Any]) -> None:
    s = invoice_access.summary(
        agg["db"], date_from=date(2026, 8, 10), date_to=date(2026, 8, 10))
    assert s["total_pin_entries"] == 3
    assert [t["date"] for t in s["trend"]] == ["2026-08-10"]


def test_recent_newest_first_and_names(agg: dict[str, Any]) -> None:
    rec = invoice_access.recent(agg["db"], limit=50)
    assert len(rec) == 9                          # all rows (no date filter on recent)
    # Newest-first: the 2026-09-05 client-B VIEWED row leads.
    assert rec[0]["outcome"] == AccessOutcome.VIEWED
    assert rec[0]["challan_number"] == agg["b_number"]
    assert rec[0]["client_name"] == "Beta Ltd"
    # The null-client row surfaces client_name None but keeps the challan number.
    nul = [r for r in rec if r["client_name"] is None]
    assert nul and nul[0]["outcome"] == AccessOutcome.NOT_AVAILABLE


def test_recent_client_filter(agg: dict[str, Any]) -> None:
    rec = invoice_access.recent(agg["db"], client_id=agg["a_id"])
    assert {r["challan_number"] for r in rec} == {agg["a_number"]}


# =============================================== record_access / prune / cap (write-side)

@pytest.fixture
def wdb(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A writable session + one seeded challan for the record_access/prune/cap tests."""
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = _mk_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = TestSession()
    challan = _seed_challan(db, token="tW", number="GIF/DC/26-27/L/000900", num_int=900,
                            project_code="ZZZ")
    db.commit()
    yield {"db": db, "challan": challan}
    db.close()
    engine.dispose()


def _count_rows(db: Session) -> int:
    return int(
        db.execute(select(func.count()).select_from(ChallanInvoiceAccess)).scalar() or 0
    )


def test_record_access_coalesces_identical_within_window(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.VIEWED, viewer_hash="h1"
    ) is True
    # A byte-identical refresh within the window writes NOTHING (coalesced -> False).
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.VIEWED, viewer_hash="h1"
    ) is False
    assert _count_rows(db) == 1


def test_record_access_different_viewer_writes_two_rows(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.VIEWED, viewer_hash="h1"
    ) is True
    # A DIFFERENT viewer_hash is a distinct event -> both log.
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.VIEWED, viewer_hash="h2"
    ) is True
    assert _count_rows(db) == 2


def test_record_access_null_viewer_coalesces(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.RATE_LIMITED, viewer_hash=None
    ) is True
    # NULL viewer_hash must coalesce via IS NULL (not `= NULL`, which never matches).
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.RATE_LIMITED, viewer_hash=None
    ) is False
    assert _count_rows(db) == 1


def test_record_access_writes_again_after_window(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    # A backdated identical row OUTSIDE the coalesce window must not suppress a new one.
    old = datetime.now(UTC) - timedelta(seconds=invoice_access.COALESCE_WINDOW_SECONDS + 30)
    db.add(_row(ch.id, None, AccessOutcome.VIEWED, old, "h1"))
    db.commit()
    assert invoice_access.record_access(
        db, challan=ch, client_id=None, outcome=AccessOutcome.VIEWED, viewer_hash="h1"
    ) is True
    assert _count_rows(db) == 2


def test_summary_trend_buckets_on_utc_date(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    # 23:30 UTC — a session-local (e.g. IST +5:30) bucketing would roll it to the next
    # day; the UTC-day expression keeps it on 2026-08-12. (Best-effort on sqlite, which
    # stores the naive UTC value we wrote.)
    db.add(_row(ch.id, None, AccessOutcome.VIEWED,
                datetime(2026, 8, 12, 23, 30, tzinfo=UTC), "hb"))
    db.commit()
    s = invoice_access.summary(db, date_from=date(2026, 8, 12), date_to=date(2026, 8, 12))
    assert [t["date"] for t in s["trend"]] == ["2026-08-12"]
    assert s["trend"][0]["views"] == 1


def test_prune_old_deletes_only_older_than_cutoff(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    now = datetime.now(UTC)
    db.add_all([
        _row(ch.id, None, AccessOutcome.VIEWED, now - timedelta(days=200), "old1"),
        _row(ch.id, None, AccessOutcome.VIEWED, now - timedelta(days=181), "old2"),
        _row(ch.id, None, AccessOutcome.VIEWED, now - timedelta(days=10), "fresh"),
    ])
    db.commit()
    deleted = invoice_access.prune_old(db, older_than_days=180)
    db.commit()
    assert deleted == 2
    assert _count_rows(db) == 1


def test_recent_limit_capped_at_200(wdb: dict[str, Any]) -> None:
    db, ch = wdb["db"], wdb["challan"]
    now = datetime.now(UTC)
    # 201 rows -> the cap (_MAX_RECENT=200) must actually bound the returned set.
    db.add_all([_row(ch.id, None, AccessOutcome.VIEWED, now, f"h{i}") for i in range(201)])
    db.commit()
    assert len(invoice_access.recent(db, limit=99999)) == 200


# ====================================================== viewer-side logging

PIN = "4821"


class _FakeSettings:
    def __init__(self, enabled: bool) -> None:
        self.qr_invoice_access_enabled = enabled


@pytest.fixture
def viewer(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    pub_service.reset_rate_limits()
    engine = _mk_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(pub_routes, "get_settings", lambda: _FakeSettings(True))

    seed = TestSession()
    client_row = projects_service.create_client(seed, name="Client Co", code="CLI", actor_uid="a")
    seed.flush()
    client_row.access_pin = PIN
    project = projects_service.create_project(
        seed, client_id=client_row.id, name="Proj", actor_uid="a")
    seed.flush()
    challan = _seed_challan(seed, token="tok-good", number="GIF/DC/26-27/L/000189",
                            num_int=189, project_code=project.code, invoice_number="CINV-777")
    seed.commit()
    number = challan.number
    client_id = client_row.id
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(pub_routes.router)
    app.dependency_overrides[get_db] = _db

    yield {"client": TestClient(app), "TestSession": TestSession,
           "client_id": client_id, "number": number, "good_password": PIN + number}
    engine.dispose()


def _outcomes(TestSession: Any) -> list[str]:  # noqa: ANN401
    db = TestSession()
    try:
        return list(db.execute(
            select(ChallanInvoiceAccess.outcome).order_by(ChallanInvoiceAccess.id)
        ).scalars())
    finally:
        db.close()


def _seed_confirmed_invoice(TestSession: Any, client_id: int) -> None:  # noqa: ANN401
    from app.modules.billing.models import SalesInvoice

    db = TestSession()
    storage_ref = get_storage().save("inv.pdf", PDF_BYTES)
    blob = StoredFile(kind="upload", filename="invoice.pdf", content_type="application/pdf",
                      size=len(PDF_BYTES), storage_ref=storage_ref, uploaded_by="ops",
                      module_key="billing")
    db.add(blob)
    db.flush()
    db.add(SalesInvoice(client_id=client_id, source_file_id=blob.id,
                        invoice_number="CINV-777", status="CONFIRMED"))
    db.commit()
    db.close()


def test_log_viewed(viewer: dict[str, Any]) -> None:
    _seed_confirmed_invoice(viewer["TestSession"], viewer["client_id"])
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["good_password"]})
    assert r.status_code == 200 and r.content == PDF_BYTES
    assert _outcomes(viewer["TestSession"]) == [AccessOutcome.VIEWED]


def test_log_wrong_pin(viewer: dict[str, Any]) -> None:
    r = viewer["client"].post("/d/tok-good", data={"password": "nope"})
    assert r.status_code == 404
    assert _outcomes(viewer["TestSession"]) == [AccessOutcome.WRONG_PIN]


def test_log_not_available(viewer: dict[str, Any]) -> None:
    # Correct password but no confirmed invoice seeded.
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["good_password"]})
    assert r.status_code == 200 and "not available yet" in r.text.lower()
    assert _outcomes(viewer["TestSession"]) == [AccessOutcome.NOT_AVAILABLE]


def test_log_no_pin(viewer: dict[str, Any]) -> None:
    db = viewer["TestSession"]()
    from app.modules.projects.models import ProjectClient
    row = db.get(ProjectClient, viewer["client_id"])
    assert row is not None
    row.access_pin = None
    db.commit()
    db.close()
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["number"]})
    assert r.status_code == 404
    assert _outcomes(viewer["TestSession"]) == [AccessOutcome.NO_PIN]


def test_log_rate_limited(viewer: dict[str, Any]) -> None:
    for _ in range(pub_service.MAX_FAILED_ATTEMPTS):
        viewer["client"].post("/d/tok-good", data={"password": "wrong"})
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["good_password"]})
    assert r.status_code == 429
    outcomes = _outcomes(viewer["TestSession"])
    # FIX 1 (coalesce): the identical WRONG_PIN attempts (same challan + outcome +
    # viewer within the 60s window — the TestClient's peer hash is constant) collapse
    # to ONE audit row; the distinct RATE_LIMITED outcome still logs. The visitor's
    # responses (repeated 404s then a 429) are unchanged — only the log write coalesces.
    assert outcomes == [AccessOutcome.WRONG_PIN, AccessOutcome.RATE_LIMITED]


def test_unknown_token_logs_nothing(viewer: dict[str, Any]) -> None:
    r = viewer["client"].post("/d/does-not-exist", data={"password": "whatever"})
    assert r.status_code == 404
    assert _outcomes(viewer["TestSession"]) == []


def test_feature_off_logs_nothing(
    viewer: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pub_routes, "get_settings", lambda: _FakeSettings(False))
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["good_password"]})
    assert r.status_code == 404
    assert _outcomes(viewer["TestSession"]) == []


def test_logging_failure_does_not_break_viewer(
    viewer: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_confirmed_invoice(viewer["TestSession"], viewer["client_id"])

    def _boom(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        raise RuntimeError("logging backend down")

    monkeypatch.setattr(invoice_access, "record_access", _boom)
    r = viewer["client"].post("/d/tok-good", data={"password": viewer["good_password"]})
    # The visitor still gets their PDF; the log write silently failed.
    assert r.status_code == 200 and r.content == PDF_BYTES
    assert _outcomes(viewer["TestSession"]) == []


# ================================================================= routes

@pytest.fixture
def routes_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = _mk_engine()
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    db = TestSession()
    client_row = projects_service.create_client(db, name="Alpha Ltd", code="AAA", actor_uid="s")
    db.flush()
    project = projects_service.create_project(db, client_id=client_row.id, name="PA", actor_uid="s")
    db.flush()
    challan = _seed_challan(db, token="tA", number="GIF/DC/26-27/L/000001", num_int=1,
                            project_code=project.code)
    db.flush()
    now = datetime.now(UTC)
    vh1 = "hh-secret-viewerhash-01"
    vh2 = "hh-secret-viewerhash-02"
    db.add_all([
        _row(challan.id, client_row.id, AccessOutcome.VIEWED, now, vh1),
        _row(challan.id, client_row.id, AccessOutcome.WRONG_PIN, now, vh2),
    ])
    db.commit()
    db.close()

    def _db() -> Iterator[Session]:
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(challan_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: MANAGER
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def test_summary_requires_manage(routes_client: TestClient) -> None:
    for u in (VIEWER_USER, OUTSIDER):
        _as(routes_client, u)
        assert routes_client.get("/api/v1/challan/invoice-access/summary").status_code == 403


def test_recent_requires_manage(routes_client: TestClient) -> None:
    for u in (VIEWER_USER, OUTSIDER):
        _as(routes_client, u)
        assert routes_client.get("/api/v1/challan/invoice-access/recent").status_code == 403


def test_summary_shape_default_range(routes_client: TestClient) -> None:
    r = routes_client.get("/api/v1/challan/invoice-access/summary")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "total_pin_entries", "total_views", "total_not_available", "total_failed",
        "approx_viewers", "by_outcome", "by_client", "trend",
    }
    assert set(body["by_outcome"]) == {
        "VIEWED", "WRONG_PIN", "NOT_AVAILABLE", "RATE_LIMITED", "NO_PIN"}
    assert body["total_pin_entries"] == 2
    assert body["total_views"] == 1
    assert body["total_failed"] == 1
    assert body["by_client"][0]["client_code"] == "AAA"


def test_summary_explicit_range_and_filter(routes_client: TestClient) -> None:
    r = routes_client.get(
        "/api/v1/challan/invoice-access/summary",
        params={"from": "2020-01-01", "to": "2020-01-31"})
    assert r.status_code == 200
    assert r.json()["total_pin_entries"] == 0     # nothing in that historical window


def test_recent_shape_and_no_hash_leak(routes_client: TestClient) -> None:
    r = routes_client.get("/api/v1/challan/invoice-access/recent")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert set(body[0]) == {"challan_number", "client_name", "accessed_at", "outcome"}
    # The viewer_hash must NEVER appear in the payload.
    assert "viewer_hash" not in r.text
    assert "viewerhash" not in r.text


def test_admin_role_also_allowed(routes_client: TestClient) -> None:
    _as(routes_client, ADMIN)
    assert routes_client.get("/api/v1/challan/invoice-access/summary").status_code == 200
    assert routes_client.get("/api/v1/challan/invoice-access/recent").status_code == 200
