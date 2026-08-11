"""Challan generator end-to-end (L/433 shape): validate -> reserve -> render ->
issue -> package, with a fake renderer (WeasyPrint is container-only).

Proves the numbering lock-window is closed: the fake renderer, on its first call,
reads the DB from a SEPARATE connection and asserts every number was already
reserved+committed before any rendering began.
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from decimal import Decimal

import pytest
from openpyxl import Workbook
from pypdf import PdfWriter
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.modules.challan import service
from app.modules.challan.models import BatchStatus, Challan, ChallanStatus
from app.modules.masterdata.models import Consignee, Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.modules.numbering.models import AllocationStatus, NumberingAllocation
from app.platform.models import Setting
from app.platform.storage import get_storage

GSTIN = "27AAAAA0000A1Z2"  # valid, Maharashtra


@event.listens_for(Engine, "connect")
def _sqlite_wal(dbapi_conn: object, _rec: object) -> None:
    cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


class FakeRenderer:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url
        self.reserved_before_first_render: int | None = None
        self.calls = 0

    def render_pdf(self, html: str) -> bytes:
        self.calls += 1
        if self.reserved_before_first_render is None:
            probe = create_engine(self.db_url, future=True)
            with probe.connect() as conn:
                self.reserved_before_first_render = conn.execute(
                    select(func.count()).select_from(NumberingAllocation)
                ).scalar()
            probe.dispose()
        return _blank_pdf()


class RaisingRenderer:
    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def render_pdf(self, html: str) -> bytes:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("render boom")
        return _blank_pdf()


def _blank_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Session, str]]:
    db_url = f"sqlite:///{tmp_path}/challan.db"
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    _seed(session)
    try:
        yield session, db_url
    finally:
        session.close()
        engine.dispose()


def _seed(db: Session) -> None:
    db.add(Consignor(name="Tech Gifsy Solutions Limited", gstin=GSTIN, state="Maharashtra",
                     address="Howrah warehouse", phone="+91 6289864191", active=True))
    db.add(Consignee(brand="Deoleo", state="Maharashtra", name="Deoleo MH", gstin=GSTIN,
                     address="Mumbai", phone="+91 6289864191", active=True))
    db.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    db.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(db, "L", fy="26-27", last_number=0)
    db.commit()


# Column order must match schema.CHALLAN_COLUMNS.
def _row(group: str, ship: str, desc: str, qty: str, rate: str, amount: str,
         brand: str = "Deoleo", state: str = "Maharashtra") -> list[str]:
    return [group, brand, state, ship, f"{ship} address", "", "", "",
            "15-05-2026", desc, "1509", qty, rate, amount, "5", "", ""]


def _workbook(rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(service.parsing.CHALLAN_COLUMNS))
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _store_source(db: Session, data: bytes) -> int:
    from app.modules.files.models import StoredFile
    ref = get_storage().save("challan-source/test/in.xlsx", data)
    sf = StoredFile(kind="challan-source", filename="in.xlsx", content_type=None,
                    size=len(data), storage_ref=ref, uploaded_by="tester",
                    module_key="document_automation")
    db.add(sf)
    db.flush()
    return sf.id


def _upload(db: Session, data: bytes) -> service.ChallanBatch:
    batch = service.new_batch(db, source_file_id=_store_source(db, data), actor_uid="tester")
    db.commit()
    return batch


# --- amounts are tax-INCLUSIVE at 5%: 100x10x1.05 = 1050.00, 200x5x1.05 = 1050.00

def test_happy_path_reserves_before_render_and_issues(env: tuple[Session, str]) -> None:
    db, db_url = env
    data = _workbook([
        _row("G1", "Store A", "Olive Oil 1L", "10", "100.00", "1050.00"),
        _row("G1", "Store A", "Olive Oil 2L", "5", "200.00", "1050.00"),
        _row("G2", "Store B", "Olive Oil 1L", "2", "100.00", "210.00"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok, [(e.column, e.message) for e in result.errors]
    assert batch.status == BatchStatus.VALIDATED.value
    assert batch.challan_count == 2 and batch.line_count == 3

    fake = FakeRenderer(db_url)
    service.generate(db, batch, fake, series="L", actor_uid="tester")

    assert batch.status == BatchStatus.COMPLETED.value
    assert fake.reserved_before_first_render == 2  # THE PROOF

    challans = list(db.execute(select(Challan).order_by(Challan.number_int)).scalars())
    assert [c.number for c in challans] == ["GIF/DC/26-27/L/000001", "GIF/DC/26-27/L/000002"]
    assert all(c.status == ChallanStatus.ISSUED.value for c in challans)
    assert challans[0].consignee_gstin == GSTIN and challans[0].consignor_name.startswith("Tech")
    # tax-inclusive totals + e-way flag (threshold 1000 rupees = 100000 paise)
    assert challans[0].total_paise == 210000 and challans[0].eway_required is True
    assert challans[1].total_paise == 21000 and challans[1].eway_required is False
    assert batch.zip_file_id is not None and batch.merged_pdf_file_id is not None


def test_value_free_challan(env: tuple[Session, str]) -> None:
    db, db_url = env
    data = _workbook([_row("G1", "Store A", "Sample unit", "1", "", "")])  # no rate/amount
    batch = _upload(db, data)
    assert service.validate_batch(db, batch, actor_uid="tester").ok
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    challan = db.execute(select(Challan)).scalar_one()
    assert challan.total_paise is None and challan.eway_required is False


def test_amount_tax_inclusive_mismatch_flagged(env: tuple[Session, str]) -> None:
    db, _ = env
    # 100x10 without the +5% GST = 1000, but tax-inclusive expects 1050 -> flagged.
    data = _workbook([_row("G1", "Store A", "Item", "10", "100.00", "1000.00")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "amount" for e in result.errors)


def test_mixed_priced_and_value_free_rejected(env: tuple[Session, str]) -> None:
    # One priced line + one value-free line in the SAME challan -> rejected, so the
    # total can't be a partial sum that mis-drives the e-way flag.
    data = _workbook([
        _row("G1", "Store A", "Priced", "10", "100.00", "1050.00"),
        _row("G1", "Store A", "Free", "1", "", ""),
    ])
    batch = _upload(db := env[0], data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any("mixes" in e.message for e in result.errors)


def test_amount_sanity_absolute_cap_catches_large_typo(env: tuple[Session, str]) -> None:
    # expected = 1000 x 1000 x 1.05 = 10,50,000; a Rs 10,000 typo is < 1% but the
    # Rs 500 absolute cap still flags it.
    db = env[0]
    data = _workbook([_row("G1", "Store A", "Bulk", "1000", "1000.00", "1040000.00")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "amount" for e in result.errors)


def test_gst_rate_mismatch_flagged(env: tuple[Session, str]) -> None:
    db, _ = env
    rows = [_row("G1", "Store A", "Item", "10", "100.00", "1050.00")]
    rows[0][14] = "18"  # supplied gst_rate 18 != HSN 1509 rate 5
    batch = _upload(db, _workbook(rows))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "gst_rate" for e in result.errors)


def test_single_consignor_required(env: tuple[Session, str]) -> None:
    db, _ = env
    db.add(Consignor(name="Second Depot", gstin=GSTIN, state="Maharashtra", active=True))
    db.commit()  # now TWO active consignors -> ambiguous
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any("consignor" in e.message for e in result.errors)


def test_case_insensitive_masterdata_resolves(env: tuple[Session, str]) -> None:
    db, _ = env
    data = _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00",
                           brand="DEOLEO", state="maharashtra")])
    batch = _upload(db, data)
    assert service.validate_batch(db, batch, actor_uid="tester").ok


def test_validation_failure_writes_error_report(env: tuple[Session, str]) -> None:
    db, _ = env
    rows = [_row("G1", "Store A", "Bad HSN", "1", "100.00", "105.00")]
    rows[0][10] = "9999"  # unknown HSN
    batch = _upload(db, _workbook(rows))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok
    assert batch.status == BatchStatus.FAILED_VALIDATION.value
    assert batch.error_report_file_id is not None
    assert db.execute(select(func.count()).select_from(NumberingAllocation)).scalar() == 0


def test_partial_generation_failure_then_retry_resumes(env: tuple[Session, str]) -> None:
    db, db_url = env
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00"),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")

    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    assert len(list(db.execute(select(Challan)).scalars())) == 1
    voided = [a for a in db.execute(select(NumberingAllocation)).scalars()
              if a.status == AllocationStatus.VOID.value]
    assert len(voided) == 1

    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.COMPLETED.value
    final = list(db.execute(select(Challan)).scalars())
    assert len(final) == 2 and len({c.allocation_id for c in final}) == 2


def test_per_batch_challan_cap(env: tuple[Session, str],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = env
    monkeypatch.setattr(service, "MAX_BATCH_CHALLANS", 1)
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any("limit" in e.message for e in result.errors)


def test_csv_field_neutralizes_formula_injection() -> None:
    assert service._csv_field("=cmd|calc") == "\"'=cmd|calc\""
    assert service._csv_field("@SUM(A1)") == "\"'@SUM(A1)\""
    assert service._csv_field("normal") == '"normal"'


def test_challan_date_drives_fy(env: tuple[Session, str]) -> None:
    db, db_url = env
    numbering.seed_series(db, "L", fy="25-26", last_number=0)
    db.commit()
    rows = [_row("G1", "Store A", "Item", "1", "100.00", "105.00")]
    rows[0][8] = "15-03-2026"  # March -> prior FY 25-26
    batch = _upload(db, _workbook(rows))
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    challan = db.execute(select(Challan)).scalar_one()
    assert challan.fy == "25-26" and challan.number == "GIF/DC/25-26/L/000001"
