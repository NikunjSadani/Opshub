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
from app.modules.challan import service
from app.modules.challan.models import Challan, ChallanStatus
from app.modules.challan.routes import router
from app.modules.masterdata.models import Consignee, Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.modules.numbering.models import NumberingAllocation
from app.platform.auth import current_user
from app.platform.models import Role, Setting, User, UserModuleAccess

GSTIN = "27AAAAA0000A1Z5"
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
    seed.add(Consignee(brand="Deoleo", state="Maharashtra", name="Deoleo MH", gstin=GSTIN,
                       active=True))
    seed.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    seed.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(seed, "L", fy="26-27", last_number=0)
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


def _xlsx(rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(service.parsing.CHALLAN_COLUMNS))
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row(group: str, hsn: str = "1509", amount: str = "105.00") -> list[str]:
    # Column order must match schema.CHALLAN_COLUMNS; amount is tax-inclusive (100x1x1.05).
    return [group, "Deoleo", "Maharashtra", "Store A", "Addr A", "", "", "",
            "15-05-2026", "Item", hsn, "1", "100.00", amount, "5", "", ""]


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


def test_register_requires_module(client: TestClient) -> None:
    _as(client, OUTSIDER)
    assert client.get("/api/v1/challan/challans").status_code == 403
    _as(client, MIS)
    assert client.get("/api/v1/challan/challans").status_code == 200
