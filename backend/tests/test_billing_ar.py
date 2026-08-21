"""Accounts-Receivable lane over HTTP + direct service checks.

Mirrors the expense/challan harness: in-memory sqlite (StaticPool, FKs ON via
PRAGMA), Base.metadata.create_all, overridden get_db + current_user. Seeds a real
client (via the projects service) + a StoredFile + billing_invoice rows directly,
then exercises payments, advances, FIFO suggestion/application, the AR tracker,
aging buckets, credit-note offsets, and the RBAC gates.

All dates are computed relative to `date.today()` (never a hardcoded future date).
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

# Import every model module so Base.metadata knows all tables the billing FKs
# reference (project_client, purchase_order/po_line_item, files_stored_file).
import app.modules.files.models  # noqa: F401
import app.modules.projects.models  # noqa: F401
import app.modules.sales_orders.models  # noqa: F401
from app.db import Base, get_db
from app.modules.billing import ar_service
from app.modules.billing.ar_routes import router
from app.modules.billing.models import CreditNote, SalesInvoice, SalesInvoiceStatus
from app.modules.files.models import StoredFile
from app.modules.projects import service as projects_service
from app.platform.auth import current_user
from app.platform.models import Level, User
from tests.rbac_util import make_role, make_user

OPERATOR = make_user("op", role=make_role(module_levels={"billing": Level.OPERATE}))
VIEWER = make_user("view", role=make_role(module_levels={"billing": Level.VIEW}))
OUTSIDER = make_user("out", role=make_role())


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )

    @event.listens_for(engine, "connect")
    def _fk_pragma(dbapi_conn: Any, _record: Any) -> None:  # noqa: ANN401
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    seed = TestSession()
    c1 = projects_service.create_client(seed, name="Acme", code="ACM", actor_uid="adm")
    c2 = projects_service.create_client(seed, name="Beta", code="BET", actor_uid="adm")
    seed.flush()
    f = StoredFile(
        kind="billing-source", filename="inv.pdf", content_type="application/pdf",
        size=1, storage_ref="ref", uploaded_by="adm", module_key="billing",
    )
    seed.add(f)
    seed.commit()
    client_id, other_client_id, file_id = c1.id, c2.id, f.id
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
    app.state.other_client_id = other_client_id
    app.state.file_id = file_id
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, user: User) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _seed_invoice(
    client: TestClient, *, number: str, grand_total: int,
    due_offset_days: int | None = None, client_id: int | None = None,
) -> int:
    """Create a CONFIRMED-style billing_invoice directly; return its id.
    `due_offset_days` is relative to today (negative = already past due)."""
    db = client.app.state.TestSession()
    due = None if due_offset_days is None else date.today() + timedelta(days=due_offset_days)
    inv = SalesInvoice(
        client_id=client_id if client_id is not None else client.app.state.client_id,
        source_file_id=client.app.state.file_id,
        invoice_number=number,
        invoice_date=date.today() - timedelta(days=5),
        due_date=due,
        grand_total_paise=grand_total,
        status=SalesInvoiceStatus.CONFIRMED.value,
    )
    db.add(inv)
    db.commit()
    inv_id = inv.id
    db.close()
    return inv_id


def _seed_credit_note(client: TestClient, *, invoice_id: int, number: str, amount: int) -> None:
    db = client.app.state.TestSession()
    cn = CreditNote(
        invoice_id=invoice_id,
        client_id=client.app.state.client_id,
        source_file_id=client.app.state.file_id,
        cn_number=number,
        cn_date=date.today(),
        grand_total_paise=amount,
        status="CONFIRMED",
    )
    db.add(cn)
    db.commit()
    db.close()


# --------------------------------------------------------------------- payments

def test_partial_payment_makes_part_paid(client: TestClient) -> None:
    inv = _seed_invoice(client, number="INV-1", grand_total=100000, due_offset_days=30)
    r = client.post("/api/v1/billing/payments",
                    json={"invoice_id": inv, "amount_paise": 40000})
    assert r.status_code == 201, r.text
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["paid_paise"] == 40000
    assert ar["outstanding_paise"] == 60000
    assert ar["status"] == "PART_PAID"
    assert ar["overdue"] is False
    # the detail carries the payment we just recorded
    assert len(ar["payments"]) == 1 and ar["payments"][0]["amount_paise"] == 40000


def test_full_payment_makes_paid(client: TestClient) -> None:
    inv = _seed_invoice(client, number="INV-2", grand_total=100000, due_offset_days=10)
    assert client.post("/api/v1/billing/payments",
                       json={"invoice_id": inv, "amount_paise": 100000}).status_code == 201
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["outstanding_paise"] == 0
    assert ar["status"] == "PAID"
    assert ar["aging_bucket"] is None  # settled -> no aging


def test_payment_over_outstanding_422(client: TestClient) -> None:
    inv = _seed_invoice(client, number="INV-3", grand_total=50000, due_offset_days=10)
    r = client.post("/api/v1/billing/payments",
                    json={"invoice_id": inv, "amount_paise": 60000})
    assert r.status_code == 422, r.text


def test_overdue_detection(client: TestClient) -> None:
    inv = _seed_invoice(client, number="INV-4", grand_total=100000, due_offset_days=-40)
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["status"] == "UNPAID"
    assert ar["overdue"] is True
    assert ar["aging_bucket"] == "31-60"
    # the tracker's overdue filter includes it
    hits = client.get("/api/v1/billing/ar", params={"overdue": "true"}).json()
    assert inv in [row["invoice_id"] for row in hits]


# --------------------------------------------------------------------- advances

def test_record_advance_then_remaining(client: TestClient) -> None:
    cid = client.app.state.client_id
    r = client.post("/api/v1/billing/advances",
                    json={"client_id": cid, "amount_paise": 300000})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["amount_paise"] == 300000
    assert body["remaining_paise"] == 300000 and body["applied_paise"] == 0
    listed = client.get("/api/v1/billing/advances", params={"client_id": cid}).json()
    assert len(listed) == 1 and listed[0]["remaining_paise"] == 300000


def test_fifo_suggestion_oldest_first(client: TestClient) -> None:
    cid = client.app.state.client_id
    a1 = client.post("/api/v1/billing/advances",
                     json={"client_id": cid, "amount_paise": 300000,
                           "received_on": str(date.today() - timedelta(days=20))}).json()
    a2 = client.post("/api/v1/billing/advances",
                     json={"client_id": cid, "amount_paise": 500000,
                           "received_on": str(date.today() - timedelta(days=5))}).json()
    inv = _seed_invoice(client, number="INV-5", grand_total=600000, due_offset_days=15)
    sugg = client.get(f"/api/v1/billing/invoices/{inv}/advance-suggestion").json()
    # oldest advance (a1) drained first for its full 300000, then a2 for the remaining 300000
    assert [s["advance_id"] for s in sugg] == [a1["advance_id"], a2["advance_id"]]
    assert [s["amount_paise"] for s in sugg] == [300000, 300000]


def test_apply_advance_reduces_outstanding(client: TestClient) -> None:
    cid = client.app.state.client_id
    adv = client.post("/api/v1/billing/advances",
                      json={"client_id": cid, "amount_paise": 300000}).json()
    inv = _seed_invoice(client, number="INV-6", grand_total=500000, due_offset_days=15)
    r = client.post("/api/v1/billing/advance-applications",
                    json={"advance_id": adv["advance_id"], "invoice_id": inv,
                          "amount_paise": 300000})
    assert r.status_code == 201, r.text
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["applied_paise"] == 300000
    assert ar["outstanding_paise"] == 200000
    assert ar["status"] == "PART_PAID"
    # the advance is now fully drained
    remaining = client.get("/api/v1/billing/advances",
                           params={"client_id": cid}).json()[0]["remaining_paise"]
    assert remaining == 0


def test_apply_over_remaining_422(client: TestClient) -> None:
    cid = client.app.state.client_id
    adv = client.post("/api/v1/billing/advances",
                      json={"client_id": cid, "amount_paise": 100000}).json()
    inv = _seed_invoice(client, number="INV-7", grand_total=500000, due_offset_days=15)
    r = client.post("/api/v1/billing/advance-applications",
                    json={"advance_id": adv["advance_id"], "invoice_id": inv,
                          "amount_paise": 150000})  # > advance remaining
    assert r.status_code == 422, r.text


def test_apply_over_outstanding_422(client: TestClient) -> None:
    cid = client.app.state.client_id
    adv = client.post("/api/v1/billing/advances",
                      json={"client_id": cid, "amount_paise": 500000}).json()
    inv = _seed_invoice(client, number="INV-8", grand_total=100000, due_offset_days=15)
    r = client.post("/api/v1/billing/advance-applications",
                    json={"advance_id": adv["advance_id"], "invoice_id": inv,
                          "amount_paise": 200000})  # > invoice outstanding
    assert r.status_code == 422, r.text


def test_apply_cross_client_422(client: TestClient) -> None:
    """An advance may not be applied to a different client's invoice."""
    adv = client.post("/api/v1/billing/advances",
                      json={"client_id": client.app.state.client_id,
                            "amount_paise": 100000}).json()
    other_inv = _seed_invoice(client, number="INV-X", grand_total=100000,
                              due_offset_days=15, client_id=client.app.state.other_client_id)
    r = client.post("/api/v1/billing/advance-applications",
                    json={"advance_id": adv["advance_id"], "invoice_id": other_inv,
                          "amount_paise": 50000})
    assert r.status_code == 422, r.text


