"""Regression tests for the post-build adversarial-audit findings.

Each test pins a specific defect the audit surfaced so it can't silently return:
  MED-1  a parse/reserve-phase error marks the batch FAILED (never wedged GENERATING)
  MED-2  a 0/negative eway_threshold setting falls back to the statutory default
  MED-3  a junk numeric "serial" date is a clean row error, not an OverflowError/500
  MED-4  voiding an ISSUED number bound to a live challan is refused (409)
  LOW-1  generate atomically claims the batch (a non-VALIDATED/FAILED batch -> 409)
  LOW-3  the upload cap rejects BEFORE the buffer can exceed max_upload_bytes
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.challan import parsing, schema, service
from app.modules.challan.models import BatchStatus, ChallanBatch
from app.modules.challan.routes import router as challan_router
from app.modules.masterdata.models import Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.modules.numbering.models import NumberingAllocation
from app.modules.numbering.routes import router as numbering_router
from app.modules.projects import service as projects_service
from app.platform.auth import current_user
from app.platform.models import Level, Setting, User
from tests.rbac_util import make_role, make_user

GSTIN = "27AAAAA0000A1Z5"
CONSIGNEE_GSTIN = "27AAPFU0939F1ZV"  # valid checksum, Maharashtra — the upload path
ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))
MIS = make_user("mis", role=make_role(module_levels={"document_automation": Level.OPERATE}))


class _NullRenderer:
    """Never actually called in these tests (they fail before rendering)."""

    def render_pdf(self, html: str) -> bytes:  # pragma: no cover - defensive
        return b""


# ------------------------------------------------------------------- fixtures

@pytest.fixture
def session(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    seed = TestSession()
    seed.add(Consignor(name="Gifsy Depot", gstin=GSTIN, state="Maharashtra", active=True))
    seed.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    seed.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(seed, "L", fy="26-27", last_number=0)
    seed.flush()
    _client = projects_service.create_client(seed, name="Britannia", code="BRI", actor_uid="seed")
    projects_service.create_project(seed, client_id=_client.id, name="Rewards", actor_uid="seed")
    seed.commit()
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(challan_router, prefix="/api/v1")
    app.include_router(numbering_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: MIS
    app.state.TestSession = TestSession
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _xlsx(rows: list[dict[str, str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([schema.COLUMN_HEADERS[k] for k in schema.CHALLAN_COLUMNS])
    for r in rows:
        ws.append([r.get(k, "") for k in schema.CHALLAN_COLUMNS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row(group: str) -> dict[str, str]:
    # amount is tax-inclusive (100x1x1.05). Consignee typed inline + resolved by GSTIN.
    return {
        "challan_group": group, "project_id": "BRI-001",
        "ship_to_name": "Store A", "ship_to_address_line1": "Addr A",
        "ship_to_state": "Maharashtra", "ship_to_pincode": "400001",
        "consignee_name": "Deoleo MH", "consignee_address_line1": "Mumbai HQ",
        "consignee_state": "Maharashtra", "consignee_gstin": CONSIGNEE_GSTIN,
        "challan_date": "15-05-2026", "description": "Item", "hsn": "1509",
        "quantity": "1", "rate": "100.00", "amount": "105.00", "gst_rate": "5",
    }


# --------------------------------------------------------- MED-3: date overflow

def test_parse_date_rejects_overflow_serial() -> None:
    assert parsing.parse_date("9999999") is None       # would overflow date -> None
    assert parsing.parse_date("2958466") is None        # one past Excel's max serial
    assert parsing.parse_date("40000") is not None      # a normal serial still parses


def test_structural_date_error_instead_of_raise() -> None:
    row = parsing.RawRow(row_number=2, cells={
        "challan_group": "G1", "project_id": "BRI-001", "ship_to_name": "S",
        "ship_to_address_line1": "A", "ship_to_state": "MH", "consignee_name": "C",
        "consignee_gstin": CONSIGNEE_GSTIN, "challan_date": "9999999",
        "description": "Item", "hsn": "1509", "quantity": "1",
    })
    errors = parsing.structural_row_errors([row])  # must NOT raise
    assert any(e.column == "challan_date" for e in errors)


# --------------------------------------------------------- MED-2: eway threshold

def _put_threshold(db: Session, amount: object) -> None:
    db.add(Setting(key="eway_threshold", value={"amount": amount}, updated_by="t"))
    db.flush()


DEFAULT_PAISE = service._DEFAULT_EWAY_RUPEES * 100  # 50,000 rupees -> 50,00,000 paise


def test_eway_threshold_zero_falls_back_to_default(session: Session) -> None:
    _put_threshold(session, 0)
    assert service._eway_threshold_paise(session) == DEFAULT_PAISE


def test_eway_threshold_negative_falls_back_to_default(session: Session) -> None:
    _put_threshold(session, -1)
    assert service._eway_threshold_paise(session) == DEFAULT_PAISE


def test_eway_threshold_valid_setting_is_used(session: Session) -> None:
    _put_threshold(session, 20000)
    assert service._eway_threshold_paise(session) == 20000 * 100


def test_eway_threshold_missing_setting_defaults(session: Session) -> None:
    assert service._eway_threshold_paise(session) == DEFAULT_PAISE


# ------------------------------------------------- MED-1: generate never wedges

def test_generate_missing_source_marks_failed_not_generating(session: Session) -> None:
    # A batch whose source file is absent must end FAILED (retryable), not stuck in
    # the committed GENERATING state that `generate_batch` refuses to retry.
    batch = service.new_batch(session, source_file_id=None, actor_uid="t")
    session.commit()
    service.generate(session, batch, _NullRenderer(), series="L", actor_uid="t")
    assert batch.status == BatchStatus.FAILED.value
    assert batch.status != BatchStatus.GENERATING.value
    assert "generation failed" in (batch.message or "")


# -------------------------------------------- MED-4: numbering void vs challan

def test_numbering_void_blocks_issued_challan_bound(client: TestClient) -> None:
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(series="L", fy="26-27", number=11,
                                formatted="GIF/DC/26-27/L/000011", status="ISSUED",
                                entity="challan", entity_id="5")
    db.add(alloc)
    db.commit()
    aid = alloc.id
    db.close()

    _as(client, ADMIN)
    r = client.post(f"/api/v1/numbering/allocations/{aid}/void", json={"reason": "x"})
    assert r.status_code == 409
    assert "challan" in r.json()["detail"].lower()


def test_numbering_void_allows_reserved_orphan(client: TestClient) -> None:
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(series="L", fy="26-27", number=12,
                                formatted="GIF/DC/26-27/L/000012", status="RESERVED")
    db.add(alloc)
    db.commit()
    aid = alloc.id
    db.close()

    _as(client, ADMIN)
    r = client.post(f"/api/v1/numbering/allocations/{aid}/void", json={"reason": "orphan"})
    assert r.status_code == 200 and r.json()["status"] == "VOID"


# ------------------------------------------ LOW-1: atomic generate claim (no race)

def test_generate_rejects_non_validated_batch(client: TestClient) -> None:
    up = client.post(
        "/api/v1/challan/batches",
        files={"file": ("in.xlsx", _xlsx([_row("G1")]), "application/vnd.ms-excel")},
    ).json()
    assert up["status"] == "VALIDATED"
    bid = up["id"]

    # Simulate a worker already having claimed the batch.
    db = client.app.state.TestSession()
    b = db.get(ChallanBatch, bid)
    assert b is not None
    b.status = BatchStatus.GENERATING.value
    db.commit()
    db.close()

    r = client.post(f"/api/v1/challan/batches/{bid}/generate", json={"series": "L"})
    assert r.status_code == 409  # the conditional claim matched 0 rows


# ---------------------------------------------------- LOW-3: upload cap tightness

def test_upload_over_cap_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.modules.challan.routes as challan_routes

    # Shrink the cap via the route-bound get_settings (upload only reads max_upload_bytes).
    monkeypatch.setattr(challan_routes, "get_settings",
                        lambda: SimpleNamespace(max_upload_bytes=2048))
    big = _xlsx([_row(f"G{i}") for i in range(200)])
    assert len(big) > 2048
    r = client.post(
        "/api/v1/challan/batches",
        files={"file": ("in.xlsx", big, "application/vnd.ms-excel")},
    )
    assert r.status_code == 413


# ------------------------------------------ inc15: money-cell overflow (HIGH)

def test_parse_paise_rejects_overflow_value() -> None:
    # A finite but absurdly-large money cell overflows the Decimal context on
    # quantize; it must return None (-> a clean row error) not raise, so the
    # synchronous upload can't 500 on a 4-character cell.
    for junk in ("1E30", "111111111111111111111111111", "123456789012345678901234567.99"):
        assert parsing.parse_paise(junk) is None


def _min_cells(**over: str) -> dict[str, str]:
    base = {
        "challan_group": "G1", "project_id": "BRI-001", "ship_to_name": "S",
        "ship_to_address_line1": "A", "ship_to_state": "MH", "consignee_name": "C",
        "consignee_gstin": CONSIGNEE_GSTIN, "challan_date": "15-05-2026",
        "description": "Item", "hsn": "1509", "quantity": "1",
    }
    base.update(over)
    return base


def test_structural_amount_overflow_is_row_error_not_crash() -> None:
    row = parsing.RawRow(row_number=2, cells=_min_cells(amount="1E30"))
    errors = parsing.structural_row_errors([row])  # must NOT raise
    assert any(e.column == "amount" for e in errors)


def test_structural_amount_too_large_is_row_error() -> None:
    # 2e13 rupees -> 2e15 paise, above the sanity ceiling -> a clean row error.
    row = parsing.RawRow(row_number=2, cells=_min_cells(amount="20000000000000"))
    errors = parsing.structural_row_errors([row])
    assert any(e.column == "amount" and "too large" in e.message for e in errors)


def test_to_str_strips_illegal_control_chars() -> None:
    # XML-illegal control chars in a cell must be stripped at coercion, so the
    # downstream Excel review report can't raise IllegalCharacterError (a 500).
    assert parsing._to_str("Deoleo\x01\x1fLtd") == "DeoleoLtd"
    assert parsing._to_str("keep\ttab\nnl") == "keep\ttab\nnl"  # tab/newline are legal
