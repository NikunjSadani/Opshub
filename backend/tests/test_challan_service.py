"""Challan generator end-to-end (L/433 shape): validate -> reserve -> render ->
issue -> package, with a fake renderer (WeasyPrint is container-only).

Proves the numbering lock-window is closed: the fake renderer, on its first call,
reads the DB from a SEPARATE connection and asserts every number was already
reserved+committed before any rendering began.

Increment 15: the consignee is typed inline + resolved by GSTIN (golden record),
and each challan references an ACTIVE Project. These tests also cover the new
behaviours — GSTIN auto-create at generation, deviation WARNINGS (non-blocking),
possible-split WARNINGS, and an unknown Project ID being a blocking error.
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from openpyxl import Workbook
from pypdf import PdfWriter
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.modules.challan import schema, service
from app.modules.challan.models import BatchStatus, Challan, ChallanStatus
from app.modules.masterdata.models import ConsigneeParty, Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.modules.numbering.models import AllocationStatus, NumberingAllocation
from app.modules.projects import service as projects_service
from app.platform.models import Setting
from app.platform.storage import get_storage

GSTIN = "27AAPFU0939F1ZV"  # valid checksum, Maharashtra (27)
CONSIGNOR_GSTIN = "27AAAAA0000A1Z5"  # consignor gstin is not checksum-validated


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
    db.add(Consignor(name="Tech Gifsy Solutions Limited", gstin=CONSIGNOR_GSTIN,
                     state="Maharashtra", address="Howrah warehouse",
                     phone="+91 6289864191", active=True))
    db.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    db.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(db, "L", fy="26-27", last_number=0)
    db.flush()
    # An ACTIVE project "BRI-001" every default row references.
    client = projects_service.create_client(db, name="Britannia", code="BRI", actor_uid="seed")
    projects_service.create_project(db, client_id=client.id, name="Rewards", actor_uid="seed")
    db.commit()


def _row(group: str, ship: str, desc: str, qty: str, rate: str, amount: str,
         gstin: str = GSTIN, project: str = "BRI-001", state: str = "Maharashtra",
         **over: str) -> dict[str, str]:
    """One line row keyed by canonical column key; `over` patches any field."""
    row = {
        "challan_group": group,
        "project_id": project,
        "ship_to_enterprise": f"{ship} Enterprises",
        "ship_to_name": ship,
        "ship_to_address_line1": f"{ship} address",
        "ship_to_address_line2": "",
        "ship_to_city": "Mumbai",
        "ship_to_state": state,
        "ship_to_pincode": "400001",
        "ship_to_phone": "9900000000",
        "consignee_name": "Deoleo MH",
        "consignee_address_line1": "Mumbai HQ",
        "consignee_address_line2": "",
        "consignee_pincode": "400001",
        "consignee_state": state,
        "consignee_phone": "9800000000",
        "consignee_gstin": gstin,
        "challan_date": "15-05-2026",
        "description": desc,
        "hsn": "1509",
        "quantity": qty,
        "rate": rate,
        "amount": amount,
        "gst_rate": "5",
        "po_number": "",
        "invoice_number": "",
    }
    row.update(over)
    return row


def _workbook(rows: list[dict[str, str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([schema.COLUMN_HEADERS[k] for k in schema.CHALLAN_COLUMNS])
    for r in rows:
        ws.append([r.get(k, "") for k in schema.CHALLAN_COLUMNS])
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
    assert challans[0].project_code == "BRI-001"
    # tax-inclusive totals + e-way flag (threshold 1000 rupees = 100000 paise)
    assert challans[0].total_paise == 210000 and challans[0].eway_required is True
    assert challans[1].total_paise == 21000 and challans[1].eway_required is False
    assert batch.zip_file_id is not None and batch.merged_pdf_file_id is not None


def test_generation_mints_unique_access_token(env: tuple[Session, str]) -> None:
    # Every issued challan gets a non-null, non-empty, unique QR access token, minted
    # at generation (feeds the public /d/{token} invoice viewer).
    db, db_url = env
    data = _workbook([
        _row("G1", "Store A", "Olive Oil 1L", "10", "100.00", "1050.00"),
        _row("G2", "Store B", "Olive Oil 1L", "2", "100.00", "210.00"),
    ])
    batch = _upload(db, data)
    assert service.validate_batch(db, batch, actor_uid="tester").ok
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    tokens = [c.access_token for c in db.execute(select(Challan)).scalars()]
    assert len(tokens) == 2
    assert all(t for t in tokens)      # non-null and non-empty
    assert len(set(tokens)) == 2       # unique per challan


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
    rows = [_row("G1", "Store A", "Item", "10", "100.00", "1050.00", gst_rate="18")]
    batch = _upload(db, _workbook(rows))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "gst_rate" for e in result.errors)


def test_single_consignor_required(env: tuple[Session, str]) -> None:
    db, _ = env
    db.add(Consignor(name="Second Depot", gstin=CONSIGNOR_GSTIN, state="Maharashtra",
                     active=True))
    db.commit()  # now TWO active consignors -> ambiguous
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any("consignor" in e.message for e in result.errors)


def test_unknown_project_is_blocking_error(env: tuple[Session, str]) -> None:
    db, _ = env
    data = _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00",
                           project="ZZZ-999")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "project_id" for e in result.errors)


def test_inactive_project_is_blocking_error(env: tuple[Session, str]) -> None:
    db, _ = env
    from app.modules.projects.models import Project, ProjectStatus
    project = db.execute(select(Project).where(Project.code == "BRI-001")).scalar_one()
    projects_service.set_status(db, project=project, status=ProjectStatus.CLOSED.value)
    db.commit()
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "project_id" for e in result.errors)


def _seed_party(db: Session, db_url: str, gstin: str, state: str = "Gujarat") -> None:
    """Create the golden record for `gstin` (name "Deoleo MH") via a clean generate."""
    data = _workbook([_row("S1", "Seed Store", "Item", "1", "100.00", "105.00",
                           gstin=gstin, state=state)])
    batch = _upload(db, data)
    assert service.validate_batch(db, batch, actor_uid="tester").ok
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")


def test_unknown_gstin_autocreated_no_contradiction(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"  # Gujarat (24), valid — new to the master
    data = _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    # A brand-new GSTIN has nothing to contradict -> straight to VALIDATED, no review.
    assert result.ok and not result.contradictions
    assert batch.status == BatchStatus.VALIDATED.value
    # No golden record is written during validation; generation auto-creates it.
    assert db.execute(
        select(func.count()).select_from(ConsigneeParty).where(ConsigneeParty.gstin == fresh)
    ).scalar() == 0
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    assert party.name == "Deoleo MH" and party.source == "UPLOAD"


def test_known_gstin_difference_needs_review_this_upload(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)

    # Same GSTIN, different name -> NEEDS_REVIEW (blocked), one PENDING decision.
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd")])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok and len(result.contradictions) == 1
    assert batch.status == BatchStatus.NEEDS_REVIEW.value
    assert batch.error_report_file_id is not None  # Excel review report attached
    decisions = service.list_decisions(db, batch)
    assert len(decisions) == 1 and decisions[0].field == "name"

    # Decide THIS_UPLOAD -> VALIDATED; the uploaded name prints, the master is untouched.
    service.submit_decisions(db, batch, {decisions[0].id: "THIS_UPLOAD"}, actor_uid="tester")
    assert batch.status == BatchStatus.VALIDATED.value
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    ch = db.execute(select(Challan).where(Challan.batch_id == batch.id)).scalar_one()
    assert ch.consignee_name == "Deoleo India Ltd"          # this-upload value printed
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    assert party.name == "Deoleo MH"                        # master UNCHANGED


def test_decision_update_master_writes_record(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd")])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    decision = service.list_decisions(db, batch)[0]
    service.submit_decisions(db, batch, {decision.id: "UPDATE_MASTER"}, actor_uid="tester",
                             allow_update_master=True)
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    ch = db.execute(select(Challan).where(Challan.batch_id == batch.id)).scalar_one()
    assert party.name == "Deoleo India Ltd" and ch.consignee_name == "Deoleo India Ltd"


def test_decision_reject_keeps_stored(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd")])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    decision = service.list_decisions(db, batch)[0]
    service.submit_decisions(db, batch, {decision.id: "REJECT"}, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    ch = db.execute(select(Challan).where(Challan.batch_id == batch.id)).scalar_one()
    assert party.name == "Deoleo MH" and ch.consignee_name == "Deoleo MH"


def test_partial_review_stays_needs_review(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)
    # Two differing fields -> two contradictions; deciding one leaves the batch blocked.
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd",
                           consignee_phone="9820000000")])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    decisions = service.list_decisions(db, batch)
    assert len(decisions) == 2
    service.submit_decisions(db, batch, {decisions[0].id: "REJECT"}, actor_uid="tester")
    assert batch.status == BatchStatus.NEEDS_REVIEW.value  # still one PENDING


def test_invalid_gstin_is_blocking_error(env: tuple[Session, str]) -> None:
    db, _ = env
    data = _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00",
                           gstin="27AAPFU0939F1ZZ")])  # bad check digit
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "consignee_gstin" for e in result.errors)


def test_same_destination_different_groups_warns(env: tuple[Session, str]) -> None:
    db = env[0]
    # G1 and G2 ship to the same consignee + destination + date -> possible split.
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00"),
        _row("G2", "Store A", "Item", "1", "100.00", "105.00"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok  # non-blocking
    assert any(w.column == "challan_group" and w.severity == "WARNING"
               for w in result.warnings)


def test_validation_failure_writes_error_report(env: tuple[Session, str]) -> None:
    db, _ = env
    rows = [_row("G1", "Store A", "Bad HSN", "1", "100.00", "105.00", hsn="9999")]
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
    rows = [_row("G1", "Store A", "Item", "1", "100.00", "105.00",
                 challan_date="15-03-2026")]  # March -> prior FY 25-26
    batch = _upload(db, _workbook(rows))
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    challan = db.execute(select(Challan)).scalar_one()
    assert challan.fy == "25-26" and challan.number == "GIF/DC/25-26/L/000001"


def test_same_gstin_inconsistent_details_is_error(env: tuple[Session, str]) -> None:
    db = env[0]
    fresh = "24AAACB2894G1ZT"  # Gujarat
    # The SAME GSTIN with two different consignee names in one upload is a blocking
    # error (it is one legal party) — this is what guarantees each contradiction has a
    # single 'your value' to decide on.
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", gstin=fresh, state="Gujarat"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00", gstin=fresh, state="Gujarat",
             consignee_name="Other Traders Pvt Ltd"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok and any(e.column == "consignee_name" for e in result.errors)
    assert batch.status == BatchStatus.FAILED_VALIDATION.value


def test_collision_warns_across_mixed_date_formats(env: tuple[Session, str]) -> None:
    db = env[0]
    # Same consignee + destination, one date "16-05-2026" and one "2026-05-16"
    # (same day, different spelling) must still collide as a possible split.
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", challan_date="16-05-2026"),
        _row("G2", "Store A", "Item", "1", "100.00", "105.00", challan_date="2026-05-16"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok
    assert any(w.column == "challan_group" and w.severity == "WARNING"
               for w in result.warnings)


def test_retry_after_partial_generation_and_project_hold_preserves_issued(
    env: tuple[Session, str],
) -> None:
    db, db_url = env
    from app.modules.projects.models import Project, ProjectStatus
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00"),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    # First run fails on the 2nd render: 1 challan issued, batch FAILED.
    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    assert len(list(db.execute(select(Challan)).scalars())) == 1

    # The project is put ON_HOLD before retry, so re-validation now fails. The
    # already-issued challan must NOT be buried: batch FAILED (not FAILED_VALIDATION),
    # counts not zeroed, the issued statutory document still present.
    proj = db.execute(select(Project).where(Project.code == "BRI-001")).scalar_one()
    projects_service.set_status(db, project=proj, status=ProjectStatus.ON_HOLD.value)
    db.commit()
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    assert "already" in (batch.message or "")
    assert len(list(db.execute(select(Challan)).scalars())) == 1


def test_update_master_not_applied_on_generation_failure(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)  # golden record name "Deoleo MH"
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd")])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    decision = service.list_decisions(db, batch)[0]
    service.submit_decisions(db, batch, {decision.id: "UPDATE_MASTER"}, actor_uid="tester",
                             allow_update_master=True)
    # Generation fails on the first render -> the master must NOT be mutated (the write
    # is deferred to batch success), so a failed batch never rewrites the shared record.
    service.generate(db, batch, RaisingRenderer(fail_on=1), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    assert party.name == "Deoleo MH"  # unchanged despite UPDATE_MASTER


def test_new_gstin_merged_superset_across_groups(env: tuple[Session, str]) -> None:
    db, db_url = env
    fresh = "24AAACB2894G1ZT"  # new to the master
    # Complementary blanks for one NEW GSTIN across two groups (NOT a conflict): the
    # party must be created from the MERGED superset, so no filled value is lost.
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", gstin=fresh, state="Gujarat",
             consignee_phone="", consignee_address_line1="A Street"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00", gstin=fresh, state="Gujarat",
             consignee_phone="9820000000", consignee_address_line1=""),
    ])
    batch = _upload(db, data)
    assert service.validate_batch(db, batch, actor_uid="tester").ok
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    party = db.execute(select(ConsigneeParty).where(ConsigneeParty.gstin == fresh)).scalar_one()
    assert party.phone == "9820000000"        # from G2 (G1 left it blank)
    assert "A Street" in party.address_line1   # from G1 (G2 left it blank)
    # Both challans print the merged phone, not one group's blank.
    for ch in db.execute(select(Challan).where(Challan.batch_id == batch.id)).scalars():
        assert "9820000000" in (ch.consignee_phone or "")


def _add_project(db: Session, name: str) -> str:
    """Create another ACTIVE project under the seeded BRI client; return its code."""
    from app.modules.projects.models import Project, ProjectClient
    client = db.execute(select(ProjectClient).where(ProjectClient.code == "BRI")).scalar_one()
    projects_service.create_project(db, client_id=client.id, name=name, actor_uid="tester")
    db.commit()
    return db.execute(
        select(Project.code).where(Project.name == name)
    ).scalar_one()


def test_resume_ignores_master_change_to_already_issued_group(env: tuple[Session, str]) -> None:
    db, db_url = env
    from app.modules.projects.models import Project, ProjectStatus
    code2 = _add_project(db, "Second")  # BRI-002 (active)
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", project="BRI-001"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00", project=code2),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    # First run: G1 issues, G2's render fails -> FAILED, one challan.
    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    assert len(list(db.execute(select(Challan)).scalars())) == 1

    # G1's project (ALREADY ISSUED) goes ON_HOLD — it must not block finishing G2.
    proj1 = db.execute(select(Project).where(Project.code == "BRI-001")).scalar_one()
    projects_service.set_status(db, project=proj1, status=ProjectStatus.ON_HOLD.value)
    db.commit()

    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.COMPLETED.value
    challans = list(db.execute(select(Challan)).scalars())
    assert len(challans) == 2 and len({c.allocation_id for c in challans}) == 2


def test_resume_blocks_when_remaining_group_master_invalid(env: tuple[Session, str]) -> None:
    db, db_url = env
    from app.modules.projects.models import Project, ProjectStatus
    code2 = _add_project(db, "Second")  # BRI-002
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", project="BRI-001"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00", project=code2),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value

    # G2's project (the REMAINING, not-yet-issued group) goes ON_HOLD -> retry can't
    # finish it; batch stays FAILED, the one issued challan is kept + named.
    proj2 = db.execute(select(Project).where(Project.code == code2)).scalar_one()
    projects_service.set_status(db, project=proj2, status=ProjectStatus.ON_HOLD.value)
    db.commit()

    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value
    assert "already issued" in (batch.message or "") and "remaining" in (batch.message or "")
    assert len(list(db.execute(select(Challan)).scalars())) == 1


def test_resume_revalidates_a_voided_group(env: tuple[Session, str]) -> None:
    db, db_url = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.COMPLETED.value
    challan = db.execute(select(Challan)).scalar_one()

    # Void the issued challan, then deactivate its HSN in master data.
    service.void_challan(db, challan, reason="wrong", actor_uid="tester")
    hsn = db.execute(select(HsnCode).where(HsnCode.hsn == "1509")).scalar_one()
    hsn.active = False
    db.commit()

    # A VOIDED group is NOT "done" — it must be RE-VALIDATED on retry (its number is
    # re-minted), so it can't be silently re-issued bypassing the now-inactive HSN.
    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status != BatchStatus.COMPLETED.value
    issued = [c for c in db.execute(select(Challan)).scalars()
              if c.status == ChallanStatus.ISSUED.value]
    assert issued == []  # nothing re-issued past HSN validation


# --- Whole-module audit regressions (inc 14-17 seam interactions) -------------

def test_consignee_consistency_flags_blank_base_then_differing(env: tuple[Session, str]) -> None:
    """H2: the first row for a GSTIN leaves an optional field blank, then two LATER
    rows carry DIFFERENT non-empty values. This must be a blocking error — else the
    second value is silently dropped and a never-entered address prints on a challan."""
    db, _ = env
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", consignee_address_line1=""),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00",
             consignee_address_line1="MUMBAI OFFICE"),
        _row("G3", "Store C", "Item", "1", "100.00", "105.00",
             consignee_address_line1="DELHI OFFICE"),
    ])
    batch = _upload(db, data)
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert not result.ok
    assert any(e.column == "consignee_address_line1" and "differs" in e.message
               for e in result.errors)


def test_duplicate_register_warning_on_reupload(env: tuple[Session, str]) -> None:
    """Owner-chosen WARN behaviour: re-uploading a batch whose (consignee GSTIN, group,
    date) is already ISSUED surfaces a NON-blocking warning, never a block."""
    db, db_url = env
    b1 = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    assert service.validate_batch(db, b1, actor_uid="tester").ok
    service.generate(db, b1, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert b1.status == BatchStatus.COMPLETED.value

    b2 = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    result = service.validate_batch(db, b2, actor_uid="tester")
    assert result.ok  # non-blocking — the operator can still proceed
    assert b2.status == BatchStatus.VALIDATED.value
    assert any(w.column == "challan_group" and "already-issued" in w.message
               for w in result.warnings)


def test_first_batch_has_no_self_duplicate_warning(env: tuple[Session, str]) -> None:
    """The batch being validated is excluded from the duplicate check — an empty
    register (or a resume of its own groups) never warns against itself."""
    db, _ = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    result = service.validate_batch(db, batch, actor_uid="tester")
    assert result.ok
    assert not any("already-issued" in w.message for w in result.warnings)


def test_recover_stuck_batch_then_retry_completes(env: tuple[Session, str]) -> None:
    """H1: a batch wedged in GENERATING (dead worker) is reconciled to a retryable
    FAILED, issued challans kept, and the retry finishes without re-minting numbers."""
    db, db_url = env
    code2 = _add_project(db, "Second")
    data = _workbook([
        _row("G1", "Store A", "Item", "1", "100.00", "105.00", project="BRI-001"),
        _row("G2", "Store B", "Item", "1", "100.00", "105.00", project=code2),
    ])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    service.generate(db, batch, RaisingRenderer(fail_on=2), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.FAILED.value

    # Simulate a crash MID-generation: the batch is wedged in GENERATING, 1 issued.
    batch.status = BatchStatus.GENERATING.value
    db.commit()
    # `now` in the future so the heartbeat reads as idle past the stuck threshold.
    service.recover_stuck_batch(db, batch, actor_uid="tester",
                                now=datetime.now(UTC) + timedelta(minutes=5))
    assert batch.status == BatchStatus.FAILED.value
    assert "already issued" in (batch.message or "")
    assert len([c for c in db.execute(select(Challan)).scalars()
                if c.status == ChallanStatus.ISSUED.value]) == 1

    service.generate(db, batch, FakeRenderer(db_url), series="L", actor_uid="tester")
    assert batch.status == BatchStatus.COMPLETED.value
    challans = list(db.execute(select(Challan)).scalars())
    assert len(challans) == 2 and len({c.allocation_id for c in challans}) == 2


def test_recover_rejects_non_generating_batch(env: tuple[Session, str]) -> None:
    db, _ = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    service.validate_batch(db, batch, actor_uid="tester")  # VALIDATED, not GENERATING
    with pytest.raises(service.ChallanError):
        service.recover_stuck_batch(db, batch, actor_uid="tester")


def test_recover_refuses_batch_still_progressing(env: tuple[Session, str]) -> None:
    """A GENERATING batch whose heartbeat is fresh (a live worker) must NOT be
    recoverable — else a recover racing a healthy render could flip a live batch to
    FAILED and let a second worker resume + void the first's reservations."""
    db, _ = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    service.validate_batch(db, batch, actor_uid="tester")
    batch.status = BatchStatus.GENERATING.value
    db.commit()  # updated_at ~ now -> looks live
    with pytest.raises(service.ChallanError):
        service.recover_stuck_batch(db, batch, actor_uid="tester")  # now = real now


