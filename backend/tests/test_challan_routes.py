"""Challan operator surface over HTTP: authz gates + upload/validate flow.

The generate happy-path needs WeasyPrint (container only), so here we cover the
gates that return before any rendering: module-gated upload/reads, the
VALIDATED-only guard on generate, and Admin-only void.
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.db import Base, get_db
from app.modules.challan import download, render, schema
from app.modules.challan.models import BatchStatus, Challan, ChallanBatch, ChallanStatus
from app.modules.challan.routes import router
from app.modules.files.models import StoredFile
from app.modules.masterdata.models import Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.modules.numbering.models import NumberingAllocation
from app.modules.projects import service as projects_service
from app.platform.auth import current_user
from app.platform.models import Role, Setting, User, UserModuleAccess

GSTIN = "27AAAAA0000A1Z5"                 # consignor / direct-insert snapshots (not checksummed)
CONSIGNEE_GSTIN = "27AAPFU0939F1ZV"       # valid checksum, Maharashtra — the upload path
ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MIS = User(firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
           module_access=[UserModuleAccess(module_key="document_automation")])
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)


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
    app.include_router(router, prefix="/api/v1")
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


def _row(group: str, hsn: str = "1509", amount: str = "105.00") -> dict[str, str]:
    # amount is tax-inclusive (100x1x1.05). Consignee typed inline + resolved by GSTIN.
    return {
        "challan_group": group, "project_id": "BRI-001",
        "ship_to_enterprise": "Store A Ent", "ship_to_name": "Store A",
        "ship_to_address_line1": "Addr A", "ship_to_state": "Maharashtra",
        "ship_to_pincode": "400001", "ship_to_phone": "9900000000",
        "consignee_name": "Deoleo MH", "consignee_address_line1": "Mumbai HQ",
        "consignee_pincode": "400001", "consignee_state": "Maharashtra",
        "consignee_phone": "9800000000", "consignee_gstin": CONSIGNEE_GSTIN,
        "challan_date": "15-05-2026", "description": "Item", "hsn": hsn,
        "quantity": "1", "rate": "100.00", "amount": amount, "gst_rate": "5",
    }


def _upload(client: TestClient, data: bytes) -> dict[str, object]:
    return client.post("/api/v1/challan/batches",
                       files={"file": ("in.xlsx", data, "application/vnd.ms-excel")}).json()


# --------------------------------------------------------------------- tests

def test_upload_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    r = client.post("/api/v1/challan/batches",
                    files={"file": ("in.xlsx", _xlsx([_row("G1")]), "application/vnd.ms-excel")})
    assert r.status_code == 403


def test_upload_validates_ok(client: TestClient) -> None:
    r = client.post("/api/v1/challan/batches",
                    files={"file": ("in.xlsx", _xlsx([_row("G1"), _row("G2")]),
                                    "application/vnd.ms-excel")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "VALIDATED"
    assert body["challan_count"] == 2


def test_upload_invalid_writes_report(client: TestClient) -> None:
    body = _upload(client, _xlsx([_row("G1", hsn="9999")]))  # unknown HSN
    assert body["status"] == "FAILED_VALIDATION"
    assert body["error_report_file_id"] is not None


def test_generate_requires_validated_batch(client: TestClient) -> None:
    # A batch that failed validation cannot be generated.
    body = _upload(client, _xlsx([_row("G1", hsn="9999")]))
    r = client.post(f"/api/v1/challan/batches/{body['id']}/generate", json={"series": "L"})
    assert r.status_code == 409


def test_generate_missing_batch_404(client: TestClient) -> None:
    r = client.post("/api/v1/challan/batches/99999/generate", json={"series": "L"})
    assert r.status_code == 404


def test_void_requires_admin(client: TestClient) -> None:
    # Seed an issued challan bound to an allocation directly.
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(series="L", fy="26-27", number=1,
                                formatted="GIF/DC/26-27/L/000001", status="ISSUED")
    db.add(alloc)
    db.flush()
    challan = Challan(
        batch_id=1, allocation_id=alloc.id, number=alloc.formatted, series="L", fy="26-27",
        number_int=1, challan_date=__import__("datetime").date(2026, 5, 15),
        consignor_name="Gifsy Depot", consignor_gstin=GSTIN, consignor_state="MH",
        consignee_brand="Deoleo", consignee_name="Deoleo MH", consignee_gstin=GSTIN,
        consignee_state="MH", ship_to_name="S", ship_to_address="A", ship_to_state="MH",
        total_paise=10000, status=ChallanStatus.ISSUED.value,
    )
    db.add(challan)
    db.commit()
    cid = challan.id
    db.close()

    _as(client, MIS)
    assert client.post(f"/api/v1/challan/challans/{cid}/void",
                       json={"reason": "x"}).status_code == 403
    _as(client, ADMIN)
    r = client.post(f"/api/v1/challan/challans/{cid}/void", json={"reason": "wrong address"})
    assert r.status_code == 200 and r.json()["status"] == "VOID"


def test_value_free_challan_lists_without_error(client: TestClient) -> None:
    # Regression: a value-free challan (total_paise=None) must serialize, not 500.
    db = client.app.state.TestSession()
    alloc = NumberingAllocation(series="L", fy="26-27", number=9,
                                formatted="GIF/DC/26-27/L/000009", status="ISSUED")
    db.add(alloc)
    db.flush()
    db.add(Challan(
        batch_id=1, allocation_id=alloc.id, number=alloc.formatted, series="L", fy="26-27",
        number_int=9, challan_date=__import__("datetime").date(2026, 5, 15),
        consignor_name="Gifsy", consignor_gstin=GSTIN, consignor_state="MH",
        consignee_brand="Deoleo", consignee_name="Deoleo MH", consignee_gstin=GSTIN,
        consignee_state="MH", ship_to_name="S", ship_to_address="A", ship_to_state="MH",
        total_paise=None, status=ChallanStatus.ISSUED.value,
    ))
    db.commit()
    db.close()
    r = client.get("/api/v1/challan/challans")
    assert r.status_code == 200
    assert any(c["total_paise"] is None for c in r.json())


def test_register_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/challan/challans").status_code == 403
    _as(client, MIS)
    assert client.get("/api/v1/challan/challans").status_code == 200


def test_needs_review_decisions_flow(client: TestClient) -> None:
    # Pre-seed a golden record so an upload with different details contradicts it.
    from app.modules.masterdata.models import ConsigneeParty
    db = client.app.state.TestSession()
    db.add(ConsigneeParty(gstin=CONSIGNEE_GSTIN, name="Stored Name Ltd",
                          state="Maharashtra", source="MANUAL"))
    db.commit()
    db.close()

    body = _upload(client, _xlsx([_row("G1")]))  # consignee name "Deoleo MH" -> contradicts
    assert body["status"] == "NEEDS_REVIEW", body
    bid = body["id"]

    decisions = client.get(f"/api/v1/challan/batches/{bid}/decisions").json()
    assert decisions and any(d["field"] == "name" for d in decisions)

    # Generation is blocked while NEEDS_REVIEW.
    blocked = client.post(f"/api/v1/challan/batches/{bid}/generate", json={"series": "L"})
    assert blocked.status_code == 409

    # Resolve every contradiction -> VALIDATED.
    r = client.patch(
        f"/api/v1/challan/batches/{bid}/decisions",
        json={"decisions": [{"id": d["id"], "choice": "REJECT"} for d in decisions]},
    )
    assert r.status_code == 200 and r.json()["status"] == "VALIDATED"


def test_decisions_require_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/challan/batches/1/decisions").status_code == 403


def _needs_review_batch(client: TestClient) -> tuple[int, list[dict[str, object]]]:
    from app.modules.masterdata.models import ConsigneeParty
    db = client.app.state.TestSession()
    db.add(ConsigneeParty(gstin=CONSIGNEE_GSTIN, name="Stored Name Ltd",
                          state="Maharashtra", source="MANUAL"))
    db.commit()
    db.close()
    body = _upload(client, _xlsx([_row("G1")]))
    assert body["status"] == "NEEDS_REVIEW"
    decisions = client.get(f"/api/v1/challan/batches/{body['id']}/decisions").json()
    return body["id"], decisions


def test_update_master_requires_admin(client: TestClient) -> None:
    _as(client, MIS)  # module grant, but NOT admin
    bid, decisions = _needs_review_batch(client)
    # A non-admin module user cannot rewrite the shared golden record.
    r = client.patch(
        f"/api/v1/challan/batches/{bid}/decisions",
        json={"decisions": [{"id": decisions[0]["id"], "choice": "UPDATE_MASTER"}]},
    )
    assert r.status_code == 403
    # But THIS_UPLOAD / REJECT (which never mutate the master) are allowed.
    r2 = client.patch(
        f"/api/v1/challan/batches/{bid}/decisions",
        json={"decisions": [{"id": d["id"], "choice": "REJECT"} for d in decisions]},
    )
    assert r2.status_code == 200 and r2.json()["status"] == "VALIDATED"


def test_duplicate_decision_ids_422(client: TestClient) -> None:
    bid, decisions = _needs_review_batch(client)
    did = decisions[0]["id"]
    r = client.patch(
        f"/api/v1/challan/batches/{bid}/decisions",
        json={"decisions": [{"id": did, "choice": "REJECT"},
                            {"id": did, "choice": "THIS_UPLOAD"}]},
    )
    assert r.status_code == 422


# ----------------------------------------------------- recover / stuck-sweep

def _generating_batch(client: TestClient, *, stale: bool = False) -> int:
    """Create a batch wedged in GENERATING; optionally backdate it past the sweep
    cutoff (a raw UPDATE, so the `onupdate` timestamp default doesn't overwrite it)."""
    db = client.app.state.TestSession()
    batch = ChallanBatch(status=BatchStatus.GENERATING.value)
    db.add(batch)
    db.commit()
    bid = batch.id
    if stale:
        db.execute(sa_update(ChallanBatch).where(ChallanBatch.id == bid)
                   .values(updated_at=datetime.now(UTC) - timedelta(hours=2)))
        db.commit()
    db.close()
    return bid


def test_recover_requires_admin(client: TestClient) -> None:
    bid = _generating_batch(client)
    _as(client, MIS)  # module grant, but NOT admin
    assert client.post(f"/api/v1/challan/batches/{bid}/recover").status_code == 403


def test_recover_missing_batch_404(client: TestClient) -> None:
    _as(client, ADMIN)
    assert client.post("/api/v1/challan/batches/99999/recover").status_code == 404


def test_recover_rejects_non_generating(client: TestClient) -> None:
    body = _upload(client, _xlsx([_row("G1")]))  # VALIDATED, not GENERATING
    _as(client, ADMIN)
    r = client.post(f"/api/v1/challan/batches/{body['id']}/recover")
    assert r.status_code == 409


def test_recover_resets_generating_to_failed(client: TestClient) -> None:
    bid = _generating_batch(client, stale=True)  # idle past the stuck threshold
    _as(client, ADMIN)
    r = client.post(f"/api/v1/challan/batches/{bid}/recover")
    assert r.status_code == 200 and r.json()["status"] == "FAILED"


def test_recover_refuses_fresh_generating(client: TestClient) -> None:
    # A batch still making progress (fresh heartbeat) is not "stuck" -> 409, not reset.
    bid = _generating_batch(client, stale=False)
    _as(client, ADMIN)
    r = client.post(f"/api/v1/challan/batches/{bid}/recover")
    assert r.status_code == 409


def test_stuck_sweep_disabled_without_secret(client: TestClient) -> None:
    get_settings.cache_clear()
    r = client.post("/api/v1/challan/batches/sweep-stuck",
                    headers={"X-Sweep-Secret": "anything"})
    assert r.status_code == 503, r.text


def test_stuck_sweep_rejects_wrong_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    try:
        assert client.post("/api/v1/challan/batches/sweep-stuck",
                           headers={"X-Sweep-Secret": "nope"}).status_code == 403
        assert client.post("/api/v1/challan/batches/sweep-stuck").status_code == 403
    finally:
        get_settings.cache_clear()


def test_stuck_sweep_resets_only_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SWEEP_SECRET", "s3cret")
    try:
        stale = _generating_batch(client, stale=True)
        fresh = _generating_batch(client, stale=False)
        r = client.post("/api/v1/challan/batches/sweep-stuck",
                        headers={"X-Sweep-Secret": "s3cret"})
        assert r.status_code == 200 and r.json() == {"reset": 1}
        db = client.app.state.TestSession()
        assert db.get(ChallanBatch, stale).status == "FAILED"
        assert db.get(ChallanBatch, fresh).status == "GENERATING"
        db.close()
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------- range/list download

import datetime as _dt  # noqa: E402


def _seed_challan(db: object, n: int, status: str = "ISSUED", *, with_pdf: bool = True) -> None:
    """Seed one challan (series L, FY 26-27, number_int=n) + a stored A4 PDF."""
    from app.platform.storage import get_storage
    alloc = NumberingAllocation(series="L", fy="26-27", number=n,
                                formatted=f"GIF/DC/26-27/L/{n:06d}", status="ISSUED")
    db.add(alloc)
    db.flush()
    pdf_id = None
    if with_pdf:
        ref = get_storage().save(f"challan-pdf/{n}.pdf", render.StubRenderer().render_pdf(""))
        sf = StoredFile(kind="challan-pdf", filename=f"{n}.pdf", content_type="application/pdf",
                        size=1, storage_ref=ref, uploaded_by="seed",
                        module_key="document_automation")
        db.add(sf)
        db.flush()
        pdf_id = sf.id
    db.add(Challan(
        batch_id=1, allocation_id=alloc.id, number=alloc.formatted, series="L", fy="26-27",
        number_int=n, challan_date=_dt.date(2026, 5, 15),
        consignor_name="Gifsy Depot", consignor_gstin=GSTIN, consignor_state="MH",
        consignee_brand="Deoleo", consignee_name="Deoleo MH", consignee_gstin=GSTIN,
        consignee_state="MH", ship_to_name="S", ship_to_address="A", ship_to_state="MH",
        total_paise=10000, status=status, pdf_file_id=pdf_id,
    ))
    db.commit()


def test_parse_number_spec() -> None:
    nums, errs = download.parse_number_spec("10-12, 15, 000018")
    assert nums == [10, 11, 12, 15, 18] and errs == []
    assert download.parse_number_spec("5-3")[1]         # reversed range -> error
    assert download.parse_number_spec("abc")[1]         # non-numeric -> error
    assert download.parse_number_spec("1-99999")[1]     # span too large -> error
    assert download.parse_number_spec("")[0] == []      # empty -> no numbers


def test_download_preview_reports_void_and_missing(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _seed_challan(db, 10, "ISSUED")
    _seed_challan(db, 11, "VOID")
    db.close()  # 12 is never seeded -> missing
    r = client.get("/api/v1/challan/download/preview",
                   params={"series": "l", "fy": "26-27", "spec": "10-12"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert [c["number_int"] for c in body["resolved"]] == [10]
    assert body["skipped_void"] == [11]
    assert body["missing"] == [12]
    assert body["no_pdf"] == []


def test_download_separate_zip_and_merged_2up(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _seed_challan(db, 20, "ISSUED")
    _seed_challan(db, 21, "ISSUED")
    _seed_challan(db, 22, "VOID")
    db.close()
    # separate -> a ZIP; the voided 22 is skipped and reported in the header
    z = client.get("/api/v1/challan/download",
                   params={"series": "L", "fy": "26-27", "spec": "20-22", "mode": "separate"})
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    assert z.headers["x-skipped-void"] == "22"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 2  # only the two ISSUED challans
    # merged -> a 2-up PDF: 2 challans on 1 A4 page
    m = client.get("/api/v1/challan/download",
                   params={"series": "L", "fy": "26-27", "spec": "20-22", "mode": "merged"})
    assert m.status_code == 200 and m.headers["content-type"] == "application/pdf"
    assert m.headers["x-skipped-void"] == "22"
    from pypdf import PdfReader
    assert len(PdfReader(io.BytesIO(m.content)).pages) == 1  # 2 challans, 2-up -> 1 sheet


def test_download_bad_spec_400_and_no_issued_404(client: TestClient) -> None:
    db = client.app.state.TestSession()
    _seed_challan(db, 30, "VOID")
    db.close()
    assert client.get("/api/v1/challan/download",
                      params={"series": "L", "fy": "26-27", "spec": "abc"}).status_code == 400
    # only a voided number in range -> nothing issued -> 404
    assert client.get("/api/v1/challan/download",
                      params={"series": "L", "fy": "26-27", "spec": "30"}).status_code == 404


def test_download_issued_without_pdf_is_reported_not_silently_dropped(client: TestClient) -> None:
    """An ISSUED challan with no rendered PDF must never be counted as downloadable nor
    silently dropped: preview separates it into `no_pdf`, and the download excludes it from
    the ZIP while naming it in X-Skipped-Unavailable (the count never over-promises)."""
    db = client.app.state.TestSession()
    _seed_challan(db, 40, "ISSUED", with_pdf=True)
    _seed_challan(db, 41, "ISSUED", with_pdf=False)  # issued but never rendered
    db.close()
    p = client.get("/api/v1/challan/download/preview",
                   params={"series": "L", "fy": "26-27", "spec": "40-41"})
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["count"] == 1                       # only the one with a PDF
    assert [c["number_int"] for c in body["resolved"]] == [40]
    assert body["no_pdf"] == [41]
    z = client.get("/api/v1/challan/download",
                   params={"series": "L", "fy": "26-27", "spec": "40-41"})
    assert z.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 1                          # only 40 downloads
    assert z.headers["x-skipped-unavailable"] == "41"  # 41 reported, not dropped in silence


def test_download_missing_blob_is_reported_not_fatal(client: TestClient) -> None:
    """The ephemeral-disk trap: an ISSUED challan whose StoredFile row exists but whose blob
    is gone must not 500 the whole batch — the other PDFs still download and it is reported."""
    from app.modules.challan.models import Challan
    from app.modules.files.models import StoredFile as _SF
    from app.platform.storage import get_storage
    db = client.app.state.TestSession()
    _seed_challan(db, 50, "ISSUED", with_pdf=True)
    _seed_challan(db, 51, "ISSUED", with_pdf=True)
    # delete 51's blob on disk (row stays) — simulates a post-redeploy ephemeral-disk loss
    c51 = db.execute(select(Challan).where(Challan.number_int == 51)).scalar_one()
    ref = db.get(_SF, c51.pdf_file_id).storage_ref
    get_storage().delete(ref)
    db.close()
    z = client.get("/api/v1/challan/download",
                   params={"series": "L", "fy": "26-27", "spec": "50-51"})
    assert z.status_code == 200, z.text            # NOT a 500
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 1                          # 50 still delivered
    assert z.headers["x-skipped-unavailable"] == "51"
    # now drop 50's blob too -> every requested challan unavailable -> 404 (nothing to hand out)
    db = client.app.state.TestSession()
    c50 = db.execute(select(Challan).where(Challan.number_int == 50)).scalar_one()
    get_storage().delete(db.get(_SF, c50.pdf_file_id).storage_ref)
    db.close()
    assert client.get("/api/v1/challan/download",
                      params={"series": "L", "fy": "26-27", "spec": "50-51"}).status_code == 404
