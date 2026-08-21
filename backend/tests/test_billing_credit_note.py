"""Client credit-note capture + PO-match over HTTP — the CLONE of test_billing_invoice.

Same harness (in-memory sqlite StaticPool, FK-pragma ON, overridden get_db + current_user,
FILES_DIR -> tmp) and the same FAKE extractor injected via `creditnote_service.get_extractor`,
so each case controls exactly what the "PDF" yields. The seed builds a real money graph to
credit AGAINST: a client + project + PO (three products) and a CONFIRMED billing_invoice that
BILLED two of them — the widget PO line (qty 6) and the gadget PO line (qty 3) — while the
third "gizmo" PO line is NEVER billed (so a CN may not credit it). Plus an UPLOADED invoice to
prove a CN can't confirm against an unconfirmed invoice.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app.db import Base, get_db
from app.modules.billing import creditnote_service as service
from app.modules.billing import invoice_service
from app.modules.billing.creditnote_routes import router
from app.modules.billing.models import (
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
)
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
from app.modules.files.models import StoredFile
from app.modules.projects import service as projects_service
from app.modules.sales_orders.models import POLineItem, Product, PurchaseOrder
from app.platform.auth import current_user
from app.platform.models import AuditLog, Level, User
from tests.rbac_util import make_role, make_user

OUR_GSTIN = "27AAACG1234A1Z5"       # us — the supplier on every client credit note
CLIENT_GSTIN = "29AABCC1111C1Z0"    # the client — the buyer / dedup discriminator

OPERATOR = make_user("ops", role=make_role(module_levels={"billing": Level.OPERATE}))
VIEWER = make_user("vwr", role=make_role(module_levels={"billing": Level.VIEW}))
MANAGER = make_user("mgr", role=make_role(module_levels={"billing": Level.MANAGE}))
OUTSIDER = make_user("out", role=make_role())


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


def _line(spec: dict[str, Any], line_no: int) -> InvoiceLine:
    taxable = int(spec.get("taxable_paise", 200000))
    grand = int(spec.get("line_total_paise", taxable))
    return InvoiceLine(
        line_no=line_no,
        description=_f(spec["description"]),
        hsn_sac=_f(spec.get("hsn", "847130")),
        quantity=_f(Decimal(str(spec.get("qty", 2)))),
        unit=_f("NOS"),
        unit_rate_paise=_f(int(spec.get("unit_rate_paise", 100000))),
        taxable_paise=_f(taxable),
        gst_rate=_f(Decimal("18.00")),
        cgst_paise=_f(0), sgst_paise=_f(0), igst_paise=_f(grand - taxable),
        line_total_paise=_f(grand),
    )


def _extracted(spec: dict[str, Any]) -> ExtractedInvoice:
    review = bool(spec.get("review"))
    taxable = int(spec.get("total_taxable_paise", 200000))
    grand = int(spec["grand_total_paise"])
    gt_status = FieldStatus.LOW_CONFIDENCE if review else FieldStatus.OK
    header = InvoiceHeader(
        supplier_name=_f("Gifsy Solutions"),
        supplier_gstin=_f(spec.get("supplier_gstin", OUR_GSTIN)),
        supplier_address=_f("1 Our Rd"),
        buyer_name=_f("Client Co"),
        buyer_gstin=_f(spec.get("buyer_gstin", CLIENT_GSTIN)),
        buyer_address=_f("2 Client Ave"),
        invoice_number=_f(spec["cn_number"]),          # header number == the CN number
        invoice_date=_f(date.fromisoformat(spec["cn_date"])),
        place_of_supply=_f("Karnataka (29)"),
        po_ref=_missing(),
    )
    totals = InvoiceTotals(
        total_taxable_paise=_f(taxable),
        total_cgst_paise=_f(0), total_sgst_paise=_f(0),
        total_igst_paise=_f(grand - taxable),
        round_off_paise=_f(0),
        grand_total_paise=_f(grand, gt_status, conf=0.4 if review else 0.99),
        amount_in_words=_f("Rupees ..."),
    )
    line_specs = spec.get("lines", [{"description": "Widget WID-1", "hsn": "847130",
                                     "unit_rate_paise": 100000, "qty": 2}])
    lines = [_line(ls, i) for i, ls in enumerate(line_specs, start=1)]
    return ExtractedInvoice(
        schema_version=CANONICAL_SCHEMA_VERSION, doc_type="gst_invoice",
        source_engine="text_layer/1.0", page_count=1, needs_ocr=False,
        review_needed=review,
        review_reasons=["grand total low confidence"] if review else [],
        header=header, lines=lines, totals=totals,
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
        "cn_number": "CCN-001",
        "cn_date": "2026-06-20",
        "grand_total_paise": 236000,
        "total_taxable_paise": 200000,
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

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn: Any, _record: Any) -> None:  # noqa: ANN401
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    seed = TestSession()
    seed_client = projects_service.create_client(
        seed, name="Client Co", code="CLI", actor_uid="adm")
    seed.flush()
    project = projects_service.create_project(
        seed, client_id=seed_client.id, name="Spine Project", actor_uid="adm")
    seed.flush()
    widget = Product(code="WID-1", name="Widget", hsn="847130", uom="NOS")
    gadget = Product(code="GAD-2", name="Gadget", hsn="852990", uom="NOS")
    gizmo = Product(code="GIZ-3", name="Gizmo", hsn="853890", uom="NOS")
    seed.add_all([widget, gadget, gizmo])
    seed.flush()
    po = PurchaseOrder(
        po_number="PO-1", client_id=seed_client.id, project_id=project.id,
        po_date=date(2026, 6, 1), status="CONFIRMED")
    seed.add(po)
    seed.flush()
    widget_line = POLineItem(
        po_id=po.id, product_id=widget.id, description="Widget WID-1", uom="NOS",
        ordered_qty=Decimal("10"), cost_price_paise=80000, sell_price_paise=100000)
    gadget_line = POLineItem(
        po_id=po.id, product_id=gadget.id, description="Gadget GAD-2", uom="NOS",
        ordered_qty=Decimal("5"), cost_price_paise=40000, sell_price_paise=50000)
    # A PO line the CONFIRMED invoice never bills — a CN may NOT credit it.
    gizmo_line = POLineItem(
        po_id=po.id, product_id=gizmo.id, description="Gizmo GIZ-3", uom="NOS",
        ordered_qty=Decimal("7"), cost_price_paise=30000, sell_price_paise=40000)
    seed.add_all([widget_line, gadget_line, gizmo_line])
    seed.flush()

    inv_file = StoredFile(
        kind="sales-source", filename="inv.pdf", content_type="application/pdf",
        size=1, storage_ref="ref-inv", uploaded_by="adm", module_key="billing")
    seed.add(inv_file)
    seed.flush()

    # A CONFIRMED billing_invoice that BILLED two lines: the widget PO line (qty 6) and the
    # gadget PO line (qty 3). So invoiced_qty_for_po_line(widget_line) == 6 before any CN
    # confirms; the billed set the CN may credit is {widget_line, gadget_line} (NOT gizmo_line).
    conf_inv = SalesInvoice(
        client_id=seed_client.id, po_id=po.id, source_file_id=inv_file.id,
        invoice_number="CINV-CONF", invoice_date=date(2026, 6, 10),
        buyer_gstin=CLIENT_GSTIN, grand_total_paise=708000, total_taxable_paise=600000,
        status=SalesInvoiceStatus.CONFIRMED.value)
    seed.add(conf_inv)
    seed.flush()
    seed.add_all([
        SalesInvoiceLine(
            invoice_id=conf_inv.id, po_line_item_id=widget_line.id, line_no=1,
            description="Widget WID-1", quantity=Decimal("6"), match_status="MATCHED"),
        SalesInvoiceLine(
            invoice_id=conf_inv.id, po_line_item_id=gadget_line.id, line_no=2,
            description="Gadget GAD-2", quantity=Decimal("3"), match_status="MATCHED"),
    ])
    # An UPLOADED (unconfirmed) invoice — a CN may not confirm against it.
    draft_inv = SalesInvoice(
        client_id=seed_client.id, po_id=po.id, source_file_id=inv_file.id,
        invoice_number="CINV-DRAFT", invoice_date=date(2026, 6, 11),
        buyer_gstin=CLIENT_GSTIN, grand_total_paise=236000, total_taxable_paise=200000,
        status=SalesInvoiceStatus.UPLOADED.value)
    seed.add(draft_inv)
    seed.commit()
    client_id = seed_client.id
    widget_line_id, gadget_line_id = widget_line.id, gadget_line.id
    gizmo_line_id = gizmo_line.id
    conf_inv_id, draft_inv_id = conf_inv.id, draft_inv.id
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
    app.dependency_overrides[current_user] = lambda: OPERATOR
    app.state.TestSession = TestSession
    app.state.client_id = client_id
    app.state.widget_line_id = widget_line_id
    app.state.gadget_line_id = gadget_line_id
    app.state.gizmo_line_id = gizmo_line_id
    app.state.conf_inv_id = conf_inv_id
    app.state.draft_inv_id = draft_inv_id
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _upload(client: TestClient, spec: dict[str, Any], *, invoice_id: int | None = None) -> Any:
    files = {"file": ("cn.pdf", _pdf(spec), "application/pdf")}
    data = {"invoice_id": str(invoice_id if invoice_id is not None
                              else client.app.state.conf_inv_id)}
    return client.post("/api/v1/billing/credit-notes", files=files, data=data)


def _confirm(client: TestClient, cn_id: int) -> Any:
    return client.patch(f"/api/v1/billing/credit-notes/{cn_id}/review", json={"confirm": True})


# --------------------------------------------------------------------- tests

def test_upload_extract_persist_and_automatch(client: TestClient) -> None:
    r = _upload(client, _spec())
    assert r.status_code == 201, r.text
    outcome = r.json()["outcomes"][0]
    # a single line that matches the PO's Widget line -> MATCHED, ready to confirm
    assert outcome["status"] == "MATCHED"
    assert outcome["cn_id"] is not None
    assert outcome["cn_number"] == "CCN-001"

    detail = client.get(f"/api/v1/billing/credit-notes/{outcome['cn_id']}").json()
    assert detail["invoice_id"] == client.app.state.conf_inv_id
    assert detail["referenced_invoice"]["invoice_number"] == "CINV-CONF"
    assert detail["source_file"]["filename"] == "cn.pdf"
    assert len(detail["lines"]) == 1
    line = detail["lines"][0]
    assert line["match_status"] == "MATCHED"
    assert line["po_line_item_id"] == client.app.state.widget_line_id
    assert line["po_line_label"] == "Widget — Widget WID-1"
    assert line["quantity"] == "2.000"
    # register shows it, carrying the credited invoice number
    reg = client.get("/api/v1/billing/credit-notes").json()
    assert len(reg) == 1 and reg[0]["invoice_number"] == "CINV-CONF"


def test_manual_match_leftover_line(client: TestClient) -> None:
    cn_id = _upload(client, _spec(lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Mystery item", "hsn": "000000", "unit_rate_paise": 1, "qty": 1},
    ])).json()["outcomes"][0]["cn_id"]
    detail = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()
    assert detail["status"] == "NEEDS_MATCH"
    leftover = next(ln for ln in detail["lines"] if ln["match_status"] == "UNMATCHED")

    mm = client.patch(
        f"/api/v1/billing/credit-notes/{cn_id}/lines/{leftover['id']}/match",
        json={"po_line_item_id": client.app.state.gadget_line_id})
    assert mm.status_code == 200, mm.text
    assert mm.json()["status"] == "MATCHED"
    mapped = next(ln for ln in mm.json()["lines"] if ln["id"] == leftover["id"])
    assert mapped["match_status"] == "MANUAL"
    assert mapped["po_line_item_id"] == client.app.state.gadget_line_id


def test_upload_rejected_against_unconfirmed_invoice(client: TestClient) -> None:
    # F4: a credit note reverses a CONFIRMED invoice — uploading one against the UPLOADED
    # (non-confirmed) invoice is rejected up front (400), enforcing confirm-then-credit order.
    r = _upload(client, _spec(cn_number="CCN-DRAFT"), invoice_id=client.app.state.draft_inv_id)
    assert r.status_code == 400, r.text
    assert "confirm it before crediting" in r.json()["detail"].lower()
    # ...and nothing was persisted against that draft invoice.
    reg = client.get(
        f"/api/v1/billing/credit-notes?invoice_id={client.app.state.draft_inv_id}").json()
    assert reg == []


def test_confirm_sets_confirmed_and_is_immutable(client: TestClient) -> None:
    cn_id = _upload(client, _spec(cn_number="CCN-OK")).json()["outcomes"][0]["cn_id"]
    r = _confirm(client, cn_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CONFIRMED"
    # a confirmed CN is immutable to further corrections
    again = client.patch(f"/api/v1/billing/credit-notes/{cn_id}/review",
                         json={"corrections": [{"field": "reason", "value": "late"}]})
    assert again.status_code == 409
    # ...and cannot be cancelled
    _as(client, MANAGER)
    assert client.post(f"/api/v1/billing/credit-notes/{cn_id}/cancel").status_code == 409


def test_confirmed_cn_reduces_invoiced_qty(client: TestClient) -> None:
    widget_line_id = client.app.state.widget_line_id
    db = client.app.state.TestSession()
    assert invoice_service.invoiced_qty_for_po_line(db, widget_line_id) == Decimal("6")
    db.close()

    # Credit 2 units of the widget line and confirm.
    cn_id = _upload(client, _spec(
        cn_number="CCN-CR", lines=[{"description": "Widget WID-1", "hsn": "847130",
                                    "unit_rate_paise": 100000, "qty": 2}],
    )).json()["outcomes"][0]["cn_id"]
    assert _confirm(client, cn_id).status_code == 200

    # 6 (confirmed invoice) − 2 (confirmed CN) = 4 units reopened for re-invoicing.
    db = client.app.state.TestSession()
    assert invoice_service.invoiced_qty_for_po_line(db, widget_line_id) == Decimal("4")
    db.close()


def test_cn_dedup_client_keyed(client: TestClient) -> None:
    first = _upload(client, _spec()).json()["outcomes"][0]
    existing_id = first["cn_id"]
    # exact same client / number / date / total -> hard dedup
    r = _upload(client, _spec())
    assert r.status_code == 409, r.text
    dup = r.json()["outcomes"][0]
    assert dup["status"] == "DUPLICATE" and dup["cn_id"] is None
    assert len(client.get("/api/v1/billing/credit-notes").json()) == 1
    # the register still holds only the original
    assert client.get("/api/v1/billing/credit-notes").json()[0]["id"] == existing_id


def test_review_correction_applies_and_audits(client: TestClient) -> None:
    cn_id = _upload(client, _spec(cn_number="CCN-C", review=True)
                    ).json()["outcomes"][0]["cn_id"]
    r = client.patch(f"/api/v1/billing/credit-notes/{cn_id}/review",
                     json={"corrections": [{"field": "grand_total_paise", "value": "150000"}]})
    assert r.status_code == 200, r.text
    assert r.json()["grand_total_paise"] == 150000

    db = client.app.state.TestSession()
    logged = db.execute(select(AuditLog)
                        .where(AuditLog.action == "billing.credit_note_corrected")).scalars().all()
    assert logged
    db.close()


def test_rbac_viewer_and_manager_gates(client: TestClient) -> None:
    cn_id = _upload(client, _spec()).json()["outcomes"][0]["cn_id"]

    # VIEWER: can read, cannot upload / match / cancel / delete
    _as(client, VIEWER)
    assert client.get("/api/v1/billing/credit-notes").status_code == 200
    assert client.get(f"/api/v1/billing/credit-notes/{cn_id}").status_code == 200
    assert _upload(client, _spec(cn_number="X")).status_code == 403
    assert client.post(f"/api/v1/billing/credit-notes/{cn_id}/match").status_code == 403

    # OPERATOR: can match, cannot cancel / delete (MANAGE)
    _as(client, OPERATOR)
    assert client.post(f"/api/v1/billing/credit-notes/{cn_id}/match").status_code == 200
    assert client.post(f"/api/v1/billing/credit-notes/{cn_id}/cancel").status_code == 403
    assert client.delete(f"/api/v1/billing/credit-notes/{cn_id}").status_code == 403

    # MANAGER can delete; OUTSIDER has no access at all
    _as(client, MANAGER)
    assert client.delete(f"/api/v1/billing/credit-notes/{cn_id}").status_code == 200
    _as(client, OUTSIDER)
    assert client.get("/api/v1/billing/credit-notes").status_code == 403


def test_missing_credit_note_404(client: TestClient) -> None:
    assert client.get("/api/v1/billing/credit-notes/99999").status_code == 404


def test_manual_match_rejects_cross_po_line(client: TestClient) -> None:
    cn_id = _upload(client, _spec(lines=[
        {"description": "Mystery", "hsn": "000000", "unit_rate_paise": 1, "qty": 1}])
    ).json()["outcomes"][0]["cn_id"]
    line_id = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()["lines"][0]["id"]
    r = client.patch(f"/api/v1/billing/credit-notes/{cn_id}/lines/{line_id}/match",
                     json={"po_line_item_id": 999999})
    assert r.status_code == 400, r.text


def test_cn_cannot_credit_line_the_invoice_did_not_bill(client: TestClient) -> None:
    # F1: the gizmo PO line exists on the same PO but the credited invoice never billed it.
    # (a) auto-match: a gizmo-looking CN line is NOT a candidate -> stays UNMATCHED.
    cn_id = _upload(client, _spec(cn_number="CCN-GIZ", lines=[
        {"description": "Gizmo GIZ-3", "hsn": "853890", "unit_rate_paise": 40000, "qty": 1}])
    ).json()["outcomes"][0]["cn_id"]
    detail = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()
    assert detail["status"] == "NEEDS_MATCH"
    line = detail["lines"][0]
    assert line["match_status"] == "UNMATCHED"

    # (b) manual match to that un-billed gizmo PO line is rejected (400).
    r = client.patch(f"/api/v1/billing/credit-notes/{cn_id}/lines/{line['id']}/match",
                     json={"po_line_item_id": client.app.state.gizmo_line_id})
    assert r.status_code == 400, r.text
    assert "not billed by the credited invoice" in r.json()["detail"].lower()


def test_over_credit_soft_flag_and_invoiced_qty_floors_at_zero(client: TestClient) -> None:
    # F1: the credited invoice billed 6 widget units; credit 8 (more than billed).
    widget_line_id = client.app.state.widget_line_id
    cn_id = _upload(client, _spec(cn_number="CCN-OVER", lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 8}])
    ).json()["outcomes"][0]["cn_id"]
    r = _confirm(client, cn_id)
    # SOFT flag — confirm still SUCCEEDS (never blocks; accounting software is source of truth).
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"
    assert "over_credited" in (r.json()["reason"] or "")

    # The §6 rollup FLOORS at 0 (6 − 8 = −2 would be negative) so the over-invoice baseline
    # can't be driven below zero.
    db = client.app.state.TestSession()
    assert invoice_service.invoiced_qty_for_po_line(db, widget_line_id) == Decimal("0")
    db.close()


def test_review_envelope_needs_ocr_and_reasons_exposed(client: TestClient) -> None:
    # A review-flagged CN (grand-total LOW_CONFIDENCE) persists + exposes the per-field envelope,
    # needs_ocr, and review_reasons — parity with the invoice lane's review screen.
    cn_id = _upload(client, _spec(cn_number="CCN-ENV", review=True)
                    ).json()["outcomes"][0]["cn_id"]
    detail = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()

    assert detail["needs_ocr"] is False
    assert detail["review_reasons"] == ["grand total low confidence"]
    fields = {f["field_path"]: f for f in detail["fields"]}
    # 8 header/totals fields carry an envelope (reason is free text — no envelope).
    assert set(fields) == {
        "cn_number", "cn_date", "total_taxable_paise", "total_cgst_paise",
        "total_sgst_paise", "total_igst_paise", "round_off_paise", "grand_total_paise",
    }
    assert fields["grand_total_paise"]["status"] == "LOW_CONFIDENCE"
    assert fields["grand_total_paise"]["confidence"] == pytest.approx(0.4)
    assert fields["cn_number"]["status"] == "OK"
    assert fields["cn_number"]["value_norm"] == "CCN-ENV"


def test_review_correction_flips_envelope_to_corrected(client: TestClient) -> None:
    # Correcting a flagged field flips its envelope to CORRECTED (human-sourced) so the editor
    # shows it resolved — mirroring the invoice lane.
    cn_id = _upload(client, _spec(cn_number="CCN-FIX", review=True)
                    ).json()["outcomes"][0]["cn_id"]
    client.patch(f"/api/v1/billing/credit-notes/{cn_id}/review",
                 json={"corrections": [{"field": "grand_total_paise", "value": "150000"}]})
    detail = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()
    gt = next(f for f in detail["fields"] if f["field_path"] == "grand_total_paise")
    assert gt["status"] == "CORRECTED"
    assert gt["value_norm"] == "150000"
    assert gt["source_engine"] == "human"


def test_line_billed_and_already_credited_qty(client: TestClient) -> None:
    # A matched line surfaces the referenced invoice's billed qty (the over-credit ceiling); an
    # unmatched line has no PO line to compare against.
    cn_id = _upload(client, _spec(cn_number="CCN-A", lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Mystery item", "hsn": "000000", "unit_rate_paise": 1, "qty": 1},
    ])).json()["outcomes"][0]["cn_id"]
    detail = client.get(f"/api/v1/billing/credit-notes/{cn_id}").json()
    widget = next(ln for ln in detail["lines"]
                  if ln["po_line_item_id"] == client.app.state.widget_line_id)
    unmatched = next(ln for ln in detail["lines"] if ln["match_status"] == "UNMATCHED")
    assert Decimal(widget["billed_qty"]) == Decimal("6")        # invoice billed 6 on this line
    assert Decimal(widget["already_credited_qty"]) == Decimal("0")
    assert unmatched["billed_qty"] is None
    assert unmatched["already_credited_qty"] is None


def test_already_credited_reflects_other_confirmed_cn(client: TestClient) -> None:
    # already_credited_qty rolls up OTHER confirmed CNs on the same PO line (this CN excluded).
    first = _upload(client, _spec(cn_number="CCN-1")).json()["outcomes"][0]["cn_id"]
    assert _confirm(client, first).status_code == 200
    second = _upload(client, _spec(cn_number="CCN-2")).json()["outcomes"][0]["cn_id"]
    detail = client.get(f"/api/v1/billing/credit-notes/{second}").json()
    widget = next(ln for ln in detail["lines"]
                  if ln["po_line_item_id"] == client.app.state.widget_line_id)
    assert Decimal(widget["billed_qty"]) == Decimal("6")
    assert Decimal(widget["already_credited_qty"]) == Decimal("2")  # first CN credited 2
