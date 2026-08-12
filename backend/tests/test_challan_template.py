"""The self-documenting upload template: structure, the download route, and a
ROUND-TRIP proof that the template's own example rows parse + validate cleanly
against seeded master data (so the examples can never silently rot)."""
from __future__ import annotations

import io
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import FastAPI
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.challan import parsing, service, template
from app.modules.challan.routes import router
from app.modules.challan.schema import CHALLAN_COLUMNS
from app.modules.masterdata.models import Consignee, Consignor, HsnCode
from app.modules.numbering import service as numbering
from app.platform.auth import current_user
from app.platform.models import Role, Setting, User, UserModuleAccess

GSTIN = "27AAAAA0000A1Z5"
MIS = User(firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
           module_access=[UserModuleAccess(module_key="document_automation")])
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS, active=True)


def _seed(db: Session) -> None:
    db.add(Consignor(name="Gifsy Depot", gstin=GSTIN, state="Maharashtra", active=True))
    db.add(Consignee(brand="Deoleo", state="Maharashtra", name="Deoleo MH", gstin=GSTIN,
                     active=True))
    db.add(HsnCode(hsn="1509", gst_rate=Decimal("5"), active=True))
    db.add(Setting(key="eway_threshold", value={"amount": 1000}, updated_by="seed"))
    numbering.seed_series(db, "L", fy="26-27", last_number=0)
    db.commit()


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    _seed(db)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def client() -> Iterator[TestClient]:
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
    yield TestClient(app)
    engine.dispose()


# --------------------------------------------------------------------- tests

def test_template_structure() -> None:
    wb = load_workbook(io.BytesIO(template.build_template_xlsx()))
    # The data sheet MUST be first (the parser reads worksheets[0]).
    assert wb.sheetnames[0] == "Challans"
    assert "Instructions" in wb.sheetnames
    ws = wb.worksheets[0]
    header = [c.value for c in ws[1]]
    assert header == list(CHALLAN_COLUMNS)  # exact keys -> parser recognizes them


def test_template_examples_roundtrip_validate(session: Session) -> None:
    # The template's OWN example rows must parse + validate against master data.
    rows, structural = parsing.parse_workbook(template.build_template_xlsx())
    assert structural == []
    result = service.validate(session, rows)
    assert result.ok, [(e.row_number, e.column, e.message) for e in result.errors]
    challans = {c.group_key: c for c in result.challans}
    assert set(challans) == {"C1", "C2"}
    # C1: single priced line, tax-inclusive 100 x 10 x 1.05 = 1050.00 -> 105000 paise.
    assert challans["C1"].total_paise == 105000
    # C2: two value-free lines -> no total.
    assert challans["C2"].total_paise is None
    assert len(challans["C2"].lines) == 2


def test_template_download_route(client: TestClient) -> None:
    r = client.get("/api/v1/challan/template.xlsx")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "challan-upload-template.xlsx" in r.headers["content-disposition"]
    # the bytes are a real workbook
    assert load_workbook(io.BytesIO(r.content)).sheetnames[0] == "Challans"


def test_template_download_requires_module(client: TestClient) -> None:
    client.app.dependency_overrides[current_user] = lambda: OUTSIDER
    assert client.get("/api/v1/challan/template.xlsx").status_code == 403
