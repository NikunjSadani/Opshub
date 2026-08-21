"""Vendor credit notes (inc 28): doc_type capture + SIGN-AWARE money aggregation.

A CREDIT_NOTE is a REDUCTION of cost, so every money aggregate — the /expense/summary
dashboard (total + by-project + by-payment) and the CSV money columns — must SUBTRACT it
(net = Σ INVOICE − Σ CREDIT_NOTE). This suite proves the net arithmetic, that the groups
still reconcile to the net total, that a pure-invoice dataset is byte-identical to before,
and the upload-time doc_type + against_invoice_id validation.

Reuses the routes-test harness (fake extractor, in-memory sqlite, seeded project/method);
the imported ``client`` fixture + helpers keep the fake-PDF spec plumbing in one place.
"""
from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from tests.expense.test_expense_routes import (
    _new_project,
    _pdf,
    _spec,
    _upload,
)


def _upload_doc(
    client: TestClient,
    *specs: dict[str, Any],
    doc_type: str = "INVOICE",
    against_invoice_id: int | None = None,
    project_id: int | None = None,
    payment_method_id: int | None = None,
) -> Any:
    """Upload N fake PDFs stamping the inc-28 doc_type / against_invoice_id form fields."""
    files = [("files", (f"doc{i}.pdf", _pdf(s), "application/pdf"))
             for i, s in enumerate(specs)]
    data: dict[str, str] = {
        "project_id": str(project_id if project_id is not None
                          else client.app.state.project_id),
        "payment_method_id": str(payment_method_id if payment_method_id is not None
                                 else client.app.state.payment_method_id),
        "doc_type": doc_type,
    }
    if against_invoice_id is not None:
        data["against_invoice_id"] = str(against_invoice_id)
    return client.post("/api/v1/expense/invoices", files=files, data=data)


def _confirm(client: TestClient, spec: dict[str, Any], **over: Any) -> int:
    """Upload one doc (INVOICE unless doc_type overridden) and confirm it; return its id."""
    inv_id = _upload_doc(client, spec, **over).json()["outcomes"][0]["invoice_id"]
    r = client.patch(f"/api/v1/expense/invoices/{inv_id}/reviews", json={"confirm": True})
    assert r.status_code == 200, r.text
    return int(inv_id)


# ---------------------------------------------------- capture + doc_type plumbing

def test_upload_credit_note_persists_doc_type(client: TestClient) -> None:
    r = _upload_doc(client, _spec(invoice_number="CN-1"), doc_type="CREDIT_NOTE")
    assert r.status_code == 201, r.text
    inv_id = r.json()["outcomes"][0]["invoice_id"]
    detail = client.get(f"/api/v1/expense/invoices/{inv_id}").json()
    assert detail["doc_type"] == "CREDIT_NOTE"
    assert detail["against_invoice_id"] is None
    # default upload (no doc_type) is still an INVOICE — byte-identical to pre-inc-28
    inv2 = _upload(client, _spec(invoice_number="INV-D")).json()["outcomes"][0]["invoice_id"]
    assert client.get(f"/api/v1/expense/invoices/{inv2}").json()["doc_type"] == "INVOICE"


def test_invalid_doc_type_rejected_400(client: TestClient) -> None:
    r = _upload_doc(client, _spec(), doc_type="BOGUS")
    assert r.status_code == 400, r.text
    assert "doc_type" in r.json()["detail"]
    assert client.get("/api/v1/expense/invoices").json() == []  # nothing persisted


# ---------------------------------------------------- sign-aware summary (core)

def test_summary_credit_note_reduces_project_and_payment_totals(client: TestClient) -> None:
    """A project with one Rs 1000 invoice + one Rs 300 credit note nets Rs 700 — the credit
    note SUBTRACTS from both the by-project and by-payment spend, and the total."""
    _confirm(client, _spec(invoice_number="INV-1000", grand_total_paise=100000))
    _confirm(client, _spec(invoice_number="CN-300", grand_total_paise=30000),
             doc_type="CREDIT_NOTE")

    s = client.get("/api/v1/expense/summary").json()
    # net = 100000 - 30000 = 70000 (Rs 700); a credit note is NOT additional spend
    assert s["total_confirmed_paise"] == 70000
    assert s["invoice_count"] == 2  # both are confirmed documents

    proj = {row["project_code"]: row for row in s["by_project"]}
    assert proj["TST-001"]["total_paise"] == 70000
    assert proj["TST-001"]["count"] == 2
    method = {row["name"]: row for row in s["by_payment_method"]}
    assert method["Bank Transfer"]["total_paise"] == 70000

    # groups reconcile to the net total
    assert sum(r["total_paise"] for r in s["by_project"]) == s["total_confirmed_paise"]
    assert sum(r["total_paise"] for r in s["by_payment_method"]) == s["total_confirmed_paise"]


