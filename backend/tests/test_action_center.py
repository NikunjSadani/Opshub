"""Action Center — the READ-ONLY computed "what needs attention" view.

In-memory sqlite (StaticPool, FKs ON like Postgres) with the action_center router mounted.
Every date is relative to ``date.today()`` (repo durable — never a hardcoded future date).

The seed builds a REAL cross-module graph, one row per membership/exclusion case:

  procurement (horizon 15):
    * PO-PAST   expected today−5  CONFIRMED  -> included, days_until −5 (most urgent)
    * PO-SOON   expected today+10 CONFIRMED  -> included, days_until 10
    * PO-FAR    expected today+40 CONFIRMED  -> EXCLUDED (beyond the 15-day horizon)
    * PO-CLOSED expected today+3  CLOSED     -> EXCLUDED (terminal status)
    (the invoicing POs carry a null expected date, so the two axes never cross-pollute)

  invoicing_due:
    * PO-BIG   line ordered 10, short_closed 1, unbilled -> open 9,  value 9×9000 = 81000
    * PO-SMALL line ordered 10,                unbilled -> open 10, value 10×5000 = 50000
    * PO-BILLED line ordered 4, fully invoiced (CONFIRMED inv qty 4) -> open 0, EXCLUDED

  ar_overdue:
    * INV-OD1 CONFIRMED due today−40, outstanding 100000 -> overdue 40d, bucket 31-60
    * INV-OD2 CONFIRMED due today−10, outstanding  50000 -> overdue 10d, bucket 0-30
    * INV-NOTDUE CONFIRMED due today+30                   -> EXCLUDED (not past due)
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module + table is registered on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.action_center import service
from app.modules.action_center.routes import router as action_center_router
from app.modules.billing.models import SalesInvoice, SalesInvoiceLine
from app.modules.files.models import StoredFile
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import POLineItem, Product, PurchaseOrder
from app.platform.auth import current_user
from app.platform.models import Level
from tests.rbac_util import make_role, make_user

TODAY = date.today()

AC_VIEWER = make_user("ac", role=make_role(module_levels={"action_center": Level.VIEW}))
# A user with a DIFFERENT module but NOT action_center -> 403 on the endpoint.
NON_AC = make_user("other", role=make_role(module_levels={"projects": Level.MANAGE}))


@pytest.fixture
def env() -> Iterator[tuple[TestClient, sessionmaker[Session], dict[str, int]]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:  # enforce FKs like Postgres
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    ids = _seed(TestSession)

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(action_center_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: AC_VIEWER
    yield TestClient(app), TestSession, ids
    engine.dispose()


def _as(client: TestClient, user: object) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _stored_file(db: Session) -> StoredFile:
    sf = StoredFile(filename="doc.pdf", size=1, storage_ref="ref", uploaded_by="seed")
    db.add(sf)
    db.flush()
    return sf


def _po(
    db: Session, *, number: str, client_id: int, project_id: int,
    expected: date | None, status: str = "CONFIRMED",
) -> PurchaseOrder:
    po = PurchaseOrder(
        po_number=number, client_id=client_id, project_id=project_id,
        po_date=TODAY, expected_procurement_date=expected, status=status)
    db.add(po)
    db.flush()
    return po


def _line(
    db: Session, *, po_id: int, product_id: int, ordered: str, sell_paise: int,
    short_closed: str = "0",
) -> POLineItem:
    line = POLineItem(
        po_id=po_id, product_id=product_id, description="Widget", uom="NOS",
        ordered_qty=Decimal(ordered), cost_price_paise=1000, sell_price_paise=sell_paise,
        short_closed_qty=Decimal(short_closed))
    db.add(line)
    db.flush()
    return line


def _confirmed_invoice(
    db: Session, *, client_id: int, po_id: int, number: str,
    due_date: date | None, grand_total_paise: int,
) -> SalesInvoice:
    inv = SalesInvoice(
        client_id=client_id, po_id=po_id, source_file_id=_stored_file(db).id,
        invoice_number=number, invoice_date=TODAY, due_date=due_date,
        grand_total_paise=grand_total_paise, total_taxable_paise=grand_total_paise,
        status="CONFIRMED")
    db.add(inv)
    db.flush()
    return inv


def _seed(TestSession: sessionmaker[Session]) -> dict[str, int]:
    db = TestSession()
    acm = ProjectClient(name="Acme Corp", code="ACM", active=True)
    db.add(acm)
    db.flush()
    proj = Project(client_id=acm.id, seq=1, code="ACM-001", name="Live", status="ACTIVE")
    db.add(proj)
    db.flush()
    widget = Product(code="WID-1", name="Widget", uom="NOS", active=True)
    db.add(widget)
    db.flush()

    # --- procurement axis (null expected date on the invoicing POs keeps the axes apart).
    po_past = _po(db, number="PO-PAST", client_id=acm.id, project_id=proj.id,
                  expected=TODAY - timedelta(days=5))
    po_soon = _po(db, number="PO-SOON", client_id=acm.id, project_id=proj.id,
                  expected=TODAY + timedelta(days=10))
    _po(db, number="PO-FAR", client_id=acm.id, project_id=proj.id,
        expected=TODAY + timedelta(days=40))                       # beyond horizon 15
    _po(db, number="PO-CLOSED", client_id=acm.id, project_id=proj.id,
        expected=TODAY + timedelta(days=3), status="CLOSED")       # terminal status

    # --- invoicing_due axis.
    po_big = _po(db, number="PO-BIG", client_id=acm.id, project_id=proj.id, expected=None)
    _line(db, po_id=po_big.id, product_id=widget.id, ordered="10", sell_paise=9000,
          short_closed="1")                                        # open 9 -> 81000
    po_small = _po(db, number="PO-SMALL", client_id=acm.id, project_id=proj.id, expected=None)
    _line(db, po_id=po_small.id, product_id=widget.id, ordered="10", sell_paise=5000)  # 50000

    po_billed = _po(db, number="PO-BILLED", client_id=acm.id, project_id=proj.id, expected=None)
    billed_line = _line(db, po_id=po_billed.id, product_id=widget.id, ordered="4",
                        sell_paise=7000)
    # A CONFIRMED invoice that fully bills the line (qty 4) -> open_qty 0 -> excluded.
    # due_date None so this confirmed invoice is never itself an overdue receivable.
    billed_inv = _confirmed_invoice(db, client_id=acm.id, po_id=po_billed.id,
                                    number="INV-BILLED", due_date=None, grand_total_paise=0)
    db.add(SalesInvoiceLine(
        invoice_id=billed_inv.id, po_line_item_id=billed_line.id, line_no=1,
        description="Widget", quantity=Decimal("4"), match_status="MATCHED"))

    # --- ar_overdue axis.
    od1 = _confirmed_invoice(db, client_id=acm.id, po_id=po_small.id, number="INV-OD1",
                             due_date=TODAY - timedelta(days=40), grand_total_paise=100000)
    od2 = _confirmed_invoice(db, client_id=acm.id, po_id=po_small.id, number="INV-OD2",
                             due_date=TODAY - timedelta(days=10), grand_total_paise=50000)
    _confirmed_invoice(db, client_id=acm.id, po_id=po_small.id, number="INV-NOTDUE",
                       due_date=TODAY + timedelta(days=30), grand_total_paise=70000)

    db.commit()
    ids = {
        "acm": acm.id, "proj": proj.id,
        "po_past": po_past.id, "po_soon": po_soon.id,
        "po_big": po_big.id, "po_small": po_small.id, "po_billed": po_billed.id,
        "od1": od1.id, "od2": od2.id,
    }
    db.close()
    return ids


# ------------------------------------------------------------- service: procurement

def test_procurement_membership_and_days_until(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.procurement_followups(db, horizon_days=15)
    db.close()
    by_po = {r.po_id: r for r in rows}
    # PO-FAR (40d, beyond horizon) and PO-CLOSED (terminal) are excluded.
    assert set(by_po) == {ids["po_past"], ids["po_soon"]}
    assert by_po[ids["po_past"]].days_until == -5   # already past -> negative
    assert by_po[ids["po_soon"]].days_until == 10
    # Identity resolved by join.
    assert by_po[ids["po_soon"]].client_name == "Acme Corp"
    assert by_po[ids["po_soon"]].project_code == "ACM-001"


def test_procurement_ordered_most_urgent_first(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.procurement_followups(db, horizon_days=15)
    db.close()
    # days_until ascending -> the overdue PO-PAST leads.
    assert [r.po_id for r in rows] == [ids["po_past"], ids["po_soon"]]


# ------------------------------------------------------------- service: invoicing_due

def test_invoicing_due_membership_and_math(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.invoicing_due(db)
    db.close()
    by_po = {r.po_id: r for r in rows}
    # PO-BILLED is fully invoiced -> excluded; the two open POs remain.
    assert set(by_po) == {ids["po_big"], ids["po_small"]}
    # PO-BIG: open = 10 − 0 invoiced − 1 short_closed = 9; value 9 × 9000.
    assert by_po[ids["po_big"]].uninvoiced_qty == Decimal("9")
    assert by_po[ids["po_big"]].uninvoiced_value_paise == 81000
    # PO-SMALL: open 10; value 10 × 5000.
    assert by_po[ids["po_small"]].uninvoiced_qty == Decimal("10")
    assert by_po[ids["po_small"]].uninvoiced_value_paise == 50000


def test_invoicing_due_ordered_by_value_desc(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.invoicing_due(db)
    db.close()
    assert [r.po_id for r in rows] == [ids["po_big"], ids["po_small"]]  # 81000 > 50000


# --------------------------------------------------------------- service: ar_overdue

def test_ar_overdue_membership_and_days(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.ar_overdue(db, TODAY)
    db.close()
    by_inv = {r.invoice_id: r for r in rows}
    # INV-NOTDUE (future due) and INV-BILLED (no due date) are excluded.
    assert set(by_inv) == {ids["od1"], ids["od2"]}
    assert by_inv[ids["od1"]].days_overdue == 40
    assert by_inv[ids["od1"]].aging_bucket == "31-60"
    assert by_inv[ids["od1"]].outstanding_paise == 100000
    assert by_inv[ids["od2"]].days_overdue == 10
    assert by_inv[ids["od2"]].aging_bucket == "0-30"
    # Ordered most days-overdue first.
    assert [r.invoice_id for r in rows] == [ids["od1"], ids["od2"]]


# --------------------------------------------------------------------- HTTP surface

def test_endpoint_shape_and_counts(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    client, _TestSession, ids = env
    r = client.get("/api/v1/action-center?horizon_days=15")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"procurement": 2, "invoicing_due": 2, "ar_overdue": 2}
    # uninvoiced_qty is serialized as a decimal string (no float/precision loss).
    assert body["invoicing_due"][0]["po_id"] == ids["po_big"]
    assert body["invoicing_due"][0]["uninvoiced_qty"] == "9.000"
    assert body["ar_overdue"][0]["invoice_id"] == ids["od1"]


def test_endpoint_horizon_default_and_bounds(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    client, _TestSession, _ids = env
    # Default horizon 15 -> PO-FAR (40d) still excluded.
    default = client.get("/api/v1/action-center").json()
    assert default["counts"]["procurement"] == 2
    # A wider horizon pulls in the 40-day PO.
    wide = client.get("/api/v1/action-center?horizon_days=90").json()
    assert wide["counts"]["procurement"] == 3
    # Out-of-range horizon is a 422 (1..90).
    assert client.get("/api/v1/action-center?horizon_days=0").status_code == 422
    assert client.get("/api/v1/action-center?horizon_days=91").status_code == 422


def test_rbac_non_action_center_user_forbidden(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    client, _TestSession, _ids = env
    _as(client, NON_AC)
    assert client.get("/api/v1/action-center").status_code == 403
    # And the viewer is allowed (proving the 403 is the gate, not a broken route).
    _as(client, AC_VIEWER)
    assert client.get("/api/v1/action-center").status_code == 200