def test_sweep_stuck_batches_resets_only_stale_generating(env: tuple[Session, str]) -> None:
    db, _ = env
    batch = _upload(db, _workbook([_row("G1", "Store A", "Item", "1", "100.00", "105.00")]))
    service.validate_batch(db, batch, actor_uid="tester")
    batch.status = BatchStatus.GENERATING.value
    db.commit()

    # Fresh GENERATING (updated_at ~ now) is NOT swept (cutoff = now - 30m).
    assert service.sweep_stuck_batches(db, older_than=timedelta(minutes=30)) == []
    db.commit()
    db.refresh(batch)
    assert batch.status == BatchStatus.GENERATING.value

    # Advance 'now' well past the window -> the stale batch is reset to FAILED.
    ids = service.sweep_stuck_batches(
        db, older_than=timedelta(minutes=30), now=datetime.now(UTC) + timedelta(hours=2))
    db.commit()
    db.refresh(batch)
    assert batch.id in ids and batch.status == BatchStatus.FAILED.value


def test_group_total_over_int8_is_a_validation_error(env: tuple[Session, str]) -> None:
    """F4: a group whose summed line amount overflows the int8 `total_paise` column
    is rejected at VALIDATION (before any number is reserved), not at generate time
    where the INSERT would raise and void already-reserved statutory numbers.

    Each cell is capped at _MAX_MONEY_PAISE (1e15); enough near-max lines sum past
    int8's ceiling (2**63-1 ~ 9.22e18). Rate is blank so the tax-inclusive amount
    sanity check is skipped and only the group-total overflow fires."""
    max_int8 = 2**63 - 1
    per_cell = 10**15  # _MAX_MONEY_PAISE, the highest a single amount cell may carry
    n_lines = max_int8 // per_cell + 2  # sum = (n)*1e15 > int8 ceiling
    assert n_lines * per_cell > max_int8
    amount = f"{per_cell // 100}.00"  # paise -> rupee string
    rows = [
        schema.RawRow(
            row_number=i + 2,
            cells=_row("G1", "Store A", f"Item {i}", "1", "", amount),
        )
        for i in range(n_lines)
    ]
    result = service.validate(db=env[0], rows=rows)
    assert not result.ok
    assert any(
        e.column == "amount" and "too large" in e.message for e in result.errors
    ), [(e.column, e.message) for e in result.errors[:3]]