def test_summary_pure_invoice_dataset_unchanged_regression(client: TestClient) -> None:
    """With NO credit notes, every sum is the plain positive total (byte-identical to the
    pre-inc-28 behaviour) — the sign-aware CASE must not alter an invoices-only dataset."""
    _confirm(client, _spec(invoice_number="INV-A", grand_total_paise=118000))
    _confirm(client, _spec(invoice_number="INV-B", grand_total_paise=200000))

    s = client.get("/api/v1/expense/summary").json()
    assert s["total_confirmed_paise"] == 118000 + 200000  # plain sum, no subtraction
    assert s["invoice_count"] == 2
    proj = {row["project_code"]: row for row in s["by_project"]}
    assert proj["TST-001"]["total_paise"] == 318000


def test_summary_credit_note_can_net_negative(client: TestClient) -> None:
    """A credit note larger than the confirmed invoices drives the net NEGATIVE (an
    over-credit) rather than clamping — the money math is honestly signed."""
    _confirm(client, _spec(invoice_number="INV-S", grand_total_paise=50000))
    _confirm(client, _spec(invoice_number="CN-BIG", grand_total_paise=80000),
             doc_type="CREDIT_NOTE")
    s = client.get("/api/v1/expense/summary").json()
    assert s["total_confirmed_paise"] == 50000 - 80000  # -30000


# ---------------------------------------------------- against_invoice_id validation

def test_against_invoice_id_validation(client: TestClient) -> None:
    # non-existent reference -> 400
    r = _upload_doc(client, _spec(invoice_number="CN-X"),
                    doc_type="CREDIT_NOTE", against_invoice_id=99999)
    assert r.status_code == 400, r.text
    assert "does not exist" in r.json()["detail"]

    # a real invoice on the DEFAULT project
    orig_id = _upload(client, _spec(invoice_number="INV-ORIG")
                      ).json()["outcomes"][0]["invoice_id"]

    # a credit note against it, same project -> 201, link stamped
    ok = _upload_doc(client, _spec(invoice_number="CN-OK"),
                     doc_type="CREDIT_NOTE", against_invoice_id=orig_id)
    assert ok.status_code == 201, ok.text
    cn_id = ok.json()["outcomes"][0]["invoice_id"]
    assert client.get(f"/api/v1/expense/invoices/{cn_id}"
                      ).json()["against_invoice_id"] == orig_id

    # a reference on a DIFFERENT project -> 400 (would misallocate the reduction)
    other_pid = _new_project(client, "OTH", "Other Project")
    other_inv = _upload(client, _spec(invoice_number="INV-OTHER"),
                        project_id=other_pid).json()["outcomes"][0]["invoice_id"]
    bad = _upload_doc(client, _spec(invoice_number="CN-BAD"),
                      doc_type="CREDIT_NOTE", against_invoice_id=other_inv)
    assert bad.status_code == 400, bad.text
    assert "different project" in bad.json()["detail"]


# ---------------------------------------------------- register + CSV doc_type

def test_register_shows_and_filters_doc_type(client: TestClient) -> None:
    _upload(client, _spec(invoice_number="R-INV"))                      # INVOICE
    _upload_doc(client, _spec(invoice_number="R-CN", grand_total_paise=30000),
                doc_type="CREDIT_NOTE")

    rows = client.get("/api/v1/expense/invoices").json()
    by_num = {row["invoice_number"]: row for row in rows}
    assert by_num["R-INV"]["doc_type"] == "INVOICE"
    assert by_num["R-CN"]["doc_type"] == "CREDIT_NOTE"

    # filter to CREDIT_NOTE only
    only_cn = client.get("/api/v1/expense/invoices",
                         params={"doc_type": "CREDIT_NOTE"}).json()
    assert [r["invoice_number"] for r in only_cn] == ["R-CN"]
    # a bogus filter value -> 400 (never a silently-empty register)
    assert client.get("/api/v1/expense/invoices",
                      params={"doc_type": "NOPE"}).status_code == 400

    # CSV carries the Doc Type column and renders the credit note NEGATIVE so a
    # spreadsheet summing the money column yields the net
    csv = client.get("/api/v1/expense/invoices.csv").text
    assert "Doc Type" in csv.splitlines()[0]
    cn_line = next(ln for ln in csv.splitlines() if "R-CN" in ln)
    assert "CREDIT_NOTE" in cn_line
    assert "-300.00" in cn_line
