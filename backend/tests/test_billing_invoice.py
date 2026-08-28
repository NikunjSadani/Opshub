"""Client-invoice capture + PO-match over HTTP.

Mirrors the expense harness (in-memory sqlite StaticPool, FK-pragma ON, overridden
get_db + current_user, FILES_DIR -> tmp). The extraction engine is a FAKE injected via
`invoice_service.get_extractor`, so each case controls exactly what the "PDF" yields:
every uploaded blob is a JSON spec the fake turns into an `ExtractedInvoice` (supplier=US,
buyer=the client — the sales inversion). A client + project + PO (two products) are seeded
so the matcher has real PO lines to bind to.
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
from app.modules.billing import ar_service, matcher
from app.modules.billing import invoice_service as service
from app.modules.billing.invoice_routes import router
from app.modules.billing.matcher import _Candidate
from app.modules.billing.models import (
    PaymentReceipt,
    SalesInvoice,
    SalesInvoiceCorrection,
    SalesInvoiceLine,
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
from app.modules.projects import service as projects_service
from app.modules.projects.models import Project
from app.modules.sales_orders.models import (
    LineStatus,
    POLineItem,
    Product,
    PurchaseOrder,
)
from app.platform.auth import current_user
from app.platform.models import AuditLog, Level, Setting, User
from tests.rbac_util import make_role, make_user

OUR_GSTIN = "27AAACG1234A1Z5"       # us — the supplier on every client invoice
CLIENT_GSTIN = "29AABCC1111C1Z0"    # the client — the buyer / dedup discriminator

ADMIN = make_user("adm", role=make_role("Administrator", is_system=True))
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
        invoice_number=_f(spec["invoice_number"]),
        invoice_date=_f(date.fromisoformat(spec["invoice_date"])),
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
        "invoice_number": "CINV-001",
        "invoice_date": "2026-06-15",
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
    seed.add_all([widget, gadget])
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
    seed.add_all([widget_line, gadget_line])
    seed.commit()
    client_id, po_id = seed_client.id, po.id
    project_id = project.id
    widget_line_id, gadget_line_id = widget_line.id, gadget_line.id
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
    app.state.po_id = po_id
    app.state.project_id = project_id
    app.state.widget_line_id = widget_line_id
    app.state.gadget_line_id = gadget_line_id
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _set_company_gstin(client: TestClient, value: Any) -> None:
    db = client.app.state.TestSession()
    db.add(Setting(key=service.COMPANY_GSTIN_SETTING, value=value))
    db.commit()
    db.close()


def _upload(
    client: TestClient, *specs: dict[str, Any],
    client_id: int | None = None, po_id: int | None | str = "default",
    project_id: int | None = None,
) -> Any:
    files = [("files", (f"cinv{i}.pdf", _pdf(s), "application/pdf"))
             for i, s in enumerate(specs)]
    data: dict[str, str] = {
        "client_id": str(client_id if client_id is not None else client.app.state.client_id),
    }
    resolved_po = client.app.state.po_id if po_id == "default" else po_id
    if resolved_po is not None:
        data["po_id"] = str(resolved_po)
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post("/api/v1/billing/invoices", files=files, data=data)


# --------------------------------------------------------------------- tests

def test_upload_extract_persist_and_automatch(client: TestClient) -> None:
    r = _upload(client, _spec())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["invoice_count"] == 1
    outcome = body["outcomes"][0]
    # a single line that matches the PO's Widget line -> MATCHED, ready to confirm
    assert outcome["status"] == "MATCHED"
    assert outcome["invoice_id"] is not None
    assert outcome["grand_total_paise"] == 236000
    assert outcome["filename"] == "cinv0.pdf"

    detail = client.get(f"/api/v1/billing/invoices/{outcome['invoice_id']}").json()
    # supplier=us, buyer=client (the sales inversion is persisted correctly)
    assert detail["supplier_gstin"] == OUR_GSTIN
    assert detail["buyer_gstin"] == CLIENT_GSTIN
    assert len(detail["lines"]) == 1
    line = detail["lines"][0]
    assert line["match_status"] == "MATCHED"
    assert line["po_line_item_id"] == client.app.state.widget_line_id
    # register shows it
    reg = client.get("/api/v1/billing/invoices").json()
    assert len(reg) == 1 and reg[0]["invoice_number"] == "CINV-001"


def test_unmatched_line_parks_in_needs_match(client: TestClient) -> None:
    r = _upload(client, _spec(lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Unknownium ZZZ", "hsn": "000000", "unit_rate_paise": 777, "qty": 1},
    ]))
    assert r.status_code == 201, r.text
    inv_id = r.json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    assert detail["status"] == "NEEDS_MATCH"
    by_no = {ln["line_no"]: ln for ln in detail["lines"]}
    assert by_no[1]["match_status"] == "MATCHED"
    assert by_no[2]["match_status"] == "UNMATCHED" and by_no[2]["po_line_item_id"] is None


def test_manual_match_then_confirm(client: TestClient) -> None:
    inv_id = _upload(client, _spec(lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Mystery item", "hsn": "000000", "unit_rate_paise": 1, "qty": 1},
    ])).json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    unmatched_line = next(ln for ln in detail["lines"] if ln["match_status"] == "UNMATCHED")

    # confirm is blocked while a line is unmatched
    blocked = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                           json={"confirm": True})
    assert blocked.status_code == 409, blocked.text
    assert "unmatched" in blocked.json()["detail"].lower()

    # manually map the leftover line to the Gadget PO line -> MANUAL
    mm = client.patch(
        f"/api/v1/billing/invoices/{inv_id}/lines/{unmatched_line['id']}/match",
        json={"po_line_item_id": client.app.state.gadget_line_id})
    assert mm.status_code == 200, mm.text
    assert mm.json()["status"] == "MATCHED"
    mapped = next(ln for ln in mm.json()["lines"] if ln["id"] == unmatched_line["id"])
    assert mapped["match_status"] == "MANUAL"
    assert mapped["po_line_item_id"] == client.app.state.gadget_line_id

    # now confirm freezes it to the immutable CONFIRMED record
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/review", json={"confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "CONFIRMED"
    assert body["confirmed_by"] == "ops" and body["confirmed_at"] is not None
    # a confirmed invoice is immutable to further corrections
    again = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                         json={"corrections": [{"field_path": "header.buyer_gstin",
                                                "value": OUR_GSTIN}]})
    assert again.status_code == 409


def test_manual_match_rejects_cross_po_line(client: TestClient) -> None:
    inv_id = _upload(client, _spec(lines=[
        {"description": "Mystery", "hsn": "000000", "unit_rate_paise": 1, "qty": 1}])
    ).json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    line_id = detail["lines"][0]["id"]
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/lines/{line_id}/match",
                     json={"po_line_item_id": 999999})
    assert r.status_code == 400, r.text


def test_confirm_blocked_until_required_field_corrected(client: TestClient) -> None:
    # a low-confidence grand total -> NEEDS_REVIEW, blocks confirm even though the line matches
    inv_id = _upload(client, _spec(invoice_number="CINV-REV", review=True)
                     ).json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    assert detail["status"] == "NEEDS_REVIEW"
    blocked = client.patch(f"/api/v1/billing/invoices/{inv_id}/review", json={"confirm": True})
    assert blocked.status_code == 409, blocked.text
    assert "required" in blocked.json()["detail"].lower()
    # correct the weak field + confirm in one call
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                     json={"corrections": [{"field_path": "totals.grand_total_paise",
                                            "value": "236000"}], "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"


def test_sales_dedup_client_gstin_keyed(client: TestClient) -> None:
    first = _upload(client, _spec()).json()["outcomes"][0]
    existing_id = first["invoice_id"]
    # exact same client / number / date / total -> hard dedup (client-GSTIN keyed)
    r = _upload(client, _spec())
    assert r.status_code == 409, r.text
    dup = r.json()["outcomes"][0]
    assert dup["status"] == "DUPLICATE"
    assert dup["duplicate_of"] == existing_id and dup["invoice_id"] is None
    assert len(client.get("/api/v1/billing/invoices").json()) == 1


def test_self_gstin_mismatch_flags_review(client: TestClient) -> None:
    _set_company_gstin(client, OUR_GSTIN)
    # supplier GSTIN on the doc is NOT our company GSTIN -> review flag
    r = _upload(client, _spec(invoice_number="CINV-FOREIGN", supplier_gstin="99ZZZZZ9999Z9Z9"))
    assert r.status_code == 201, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "NEEDS_REVIEW"
    assert any("not our company GSTIN" in reason for reason in outcome["review_reasons"])


def test_self_gstin_match_no_flag(client: TestClient) -> None:
    _set_company_gstin(client, OUR_GSTIN)
    r = _upload(client, _spec(invoice_number="CINV-OK"))  # supplier == OUR_GSTIN default
    assert r.status_code == 201, r.text
    assert r.json()["outcomes"][0]["status"] == "MATCHED"


def test_delete_then_reupload_succeeds(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]
    _as(client, MANAGER)
    assert client.delete(f"/api/v1/billing/invoices/{inv_id}").status_code == 200
    _as(client, OPERATOR)
    r = _upload(client, _spec())
    assert r.status_code == 201, r.text
    assert r.json()["outcomes"][0]["status"] == "MATCHED"


def test_corrections_apply_and_audit(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="CINV-C", review=True)
                     ).json()["outcomes"][0]["invoice_id"]
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                     json={"corrections": [{"field_path": "header.buyer_gstin",
                                            "value": "24AAAAA0000A1Z5"}]})
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["buyer_gstin"] == "24AAAAA0000A1Z5"
    fld = next(f for f in detail["fields"] if f["field_path"] == "header.buyer_gstin")
    assert fld["status"] == "CORRECTED" and fld["source_engine"] == "human"

    db = client.app.state.TestSession()
    corr = db.execute(select(SalesInvoiceCorrection)
                      .where(SalesInvoiceCorrection.invoice_id == inv_id)).scalars().all()
    assert len(corr) == 1 and corr[0].new_value == "24AAAAA0000A1Z5"
    logged = db.execute(select(AuditLog)
                        .where(AuditLog.action == "billing.invoice_corrected")).scalars().all()
    assert logged
    db.close()


def test_cancel_manage_gated(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]
    # OPERATOR cannot cancel (MANAGE)
    assert client.post(f"/api/v1/billing/invoices/{inv_id}/cancel").status_code == 403
    _as(client, MANAGER)
    r = client.post(f"/api/v1/billing/invoices/{inv_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"


def test_rbac_viewer_and_operator_gates(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]

    # VIEWER: can read, cannot upload / match / cancel / delete
    _as(client, VIEWER)
    assert client.get("/api/v1/billing/invoices").status_code == 200
    assert client.get(f"/api/v1/billing/invoices/{inv_id}").status_code == 200
    assert _upload(client, _spec(invoice_number="X")).status_code == 403
    assert client.post(f"/api/v1/billing/invoices/{inv_id}/match").status_code == 403
    assert client.delete(f"/api/v1/billing/invoices/{inv_id}").status_code == 403

    # OPERATOR: can match, cannot cancel / delete (MANAGE)
    _as(client, OPERATOR)
    assert client.post(f"/api/v1/billing/invoices/{inv_id}/match").status_code == 200
    assert client.post(f"/api/v1/billing/invoices/{inv_id}/cancel").status_code == 403
    assert client.delete(f"/api/v1/billing/invoices/{inv_id}").status_code == 403

    # OUTSIDER: no access at all
    _as(client, OUTSIDER)
    assert client.get("/api/v1/billing/invoices").status_code == 403


def test_delete_removes_source_blob(client: TestClient) -> None:
    inv_id = _upload(client, _spec()).json()["outcomes"][0]["invoice_id"]
    _as(client, MANAGER)
    assert client.delete(f"/api/v1/billing/invoices/{inv_id}").status_code == 200
    db = client.app.state.TestSession()
    assert db.query(SalesInvoice).count() == 0
    db.close()


def test_missing_invoice_404(client: TestClient) -> None:
    assert client.get("/api/v1/billing/invoices/99999").status_code == 404
    _as(client, MANAGER)
    assert client.delete("/api/v1/billing/invoices/99999").status_code == 404


# ------------------------------------------------- H1: matcher word-boundary identity

def _score_of(
    description: str, hsn: str, code: str, cand_text: str, cand_hsn: str,
) -> float:
    line = SalesInvoiceLine(
        description=description, hsn_sac=hsn, unit_rate_paise=None, quantity=None)
    cand = _Candidate(
        po_line_id=1, code=code, hsn=cand_hsn, text=cand_text,
        sell_price_paise=None, ordered_qty=None)
    return matcher._score(line, cand)


def test_matcher_identity_requires_word_boundary(client: TestClient) -> None:
    thr = matcher.MATCH_THRESHOLD
    # H1 (reproduced): a short code must NOT substring-match inside an unrelated word.
    assert _score_of("Gasket GAS-100 rubber", "998877", "S1", "Sprocket", "111111") < thr
    assert _score_of("Bearing BRG-9", "998877", "A", "Axle", "222222") < thr
    # A legit hyphenated code STILL auto-matches at a token boundary.
    assert _score_of("Widget WID-1", "847130", "WID-1", "Widget WID-1", "847130") >= thr


# ------------------------------------------------- M1: invoiced_qty rollup + soft over-billing

def _confirm(client: TestClient, inv_id: int) -> Any:
    return client.patch(f"/api/v1/billing/invoices/{inv_id}/review", json={"confirm": True})


def test_over_invoiced_soft_flag_on_second_confirm(client: TestClient) -> None:
    widget_line_id = client.app.state.widget_line_id  # ordered_qty = 10
    a = _upload(client, _spec(
        invoice_number="CINV-A", grand_total_paise=600000, total_taxable_paise=600000,
        lines=[{"description": "Widget WID-1", "hsn": "847130",
                "unit_rate_paise": 100000, "qty": 6}],
    )).json()["outcomes"][0]["invoice_id"]
    ra = _confirm(client, a)
    assert ra.status_code == 200, ra.text
    # 6 of 10 -> under ordered_qty, no soft flag
    assert not any("over_invoiced" in r for r in ra.json()["review_reasons"])

    b = _upload(client, _spec(
        invoice_number="CINV-B", grand_total_paise=600001, total_taxable_paise=600001,
        lines=[{"description": "Widget WID-1", "hsn": "847130",
                "unit_rate_paise": 100000, "qty": 6}],
    )).json()["outcomes"][0]["invoice_id"]
    rb = _confirm(client, b)
    # 6 + 6 = 12 > 10 -> SOFT flag, but confirm STILL succeeds (never blocks)
    assert rb.status_code == 200, rb.text
    assert rb.json()["status"] == "CONFIRMED"
    assert any("over_invoiced" in r for r in rb.json()["review_reasons"])

    # the §6 rollup now sums BOTH confirmed invoices' quantities for that PO line
    db = client.app.state.TestSession()
    assert service.invoiced_qty_for_po_line(db, widget_line_id) == Decimal("12")
    db.close()


# --------------------------------- PO confirm lifecycle: invoice confirm nudges the PO

def test_confirm_invoice_moves_po_to_in_progress(client: TestClient) -> None:
    # The harness seeds the PO as CONFIRMED; confirming a matched client invoice against it
    # nudges the PO CONFIRMED -> IN_PROGRESS (best-effort status hook in confirm_invoice).
    po_id = client.app.state.po_id
    db = client.app.state.TestSession()
    assert db.get(PurchaseOrder, po_id).status == "CONFIRMED"
    db.close()

    inv_id = _upload(client, _spec(invoice_number="CINV-IP")
                     ).json()["outcomes"][0]["invoice_id"]
    assert _confirm(client, inv_id).status_code == 200

    db = client.app.state.TestSession()
    assert db.get(PurchaseOrder, po_id).status == "IN_PROGRESS"
    db.close()


# ------------------------------------------------- M2: delete blocked by AR children

def test_delete_blocked_when_receivable_records_exist(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="CINV-AR")
                     ).json()["outcomes"][0]["invoice_id"]
    assert _confirm(client, inv_id).status_code == 200
    db = client.app.state.TestSession()
    db.add(PaymentReceipt(
        client_id=client.app.state.client_id, invoice_id=inv_id,
        amount_paise=100000, received_on=date(2026, 6, 20)))
    db.commit()
    db.close()
    _as(client, MANAGER)
    r = client.delete(f"/api/v1/billing/invoices/{inv_id}")
    assert r.status_code == 409, r.text
    assert "receivable" in r.json()["detail"].lower()


# ------------------------------------------------- M3: confirm/cancel re-validate under lock

def test_confirm_and_cancel_revalidate_status_under_lock(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="CINV-LOCK")
                     ).json()["outcomes"][0]["invoice_id"]
    assert _confirm(client, inv_id).status_code == 200
    # a re-confirm is rejected under the lock (status re-checked after re-read)
    assert _confirm(client, inv_id).status_code == 409
    # a confirmed invoice cannot be cancelled (cancel re-reads + re-validates too)
    _as(client, MANAGER)
    r = client.post(f"/api/v1/billing/invoices/{inv_id}/cancel")
    assert r.status_code == 409, r.text


# ------------------------------------------------- M4: manual match rejects a retired PO line

def test_manual_match_rejects_closed_po_line(client: TestClient) -> None:
    inv_id = _upload(client, _spec(lines=[
        {"description": "Mystery", "hsn": "000000", "unit_rate_paise": 1, "qty": 1}])
    ).json()["outcomes"][0]["invoice_id"]
    line_id = client.get(f"/api/v1/billing/invoices/{inv_id}").json()["lines"][0]["id"]
    db = client.app.state.TestSession()
    gl = db.get(POLineItem, client.app.state.gadget_line_id)
    assert gl is not None
    gl.line_status = LineStatus.SHORT_CLOSED.value
    db.commit()
    db.close()
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/lines/{line_id}/match",
                     json={"po_line_item_id": client.app.state.gadget_line_id})
    assert r.status_code == 400, r.text
    assert "SHORT_CLOSED" in r.json()["detail"]


# ------------------------------------------------- L1: required field corrected to blank blocks

def test_required_field_corrected_to_blank_blocks_confirm(client: TestClient) -> None:
    inv_id = _upload(client, _spec(invoice_number="CINV-BLANK")
                     ).json()["outcomes"][0]["invoice_id"]
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                     json={"corrections": [{"field_path": "header.buyer_gstin", "value": ""}]})
    assert r.status_code == 200, r.text
    blocked = _confirm(client, inv_id)
    assert blocked.status_code == 409, blocked.text
    assert "buyer_gstin" in blocked.json()["detail"]


# ------------------------------------------- PO-less invoice: confirm without a PO

def test_po_less_invoice_confirms_with_unmatched_lines(client: TestClient) -> None:
    # Some clients never issue a PO. A PO-less invoice (po_id NULL) has no PO lines to match,
    # so it reads as MATCHED on upload (lines stay UNMATCHED) and confirms as-is.
    r = _upload(client, _spec(invoice_number="CINV-NOPO"), po_id=None)
    assert r.status_code == 201, r.text
    outcome = r.json()["outcomes"][0]
    inv_id = outcome["invoice_id"]
    assert outcome["status"] == "MATCHED"

    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    assert detail["po_id"] is None
    # the line never bound to a PO line (nothing to match against)
    assert detail["lines"][0]["match_status"] == "UNMATCHED"
    assert detail["lines"][0]["po_line_item_id"] is None

    r = _confirm(client, inv_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"
    assert r.json()["confirmed_by"] == "ops" and r.json()["confirmed_at"] is not None
    # immutable after confirm
    again = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                         json={"corrections": [{"field_path": "header.buyer_gstin",
                                                "value": OUR_GSTIN}]})
    assert again.status_code == 409, again.text


def test_po_linked_unmatched_line_still_blocks_confirm(client: TestClient) -> None:
    # A PO-linked invoice keeps the full line-matching requirement (unchanged behavior).
    inv_id = _upload(client, _spec(invoice_number="CINV-PO", lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Unknownium ZZZ", "hsn": "000000", "unit_rate_paise": 777, "qty": 1},
    ])).json()["outcomes"][0]["invoice_id"]
    blocked = _confirm(client, inv_id)
    assert blocked.status_code == 409, blocked.text
    assert "match every line" in blocked.json()["detail"].lower()
    assert "unmatched" in blocked.json()["detail"].lower()


def test_po_less_invoice_weak_required_field_still_blocks(client: TestClient) -> None:
    # A PO-less invoice STILL needs its required header fields (a weak grand total blocks).
    inv_id = _upload(client, _spec(invoice_number="CINV-NOPO-REV", review=True),
                     po_id=None).json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    assert detail["po_id"] is None
    assert detail["status"] == "NEEDS_REVIEW"
    blocked = _confirm(client, inv_id)
    assert blocked.status_code == 409, blocked.text
    assert "required" in blocked.json()["detail"].lower()
    # correcting the weak field unblocks confirm
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/review",
                     json={"corrections": [{"field_path": "totals.grand_total_paise",
                                            "value": "236000"}], "confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "CONFIRMED"


def test_derive_status_matched_for_po_less_needs_match_for_po_linked(client: TestClient) -> None:
    # PO-less + lines + no weak required field -> MATCHED (ready to confirm).
    po_less = _upload(client, _spec(invoice_number="CINV-DS-NOPO"), po_id=None
                      ).json()["outcomes"][0]
    assert po_less["status"] == "MATCHED"
    # PO-linked with an unmatched line -> NEEDS_MATCH (unchanged).
    po_linked = _upload(client, _spec(invoice_number="CINV-DS-PO", lines=[
        {"description": "Widget WID-1", "hsn": "847130", "unit_rate_paise": 100000, "qty": 2},
        {"description": "Unknownium ZZZ", "hsn": "000000", "unit_rate_paise": 777, "qty": 1},
    ])).json()["outcomes"][0]
    assert po_linked["status"] == "NEEDS_MATCH"


# ------------------------------------------- project attribution (PO-less P&L)

def _second_client_project(client: TestClient) -> tuple[int, int]:
    """Create a SEPARATE client + ACTIVE project via the projects service; return their ids."""
    db = client.app.state.TestSession()
    other = projects_service.create_client(db, name="Other Co", code="OTH", actor_uid="adm")
    db.flush()
    proj = projects_service.create_project(
        db, client_id=other.id, name="Other Project", actor_uid="adm")
    db.commit()
    ids = (other.id, proj.id)
    db.close()
    return ids


def test_upload_with_valid_project_stamps_it(client: TestClient) -> None:
    # A PO-less upload with a valid same-client ACTIVE project stamps project_id on the invoice.
    project_id = client.app.state.project_id
    r = _upload(client, _spec(invoice_number="CINV-PRJ"), po_id=None, project_id=project_id)
    assert r.status_code == 201, r.text
    inv_id = r.json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/billing/invoices/{inv_id}").json()
    assert detail["po_id"] is None
    assert detail["project_id"] == project_id


def test_upload_project_of_different_client_rejected(client: TestClient) -> None:
    # A project belonging to ANOTHER client is a 400 (and nothing persists).
    _other_client_id, other_project_id = _second_client_project(client)
    r = _upload(client, _spec(invoice_number="CINV-XCLIENT"), po_id=None,
                project_id=other_project_id)
    assert r.status_code == 400, r.text
    assert "does not belong" in r.json()["detail"]
    assert client.get("/api/v1/billing/invoices").json() == []


def test_upload_inactive_project_rejected(client: TestClient) -> None:
    # An ON_HOLD (non-ACTIVE) project is a 400.
    project_id = client.app.state.project_id
    db = client.app.state.TestSession()
    proj = db.get(Project, project_id)
    assert proj is not None
    proj.status = "ON_HOLD"
    db.commit()
    db.close()
    r = _upload(client, _spec(invoice_number="CINV-HOLD"), po_id=None, project_id=project_id)
    assert r.status_code == 400, r.text
    assert "not found or is not ACTIVE" in r.json()["detail"]


def test_upload_unknown_project_rejected(client: TestClient) -> None:
    r = _upload(client, _spec(invoice_number="CINV-NOPRJ"), po_id=None, project_id=999999)
    assert r.status_code == 400, r.text
    assert "not found or is not ACTIVE" in r.json()["detail"]


def test_set_project_editable_then_immutable_after_confirm(client: TestClient) -> None:
    project_id = client.app.state.project_id
    # PO-less invoice, initially unattributed.
    inv_id = _upload(client, _spec(invoice_number="CINV-SETPRJ"), po_id=None
                     ).json()["outcomes"][0]["invoice_id"]
    assert client.get(f"/api/v1/billing/invoices/{inv_id}").json()["project_id"] is None

    # Assign a project on the in-review invoice.
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/project",
                     json={"project_id": project_id})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == project_id

    # Clear it (None falls back to unattributed).
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/project", json={"project_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["project_id"] is None

    # Re-assign, then confirm.
    assert client.patch(f"/api/v1/billing/invoices/{inv_id}/project",
                        json={"project_id": project_id}).status_code == 200
    assert _confirm(client, inv_id).status_code == 200

    # Immutable after CONFIRMED -> 409, and project_id is unchanged.
    blocked = client.patch(f"/api/v1/billing/invoices/{inv_id}/project", json={"project_id": None})
    assert blocked.status_code == 409, blocked.text
    assert client.get(f"/api/v1/billing/invoices/{inv_id}").json()["project_id"] == project_id


def test_set_project_wrong_client_rejected(client: TestClient) -> None:
    _other_client_id, other_project_id = _second_client_project(client)
    inv_id = _upload(client, _spec(invoice_number="CINV-SETX"), po_id=None
                     ).json()["outcomes"][0]["invoice_id"]
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/project",
                     json={"project_id": other_project_id})
    assert r.status_code == 400, r.text
    assert "does not belong" in r.json()["detail"]


def test_set_project_rbac_viewer_forbidden(client: TestClient) -> None:
    project_id = client.app.state.project_id
    inv_id = _upload(client, _spec(invoice_number="CINV-SETRBAC"), po_id=None
                     ).json()["outcomes"][0]["invoice_id"]
    _as(client, VIEWER)
    r = client.patch(f"/api/v1/billing/invoices/{inv_id}/project",
                     json={"project_id": project_id})
    assert r.status_code == 403, r.text


def test_po_less_confirmed_appears_in_ar_and_zero_invoiced_qty(client: TestClient) -> None:
    widget_line_id = client.app.state.widget_line_id
    inv_id = _upload(client, _spec(invoice_number="CINV-NOPO-AR"), po_id=None
                     ).json()["outcomes"][0]["invoice_id"]
    assert _confirm(client, inv_id).status_code == 200

    db = client.app.state.TestSession()
    # AR register: a PO-less CONFIRMED invoice is a receivable (owed = its header total).
    rows = ar_service.ar_register(db, client_id=client.app.state.client_id)
    ar = next((r for r in rows if r.invoice_id == inv_id), None)
    assert ar is not None, "PO-less confirmed invoice must appear in the AR register"
    assert ar.grand_total_paise == 236000
    assert ar.outstanding_paise == 236000
    assert ar.status == ar_service.STATUS_UNPAID
    # §6 rollup: its lines carry po_line_item_id NULL, so they add 0 to any PO line's qty.
    assert service.invoiced_qty_for_po_line(db, widget_line_id) == Decimal("0")
    db.close()