def test_group_total_within_int8_validates(env: tuple[Session, str]) -> None:
    """F4 guard: a single large-but-in-range priced line still validates (the
    overflow check must not reject a legitimate near-ceiling total)."""
    amount = f"{(10**15) // 100}.00"  # one cell at the per-cell cap, well under int8
    rows = [schema.RawRow(
        row_number=2, cells=_row("G1", "Store A", "Item", "1", "", amount))]
    result = service.validate(db=env[0], rows=rows)
    assert result.ok, [(e.column, e.message) for e in result.errors]


def test_submit_decisions_update_master_requires_authorization(
    env: tuple[Session, str]
) -> None:
    """L4: UPDATE_MASTER is refused at the SERVICE boundary without authorization
    (defense-in-depth beyond the HTTP route); THIS_UPLOAD stays open to anyone."""
    db, db_url = env
    fresh = "24AAACB2894G1ZT"
    _seed_party(db, db_url, fresh)
    data = _workbook([_row("G2", "Store B", "Item", "1", "100.00", "105.00",
                           gstin=fresh, state="Gujarat", consignee_name="Deoleo India Ltd")])
    batch = _upload(db, data)
    service.validate_batch(db, batch, actor_uid="tester")
    decision_id = service.list_decisions(db, batch)[0].id

    with pytest.raises(service.ChallanError):
        service.submit_decisions(db, batch, {decision_id: "UPDATE_MASTER"}, actor_uid="t")
    db.rollback()

    service.submit_decisions(db, batch, {decision_id: "THIS_UPLOAD"}, actor_uid="t")
    assert batch.status == BatchStatus.VALIDATED.value
