"""Challan operator surface over HTTP: authz gates + upload/validate flow.

The generate happy-path needs WeasyPrint (container only), so here we cover the
gates that return before any rendering: module-gated upload/reads, the
VALIDATED-only guard on generate, and Admin-only void.
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
from app.modules.challan import schema
from app.modules.challan.models import Challan, ChallanStatus
from app.modules.challan.routes import router
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