def test_unapply_advance_reopens_outstanding(client: TestClient) -> None:
    cid = client.app.state.client_id
    adv = client.post("/api/v1/billing/advances",
                      json={"client_id": cid, "amount_paise": 200000}).json()
    inv = _seed_invoice(client, number="INV-9", grand_total=200000, due_offset_days=15)
    app_id = client.post("/api/v1/billing/advance-applications",
                         json={"advance_id": adv["advance_id"], "invoice_id": inv,
                               "amount_paise": 200000}).json()["id"]
    assert client.get(f"/api/v1/billing/invoices/{inv}/ar").json()["status"] == "PAID"
    # reverse it: invoice re-opens, advance remaining restored
    assert client.delete(f"/api/v1/billing/advance-applications/{app_id}").status_code == 200
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["outstanding_paise"] == 200000 and ar["status"] == "UNPAID"
    assert client.get("/api/v1/billing/advances",
                      params={"client_id": cid}).json()[0]["remaining_paise"] == 200000


# ---------------------------------------------------------------- credit notes

def test_credit_note_reduces_outstanding(client: TestClient) -> None:
    inv = _seed_invoice(client, number="INV-10", grand_total=100000, due_offset_days=15)
    _seed_credit_note(client, invoice_id=inv, number="CN-1", amount=30000)
    ar = client.get(f"/api/v1/billing/invoices/{inv}/ar").json()
    assert ar["credited_paise"] == 30000
    assert ar["outstanding_paise"] == 70000
    assert ar["status"] == "PART_PAID"


