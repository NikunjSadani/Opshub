"""Expense/Invoice operator surface over HTTP.

Mirrors the challan harness (in-memory sqlite StaticPool, create_all, overridden
get_db + current_user, FILES_DIR -> tmp). The extraction engine is a FAKE injected
via `service.get_extractor`, so every case controls exactly what the "PDF" yields:
each uploaded blob is a JSON spec the fake turns into an `ExtractedInvoice`.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.expense import service
from app.modules.expense.canonical import (
    CANONICAL_SCHEMA_VERSION,
    ArithmeticChecks,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceHeader,
    InvoiceLine,
    InvoiceTotals,
)
from app.modules.expense.models import InvoiceCorrection
from app.modules.expense.routes import router
from app.platform.auth import current_user
from app.platform.models import AuditLog, Role, User, UserModuleAccess

SUPPLIER_GSTIN = "27AAPFU0939F1ZV"

ADMIN = User(firebase_uid="adm", email="a@x.com", name="A", role=Role.ADMIN, active=True)
MIS = User(firebase_uid="mis", email="m@x.com", name="M", role=Role.MIS, active=True,
           module_access=[UserModuleAccess(module_key="expense_invoice")])
OUTSIDER = User(firebase_uid="out", email="o@x.com", name="O", role=Role.OPERATIONS,
                active=True)


# ------------------------------------------------- fake extractor + specs

def _f(value: Any, status: FieldStatus = FieldStatus.OK, conf: float = 0.99) -> Field[Any]:
    return Field(
        value_normalized=value,
        value_raw="" if value is None else str(value),
        confidence=0.0 if status is FieldStatus.MISSING else conf,
        source_engine="text_layer/1.0",
        page=1,
        status=status,
    )


def _missing() -> Field[Any]:
    return _f(None, FieldStatus.MISSING)


def _ocr_invoice(spec: dict[str, Any]) -> ExtractedInvoice:
    header = InvoiceHeader(
        supplier_name=_missing(), supplier_gstin=_missing(), supplier_address=_missing(),
        buyer_name=_missing(), buyer_gstin=_missing(), buyer_address=_missing(),
        invoice_number=_missing(), invoice_date=_missing(),
        place_of_supply=_missing(), po_ref=_missing(),
    )
    totals = InvoiceTotals(
        total_taxable_paise=_missing(), total_cgst_paise=_missing(),
        total_sgst_paise=_missing(), total_igst_paise=_missing(),
        round_off_paise=_missing(), grand_total_paise=_missing(),
        amount_in_words=_missing(),
    )
    return ExtractedInvoice(
        schema_version=CANONICAL_SCHEMA_VERSION, doc_type="gst_invoice",
        source_engine="text_layer/1.0", page_count=1, needs_ocr=True,
        review_needed=False, review_reasons=["no text layer"],
        header=header, lines=[], totals=totals,
        arithmetic=ArithmeticChecks(True, True, True, True, 0),
        raw_text="", content_hash="", dedup_key="",
    )


def _extracted(spec: dict[str, Any]) -> ExtractedInvoice:
    if spec.get("needs_ocr"):
        return _ocr_invoice(spec)
    review = bool(spec.get("review"))
    taxable = int(spec.get("taxable_paise", 100000))
    grand = int(spec["grand_total_paise"])
    gt_status = FieldStatus.LOW_CONFIDENCE if review else FieldStatus.OK
    header = InvoiceHeader(
        supplier_name=_f(spec.get("supplier_name", "ACME Supplies Pvt Ltd")),
        supplier_gstin=_f(spec["supplier_gstin"]),
        supplier_address=_f("1 Industrial Rd"),
        buyer_name=_f("Gifsy Solutions"),
        buyer_gstin=_f("27AAAAA0000A1Z5"),
        buyer_address=_f("2 Corporate Ave"),
        invoice_number=_f(spec["invoice_number"]),
        invoice_date=_f(date.fromisoformat(spec["invoice_date"])),
        place_of_supply=_f("Maharashtra (27)"),
        po_ref=_missing(),
    )
    totals = InvoiceTotals(
        total_taxable_paise=_f(taxable),
        total_cgst_paise=_f(0),
        total_sgst_paise=_f(0),
        total_igst_paise=_f(grand - taxable),
        round_off_paise=_f(0),
        grand_total_paise=_f(grand, gt_status, conf=0.4 if review else 0.99),
        amount_in_words=_f("Rupees ..."),
    )
    line = InvoiceLine(
        line_no=1, description=_f("Widget"), hsn_sac=_f("847130"),
        quantity=_f(Decimal("1")), unit=_f("NOS"),
        unit_rate_paise=_f(taxable), taxable_paise=_f(taxable),
        gst_rate=_f(Decimal("18.00")), cgst_paise=_f(0), sgst_paise=_f(0),
        igst_paise=_f(grand - taxable), line_total_paise=_f(grand),
    )
    return ExtractedInvoice(
        schema_version=CANONICAL_SCHEMA_VERSION, doc_type="gst_invoice",
        source_engine="text_layer/1.0", page_count=1, needs_ocr=False,
        review_needed=review,
        review_reasons=["grand total low confidence"] if review else [],
        header=header, lines=[line], totals=totals,
        arithmetic=ArithmeticChecks(True, True, True, True, 0),
        raw_text=json.dumps(spec, sort_keys=True), content_hash="", dedup_key="",
    )


class FakeExtractor:
    name = "fake/1.0"

    def extract(self, pdf_bytes: bytes, *, doc_type: str = "gst_invoice") -> ExtractedInvoice:
        spec = json.loads(pdf_bytes.decode("utf-8"))
        if spec.get("corrupt"):
            raise ValueError("no readable content")
        return _extracted(spec)


def _spec(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "supplier_gstin": SUPPLIER_GSTIN,
        "invoice_number": "INV-001",
        "invoice_date": "2026-05-15",
        "grand_total_paise": 118000,
        "taxable_paise": 100000,
    }
    base.update(overrides)
    return base


def _pdf(spec: dict[str, Any]) -> bytes:
    return json.dumps(spec).encode("utf-8")


# --------------------------------------------------------------------- fixture

@pytest.fixture
def client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("FILES_DIR", f"{tmp_path}/_files")
    monkeypatch.setattr(service, "get_extractor", lambda *a, **k: FakeExtractor())
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


def _upload(client: TestClient, *specs: dict[str, Any]) -> Any:
    files = [("files", (f"inv{i}.pdf", _pdf(s), "application/pdf"))
             for i, s in enumerate(specs)]
    return client.post("/api/v1/expense/invoices", files=files)


# --------------------------------------------------------------------- tests

def test_upload_extracted_happy(client: TestClient) -> None:
    r = _upload(client, _spec())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invoice_count"] == 1
    outcome = body["outcomes"][0]
    assert outcome["status"] == "EXTRACTED"
    assert outcome["invoice_id"] is not None
    assert outcome["grand_total_paise"] == 118000
    # the route echoes the stored filename onto each per-file outcome
    assert outcome["filename"] == "inv0.pdf"
    # a clean EXTRACTED file carries no review flags
    assert outcome["review_reasons"] == []
    # register shows it
    reg = client.get("/api/v1/expense/invoices").json()
    assert len(reg) == 1 and reg[0]["invoice_number"] == "INV-001"


def test_upload_needs_review(client: TestClient) -> None:
    r = _upload(client, _spec(invoice_number="INV-REVIEW", review=True))
    assert r.status_code == 201, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "NEEDS_REVIEW"
    assert outcome["filename"] == "inv0.pdf"
    # a NEEDS_REVIEW outcome carries the extraction's review reasons for the FE list
    assert outcome["review_reasons"] == ["grand total low confidence"]
    detail = client.get(f"/api/v1/expense/invoices/{outcome['invoice_id']}").json()
    gt = next(f for f in detail["fields"] if f["field_path"] == "totals.grand_total_paise")
    assert gt["status"] == "LOW_CONFIDENCE"


def test_upload_needs_ocr(client: TestClient) -> None:
    r = _upload(client, _spec(needs_ocr=True))
    assert r.status_code == 201, r.text
    assert r.json()["outcomes"][0]["status"] == "NEEDS_OCR"


def test_upload_rejected_on_unreadable(client: TestClient) -> None:
    r = _upload(client, {"corrupt": True})
    assert r.status_code == 201, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "REJECTED"
    assert "unreadable" in (outcome["message"] or "")


def test_duplicate_upload_returns_409_with_existing_id(client: TestClient) -> None:
    first = _upload(client, _spec()).json()["outcomes"][0]
    existing_id = first["invoice_id"]
    # Same identity key -> hard dedup.
    r = _upload(client, _spec())
    assert r.status_code == 409, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "DUPLICATE"
    assert outcome["duplicate_of"] == existing_id
    assert outcome["invoice_id"] is None
    # only the ONE invoice exists
    assert len(client.get("/api/v1/expense/invoices").json()) == 1


def test_delete_then_reupload_succeeds(client: TestClient) -> None:
    first = _upload(client, _spec()).json()["outcomes"][0]
    inv_id = first["invoice_id"]
    assert client.delete(f"/api/v1/expense/invoices/{inv_id}").status_code == 200
    # re-upload of the same identity now succeeds (the collision is gone)
    r = _upload(client, _spec())
    assert r.status_code == 201, r.text
    assert r.json()["outcomes"][0]["status"] == "EXTRACTED"


def test_corrections_apply_and_audit(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="INV-C", review=True)
                     ).json()["outcomes"][0]["invoice_id"]
    r = client.patch(
        f"/api/v1/expense/invoices/{inv_id}/reviews",
        json={"corrections": [{"field_path": "header.supplier_name", "value": "Corrected Co"}]},
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["supplier_name"] == "Corrected Co"
    fld = next(f for f in detail["fields"] if f["field_path"] == "header.supplier_name")
    assert fld["status"] == "CORRECTED" and fld["source_engine"] == "human"

    db = client.app.state.TestSession()
    corr = db.execute(select(InvoiceCorrection)
                      .where(InvoiceCorrection.invoice_id == inv_id)).scalars().all()
    assert len(corr) == 1 and corr[0].new_value == "Corrected Co"
    logged = db.execute(select(AuditLog)
                        .where(AuditLog.action == "expense.invoice_corrected")
                        ).scalars().all()
    assert logged
    db.close()


def test_unknown_correction_field_400(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]
    r = client.patch(f"/api/v1/expense/invoices/{inv_id}/reviews",
                     json={"corrections": [{"field_path": "header.bogus", "value": "x"}]})
    assert r.status_code == 400


def test_confirm_blocked_then_freezes(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="INV-CONF", review=True)
                     ).json()["outcomes"][0]["invoice_id"]
    # blocked: grand_total is still LOW_CONFIDENCE
    blocked = client.patch(f"/api/v1/expense/invoices/{inv_id}/reviews",
                           json={"confirm": True})
    assert blocked.status_code == 409, blocked.text
    # correct the weak required field, then confirm in one call
    r = client.patch(
        f"/api/v1/expense/invoices/{inv_id}/reviews",
        json={"corrections": [{"field_path": "totals.grand_total_paise", "value": "118000"}],
              "confirm": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CONFIRMED"
    assert body["confirmed_by"] == "mis" and body["confirmed_at"] is not None
    # confirmed invoice is immutable to further corrections
    again = client.patch(
        f"/api/v1/expense/invoices/{inv_id}/reviews",
        json={"corrections": [{"field_path": "header.supplier_name", "value": "Z"}]},
    )
    assert again.status_code == 409


def test_non_admin_cannot_delete_confirmed_but_can_delete_unconfirmed(
    client: TestClient,
) -> None:
    # unconfirmed: MIS (module grant, not admin) may delete
    unconf = _upload(client, _spec(invoice_number="INV-U")
                     ).json()["outcomes"][0]["invoice_id"]
    assert client.delete(f"/api/v1/expense/invoices/{unconf}").status_code == 200

    # confirmed: MIS is blocked, ADMIN succeeds
    conf = _upload(client, _spec(invoice_number="INV-K")
                   ).json()["outcomes"][0]["invoice_id"]
    assert client.patch(f"/api/v1/expense/invoices/{conf}/reviews",
                        json={"confirm": True}).status_code == 200
    assert client.delete(f"/api/v1/expense/invoices/{conf}").status_code == 403
    _as(client, ADMIN)
    assert client.delete(f"/api/v1/expense/invoices/{conf}").status_code == 200


def test_module_gate_403_for_non_granted_user(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]
    _as(client, OUTSIDER)
    assert _upload(client, _spec(invoice_number="X")).status_code == 403
    assert client.get("/api/v1/expense/invoices").status_code == 403
    assert client.get(f"/api/v1/expense/invoices/{inv_id}").status_code == 403
    assert client.get(f"/api/v1/expense/invoices/{inv_id}/reviews").status_code == 403
    assert client.delete(f"/api/v1/expense/invoices/{inv_id}").status_code == 403


def test_register_filters_and_csv(client: TestClient) -> None:
    _upload(client, _spec(invoice_number="INV-A", supplier_name="Alpha Traders"))
    _upload(client, _spec(invoice_number="INV-B", supplier_name="Beta Supplies"))
    # supplier filter
    hits = client.get("/api/v1/expense/invoices", params={"supplier": "Alpha"}).json()
    assert len(hits) == 1 and hits[0]["invoice_number"] == "INV-A"
    # gstin filter (both share the supplier GSTIN)
    assert len(client.get("/api/v1/expense/invoices",
                          params={"gstin": SUPPLIER_GSTIN}).json()) == 2
    # CSV export
    csv = client.get("/api/v1/expense/invoices.csv")
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    text = csv.text
    assert "Invoice No,Date,Supplier" in text
    assert "INV-A" in text and "INV-B" in text


def test_missing_invoice_404(client: TestClient) -> None:
    assert client.get("/api/v1/expense/invoices/99999").status_code == 404
    assert client.delete("/api/v1/expense/invoices/99999").status_code == 404
    assert client.patch("/api/v1/expense/invoices/99999/reviews",
                        json={"confirm": True}).status_code == 404
