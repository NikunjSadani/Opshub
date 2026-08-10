"""Challan generator end-to-end: validate -> reserve -> render -> issue -> package.

Exercises the real orchestration with a fake renderer (WeasyPrint isn't installed
locally). Critically, it PROVES the numbering lock-window is closed: the fake
renderer, on its first call, reads the DB from a SEPARATE connection and asserts
every number was already reserved+committed before any rendering began.
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

GSTIN = "27AAAAA0000A1Z5"


@event.listens_for(Engine, "connect")
def _sqlite_wal(dbapi_conn: object, _rec: object) -> None:
    # WAL so a separate reader connection sees committed rows without blocking on
    # the writer's open transaction — needed for the "committed before render" proof.
    cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


class FakeRenderer:
    """Renders a blank 1-page PDF; on first call records reservations visible from
    a fresh connection (proving they were committed before rendering)."""

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
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()


@pytest.fixture
def env(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Session, str]]:
    db_file = f"{tmp_path}/challan.db"
    db_url = f"sqlite:///{db_file}"
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
    db.add(Consignor(name="Gifsy Depot", gstin=GSTIN, state="Maharashtra", active=True))
    db.add(Consignee(brand="Deoleo", state="Maharashtra", name="Deoleo MH",
                     gstin=GSTIN, active=True))
    db.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    db.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(db, "L", fy="26-27", last_number=0)
    db.commit()


def _row(group: str, ship: str, desc: str, qty: str, rate: str, amount: str) -> list[str]:
    return [group, "Gifsy Depot", "Deoleo", ship, f"{ship} address", "Maharashtra",
            "15-05-2026", desc, "1509", qty, "NOS", rate, amount, "5", "", ""]


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


# ------------------------------------------------------------------- tests

def test_happy_path_reserves_before_render_and_issues(env: tuple[Session, str]) -> None:
    db, db_url = env
    data = _workbook([
        _row("G1", "Store A", "Olive Oil 1L", "10", "100.00", "1000.00"),
        _row("G1", "Store A", "Olive Oil 2L", "5", "180.00", "900.00"),
        _row("G2", "Store B", "Olive Oil 1L", "2", "100.00", "200.00"),
    ])
    batch = _upload(db, data)

    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok
    assert batch.status == BatchStatus.VALIDATED.value
    assert batch.challan_count == 2 and batch.line_count == 3

    fake = FakeRenderer(db_url)
    service.generate(db, batch, fake, series="L", actor_uid="tester")

    assert batch.status == BatchStatus.COMPLETED.value
    # THE PROOF: both numbers were reserved+committed before the first render.
    assert fake.reserved_before_first_render == 2

    challans = list(db.execute(select(Challan).order_by(Challan.number_int)).scalars())
    assert [c.number for c in challans] == ["GIF/DC/26-27/L/000001", "GIF/DC/26-27/L/000002"]
    assert all(c.status == ChallanStatus.ISSUED.value for c in challans)
    assert all(c.pdf_file_id is not None for c in challans)
    # consignee snapshot resolved from the Brand->State registry
    assert challans[0].consignee_gstin == GSTIN and challans[0].consignee_name == "Deoleo MH"
    # totals + e-way flag (threshold 1000): G1=1900 -> True, G2=200 -> False
    assert challans[0].total_paise == 190000 and challans[0].eway_required is True
    assert challans[1].total_paise == 20000 and challans[1].eway_required is False
    # numbers ISSUED, artifacts packaged
    allocs = list(db.execute(select(NumberingAllocation)).scalars())
    assert all(a.status == AllocationStatus.ISSUED.value for a in allocs)
    assert batch.zip_file_id is not None and batch.merged_pdf_file_id is not None


def test_case_insensitive_masterdata_resolves(env: tuple[Session, str]) -> None:
    db, _ = env
    # Spreadsheet uses different casing than the registry — must still resolve.
    data = _workbook([_row_ci("G1", "Store A", "Item", "1", "10.00", "10.00")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok, [(e.column, e.message) for e in result.errors]


def _row_ci(group: str, ship: str, desc: str, qty: str, rate: str, amount: str) -> list[str]:
    return [group, "gifsy depot", "DEOLEO", ship, f"{ship} address", "maharashtra",
            "15-05-2026", desc, "1509", qty, "NOS", rate, amount, "5", "", ""]


def test_validation_failure_writes_error_report(env: tuple[Session, str]) -> None:
    db, _ = env
    data = _workbook([
        _row("G1", "Store A", "Bad HSN", "1", "10.00", "10.00"),
    ])
    # Break the HSN so semantic validation fails.
    rows = data
    batch = _upload(db, _replace_hsn(rows))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok
    assert batch.status == BatchStatus.FAILED_VALIDATION.value
    assert batch.error_report_file_id is not None
    # no numbers reserved on a failed batch
    assert db.execute(select(func.count()).select_from(NumberingAllocation)).scalar() == 0


def _replace_hsn(data: bytes) -> bytes:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))
    ws = wb.active
    # hsn is column index 9 (1-based) per CHALLAN_COLUMNS order
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=9, value="9999")  # unknown HSN
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_amount_sanity_flagged(env: tuple[Session, str]) -> None:
    db, _ = env
    data = _workbook([_row("G1", "Store A", "Item", "10", "100.00", "50.00")])  # 10x100 != 50
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok
    assert any(e.column == "amount" for e in result.errors)


class RaisingRenderer:
    """Renders blank PDFs but raises on the Nth call (to simulate a render crash)."""

    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def render_pdf(self, html: str) -> bytes:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("render boom")
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()


def test_partial_generation_failure_then_retry_resumes(env: tuple[Session, str]) -> None:
    db, db_url = env
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "10.00", "10.00"),
        _row("G2", "Store B", "Item", "1", "10.00", "10.00"),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")

    # Render #2 crashes: challan 1 is committed-ISSUED, batch goes FAILED, and the
    # still-RESERVED number for G2 is voided (no wedge in GENERATING).
    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    challans = list(db.execute(select(Challan)).scalars())
    assert len(challans) == 1 and challans[0].status == ChallanStatus.ISSUED.value
    voided = [a for a in db.execute(select(NumberingAllocation)).scalars()
              if a.status == AllocationStatus.VOID.value]
    assert len(voided) == 1  # the orphaned G2 reservation

    # Retry with a working renderer: resume-safe — challan 1 is reused (not
    # double-created), G2 gets a fresh number, batch COMPLETES.
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.COMPLETED.value
    final = list(db.execute(select(Challan)).scalars())
    assert len(final) == 2
    assert len({c.allocation_id for c in final}) == 2  # no duplicate binding
    assert len({c.number for c in final}) == 2
    assert all(c.status == ChallanStatus.ISSUED.value for c in final)


def test_regenerate_completed_batch_is_idempotent(env: tuple[Session, str]) -> None:
    db, db_url = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "10.00", "10.00")]))
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    # Re-running does not create a second challan or burn a second number.
    assert db.execute(select(func.count()).select_from(Challan)).scalar() == 1


def test_per_batch_challan_cap(env: tuple[Session, str],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = env
    monkeypatch.setattr(service, "MAX_BATCH_CHALLANS", 1)
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "10.00", "10.00"),
        _row("G2", "Store B", "Item", "1", "10.00", "10.00"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok
    assert any("limit" in e.message for e in result.errors)


def test_csv_field_neutralizes_formula_injection() -> None:
    assert service._csv_field("=cmd|calc") == "\"'=cmd|calc\""
    assert service._csv_field("@SUM(A1)") == "\"'@SUM(A1)\""
    assert service._csv_field("normal") == '"normal"'


def test_challan_date_drives_fy(env: tuple[Session, str]) -> None:
    db, db_url = env
    # A March date (pre-1 Apr) falls in the PRIOR FY 25-26; seed that series too.
    numbering.seed_series(db, "L", fy="25-26", last_number=0)
    db.commit()
    rows = [_row("G1", "Store A", "Item", "1", "10.00", "10.00")]
    rows[0][6] = "15-03-2026"  # challan_date column
    batch = _upload(db, _workbook(rows))
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    challan = db.execute(select(Challan)).scalar_one()
    assert challan.fy == "25-26"
    assert challan.number == "GIF/DC/25-26/L/000001"