# ---------------------------------------------------------------- aging buckets

def test_aging_buckets(client: TestClient) -> None:
    cases = {
        "AGE-A": (10, "0-30"),     # not yet due -> 0-30
        "AGE-B": (-15, "0-30"),    # 15 days past due
        "AGE-C": (-45, "31-60"),
        "AGE-D": (-75, "61-90"),
        "AGE-E": (-120, "90+"),
    }
    expected: dict[int, str] = {}
    for num, (offset, bucket) in cases.items():
        inv = _seed_invoice(client, number=num, grand_total=100000, due_offset_days=offset)
        expected[inv] = bucket
    rows = client.get("/api/v1/billing/ar").json()
    by_id = {row["invoice_id"]: row for row in rows}
    for inv_id, bucket in expected.items():
        assert by_id[inv_id]["aging_bucket"] == bucket, f"{inv_id} -> {bucket}"


# ------------------------------------------------------------------- AR tracker

def test_ar_tracker_filters(client: TestClient) -> None:
    paid = _seed_invoice(client, number="T-PAID", grand_total=100000, due_offset_days=10)
    client.post("/api/v1/billing/payments",
                json={"invoice_id": paid, "amount_paise": 100000})
    _seed_invoice(client, number="T-UNPAID", grand_total=100000, due_offset_days=10)
    # status filter
    paid_rows = client.get("/api/v1/billing/ar", params={"status": "PAID"}).json()
    assert [r["invoice_number"] for r in paid_rows] == ["T-PAID"]
    unpaid_rows = client.get("/api/v1/billing/ar", params={"status": "UNPAID"}).json()
    assert "T-UNPAID" in [r["invoice_number"] for r in unpaid_rows]
    # client filter isolates the other client's invoices
    other = _seed_invoice(client, number="T-OTHER", grand_total=100000,
                          due_offset_days=10, client_id=client.app.state.other_client_id)
    mine = client.get("/api/v1/billing/ar",
                      params={"client_id": client.app.state.client_id}).json()
    assert other not in [r["invoice_id"] for r in mine]


# -------------------------------------------------------------------- RBAC gates

def test_viewer_can_read_but_not_record(client: TestClient) -> None:
    inv = _seed_invoice(client, number="RB-1", grand_total=100000, due_offset_days=10)
    _as(client, VIEWER)
    # VIEW may read the tracker + detail
    assert client.get("/api/v1/billing/ar").status_code == 200
    assert client.get(f"/api/v1/billing/invoices/{inv}/ar").status_code == 200
    # but may NOT record a payment / advance / application (needs OPERATE)
    assert client.post("/api/v1/billing/payments",
                       json={"invoice_id": inv, "amount_paise": 1000}).status_code == 403
    assert client.post("/api/v1/billing/advances",
                       json={"client_id": client.app.state.client_id,
                             "amount_paise": 1000}).status_code == 403


def test_outsider_fully_blocked(client: TestClient) -> None:
    inv = _seed_invoice(client, number="RB-2", grand_total=100000, due_offset_days=10)
    _as(client, OUTSIDER)
    assert client.get("/api/v1/billing/ar").status_code == 403
    assert client.get(f"/api/v1/billing/invoices/{inv}/ar").status_code == 403
    assert client.post("/api/v1/billing/payments",
                       json={"invoice_id": inv, "amount_paise": 1000}).status_code == 403


# ---------------------------------------------------- direct service edge checks

def test_service_missing_invoice_and_advance(client: TestClient) -> None:
    db = client.app.state.TestSession()
    with pytest.raises(ar_service.InvoiceNotFound):
        ar_service.record_payment(db, invoice_id=99999, amount_paise=100)
    with pytest.raises(ar_service.InvalidAmount):
        ar_service.record_advance(db, client_id=client.app.state.client_id, amount_paise=0)
    with pytest.raises(ar_service.AdvanceNotFound):
        ar_service.apply_advance(db, advance_id=99999, invoice_id=1, amount_paise=100)
    db.close()
