"""Finance P&L — money-critical service math + the read-only HTTP surface.

In-memory sqlite (StaticPool, FKs ON like Postgres) with the finance router mounted.
The seed builds a REAL cross-module money graph:

  * client ACM, project ACM-001, a PO under it;
  * CONFIRMED billing invoices (revenue) + one CONFIRMED client credit note
    (reduces revenue) + one NON-confirmed invoice (must be excluded);
  * CONFIRMED expense invoices doc_type INVOICE (cost) + a vendor CREDIT_NOTE
    (reduces cost, sign-aware) + one NON-confirmed expense (must be excluded);
  * the GEN / GEN-001 general-bucket project with an overhead expense.

Money (all net-of-GST taxable, paise):
  ACM-001 revenue = 100000 + 50000 − 20000(CN)          = 130000
  ACM-001 cost    = 40000 + 10000 − 15000(vendor CN)    =  35000
  ACM-001 margin  = 95000 ;  margin% = 95000/130000*100 =  73.08
  GEN-001 cost    = 25000 ; revenue 0 -> margin% None
  consolidated totals: revenue 130000 · cost 60000 · margin 70000
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module + table is registered on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.billing.models import CreditNote, SalesInvoice
from app.modules.expense.models import Invoice as ExpenseInvoice
from app.modules.expense.models import InvoiceBatch
from app.modules.files.models import StoredFile
from app.modules.finance import service
from app.modules.finance.routes import router as finance_router
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import PurchaseOrder
from app.platform.auth import current_user
from app.platform.models import Level
from tests.rbac_util import make_role, make_user

FINANCE_VIEWER = make_user("fin", role=make_role(module_levels={"finance": Level.VIEW}))
# A user with access to a DIFFERENT module but NOT finance -> 403 on every finance route.
NON_FINANCE = make_user("other", role=make_role(module_levels={"projects": Level.MANAGE}))


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
    app.include_router(finance_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda: FINANCE_VIEWER
    yield TestClient(app), TestSession, ids
    engine.dispose()


def _as(client: TestClient, user: object) -> None:
    client.app.dependency_overrides[current_user] = lambda: user


def _stored_file(db: Session) -> StoredFile:
    sf = StoredFile(filename="doc.pdf", size=1, storage_ref="ref", uploaded_by="seed")
    db.add(sf)
    db.flush()
    return sf


def _seed(TestSession: sessionmaker[Session]) -> dict[str, int]:
    db = TestSession()

    # Clients + projects. Client name carries a leading "=" to exercise the CSV guard.
    acm = ProjectClient(name="=Acme Corp", code="ACM", active=True)
    gen = ProjectClient(name="General", code="GEN", active=True)
    db.add_all([acm, gen])
    db.flush()
    p1 = Project(client_id=acm.id, seq=1, code="ACM-001", name="Live", status="ACTIVE")
    gen_p = Project(client_id=gen.id, seq=1, code="GEN-001", name="General / Overhead",
                    status="ACTIVE")
    db.add_all([p1, gen_p])
    db.flush()

    # A PO under the client project (the join billing_invoice.po_id -> project).
    po = PurchaseOrder(po_number="PO-1", client_id=acm.id, project_id=p1.id,
                       po_date=date(2026, 1, 1), status="CONFIRMED")
    db.add(po)
    db.flush()

    # --- Revenue: two CONFIRMED billing invoices under the PO, one CONFIRMED credit
    #     note (reduces revenue), and one NON-confirmed invoice that must be excluded.
    inv1 = SalesInvoice(client_id=acm.id, po_id=po.id, source_file_id=_stored_file(db).id,
                        invoice_number="INV-1", invoice_date=date(2026, 2, 1),
                        total_taxable_paise=100000, status="CONFIRMED")
    inv2 = SalesInvoice(client_id=acm.id, po_id=po.id, source_file_id=_stored_file(db).id,
                        invoice_number="INV-2", invoice_date=date(2026, 2, 10),
                        total_taxable_paise=50000, status="CONFIRMED")
    inv_draft = SalesInvoice(client_id=acm.id, po_id=po.id, source_file_id=_stored_file(db).id,
                             invoice_number="INV-DRAFT", invoice_date=date(2026, 2, 15),
                             total_taxable_paise=999999, status="UPLOADED")
    db.add_all([inv1, inv2, inv_draft])
    db.flush()
    cn = CreditNote(invoice_id=inv1.id, client_id=acm.id, source_file_id=_stored_file(db).id,
                    cn_number="CN-1", cn_date=date(2026, 2, 20),
                    total_taxable_paise=20000, status="CONFIRMED")
    cn_draft = CreditNote(invoice_id=inv2.id, client_id=acm.id,
                          source_file_id=_stored_file(db).id, cn_number="CN-DRAFT",
                          cn_date=date(2026, 2, 21), total_taxable_paise=77777, status="UPLOADED")
    db.add_all([cn, cn_draft])

    # --- Cost: CONFIRMED expense INVOICEs (cost) + a vendor CREDIT_NOTE (reduces cost)
    #     on ACM-001, plus one NON-confirmed expense that must be excluded; and the GEN
    #     overhead expense.
    batch = InvoiceBatch(status="COMPLETED")
    db.add(batch)
    db.flush()
    exp1 = ExpenseInvoice(batch_id=batch.id, project_id=p1.id, doc_type="INVOICE",
                          invoice_date=date(2026, 2, 5), total_taxable_paise=40000,
                          status="CONFIRMED")
    exp2 = ExpenseInvoice(batch_id=batch.id, project_id=p1.id, doc_type="INVOICE",
                          invoice_date=date(2026, 2, 8), total_taxable_paise=10000,
                          status="CONFIRMED")
    exp_cn = ExpenseInvoice(batch_id=batch.id, project_id=p1.id, doc_type="CREDIT_NOTE",
                            invoice_date=date(2026, 2, 12), total_taxable_paise=15000,
                            status="CONFIRMED")
    exp_draft = ExpenseInvoice(batch_id=batch.id, project_id=p1.id, doc_type="INVOICE",
                               invoice_date=date(2026, 2, 14), total_taxable_paise=88888,
                               status="EXTRACTED")
    exp_gen = ExpenseInvoice(batch_id=batch.id, project_id=gen_p.id, doc_type="INVOICE",
                             invoice_date=date(2026, 2, 18), total_taxable_paise=25000,
                             status="CONFIRMED")
    db.add_all([exp1, exp2, exp_cn, exp_draft, exp_gen])
    db.commit()
    ids = {"acm": acm.id, "gen": gen.id, "p1": p1.id, "gen_p": gen_p.id}
    db.close()
    return ids


# ---------------------------------------------------------------- service: project P&L

def test_project_revenue_net_of_credit_note(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    line = service.project_pnl(db, ids["p1"])
    db.close()
    # 100000 + 50000 − 20000(confirmed CN); the 999999 draft invoice is excluded.
    assert line.revenue_paise == 130000


def test_project_cost_is_sign_aware(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    line = service.project_pnl(db, ids["p1"])
    db.close()
    # 40000 + 10000 − 15000(vendor CREDIT_NOTE); the 88888 non-confirmed row is excluded.
    assert line.cost_paise == 35000


def test_project_margin_and_pct(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    line = service.project_pnl(db, ids["p1"])
    db.close()
    assert line.margin_paise == 95000
    assert line.margin_pct == 73.08  # 95000/130000*100 rounded to 2dp


def test_zero_revenue_margin_pct_is_none(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    line = service.project_pnl(db, ids["gen_p"])
    db.close()
    assert line.revenue_paise == 0
    assert line.cost_paise == 25000
    assert line.margin_paise == -25000
    assert line.margin_pct is None  # revenue 0 -> undefined


# ------------------------------------------------------------- service: by-project list

def test_pnl_by_project_excludes_general_bucket(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    rows = service.pnl_by_project(db)
    db.close()
    codes = {r.project_code for r in rows}
    assert "ACM-001" in codes
    assert "GEN-001" not in codes  # the general bucket never appears as a client row
    (row,) = [r for r in rows if r.project_code == "ACM-001"]
    assert (row.revenue_paise, row.cost_paise, row.margin_paise) == (130000, 35000, 95000)
    assert row.client_code == "ACM"


def test_pnl_by_project_client_filter(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    mine = service.pnl_by_project(db, service.PnlFilters(client_id=ids["acm"]))
    other = service.pnl_by_project(db, service.PnlFilters(client_id=999999))
    db.close()
    assert {r.project_code for r in mine} == {"ACM-001"}
    assert other == []


# --------------------------------------------------------------- service: consolidated

def test_consolidated_totals_and_general_bucket(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    _client, TestSession, ids = env
    db = TestSession()
    data = service.consolidated_pnl(db)
    db.close()

    # Per-project rows exclude GEN; the general bucket carries GEN's overhead only.
    assert {r.project_code for r in data.projects} == {"ACM-001"}
    assert data.general_bucket.revenue_paise == 0
    assert data.general_bucket.cost_paise == 25000
    assert data.general_bucket.margin_paise == -25000

    # Totals over EVERY project: revenue 130000, cost 35000+25000, margin 70000.
    assert data.totals.revenue_paise == 130000
    assert data.totals.cost_paise == 60000
    assert data.totals.margin_paise == 70000

    # Reconciliation: consolidated == Σ(project rows) + general bucket.
    row_rev = sum(r.revenue_paise for r in data.projects)
    row_cost = sum(r.cost_paise for r in data.projects)
    assert data.totals.revenue_paise == row_rev + data.general_bucket.revenue_paise
    assert data.totals.cost_paise == row_cost + data.general_bucket.cost_paise


# --------------------------------------------------------------------- HTTP surface

def test_routes_happy_path_and_csv_shape(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    client, _TestSession, ids = env

    r = client.get("/api/v1/finance/pnl/projects")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1 and body[0]["project_code"] == "ACM-001"
    assert body[0]["margin_paise"] == 95000

    r = client.get("/api/v1/finance/pnl/consolidated")
    assert r.status_code == 200, r.text
    con = r.json()
    assert con["totals"]["cost_paise"] == 60000
    assert con["general_bucket"]["cost_paise"] == 25000

    r = client.get(f"/api/v1/finance/pnl/projects/{ids['p1']}")
    assert r.status_code == 200, r.text
    assert r.json()["pnl"]["revenue_paise"] == 130000

    # Unknown project -> 404 (path shadowing check: .csv/consolidated are not swallowed).
    assert client.get("/api/v1/finance/pnl/projects/424242").status_code == 404

    # CSV export: header + exactly one data row, rupee money, and the leading-"=" client
    # name neutralized with a "'" prefix (CSV-injection guard).
    r = client.get("/api/v1/finance/pnl/projects.csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("Project Code,Project Name")
    assert len(lines) == 2
    assert "ACM-001" in lines[1]
    assert "1300.00" in lines[1]  # revenue 130000 paise -> rupees
    assert '"\'=Acme Corp"' in lines[1]  # injection guard prefixed the leading "="


def test_rbac_non_finance_user_forbidden(
    env: tuple[TestClient, sessionmaker[Session], dict[str, int]]
) -> None:
    client, _TestSession, ids = env
    _as(client, NON_FINANCE)
    for path in (
        "/api/v1/finance/pnl/projects",
        "/api/v1/finance/pnl/projects.csv",
        "/api/v1/finance/pnl/consolidated",
        f"/api/v1/finance/pnl/projects/{ids['p1']}",
    ):
        assert client.get(path).status_code == 403, path

    # And the finance viewer is allowed (proving the 403s are the gate, not a broken route).
    _as(client, FINANCE_VIEWER)
    assert client.get("/api/v1/finance/pnl/projects").status_code == 200


# ------------------------------------------------- F2 / F3: isolated money-edge cases

def _fresh_session() -> sessionmaker[Session]:
    """A private in-memory DB (FKs ON) so an edge-case graph never perturbs the tuned seed."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _client_project_po(db: Session) -> tuple[int, int]:
    """Seed one client + project + PO; return (project_id, po_id)."""
    cli = ProjectClient(name="Edge Co", code="EDG", active=True)
    db.add(cli)
    db.flush()
    proj = Project(client_id=cli.id, seq=1, code="EDG-001", name="Edge", status="ACTIVE")
    db.add(proj)
    db.flush()
    po = PurchaseOrder(po_number="PO-E", client_id=cli.id, project_id=proj.id,
                       po_date=date(2026, 1, 1), status="CONFIRMED")
    db.add(po)
    db.flush()
    return proj.id, po.id


def test_cn_reduction_ignored_when_credited_invoice_not_confirmed() -> None:
    # F2: a CONFIRMED credit note against a NON-confirmed invoice must NOT reduce revenue
    # (symmetric with the revenue side, which only counts CONFIRMED invoices).
    TestSession = _fresh_session()
    db = TestSession()
    project_id, po_id = _client_project_po(db)
    client_id = db.execute(select(PurchaseOrder.client_id).where(
        PurchaseOrder.id == po_id)).scalar_one()

    inv_conf = SalesInvoice(client_id=client_id, po_id=po_id,
                            source_file_id=_stored_file(db).id, invoice_number="INV-C",
                            invoice_date=date(2026, 2, 1), total_taxable_paise=100000,
                            status="CONFIRMED")
    inv_draft = SalesInvoice(client_id=client_id, po_id=po_id,
                             source_file_id=_stored_file(db).id, invoice_number="INV-D",
                             invoice_date=date(2026, 2, 2), total_taxable_paise=50000,
                             status="UPLOADED")
    db.add_all([inv_conf, inv_draft])
    db.flush()
    # CONFIRMED CN, but it credits the UNCONFIRMED invoice -> excluded by the F2 filter.
    db.add(CreditNote(invoice_id=inv_draft.id, client_id=client_id,
                      source_file_id=_stored_file(db).id, cn_number="CN-D",
                      cn_date=date(2026, 2, 3), total_taxable_paise=30000, status="CONFIRMED"))
    db.commit()

    line = service.project_pnl(db, project_id)
    db.close()
    # Only the confirmed invoice counts; the CN against the draft is ignored.
    assert line.revenue_paise == 100000


def test_negative_revenue_margin_pct_is_none() -> None:
    # F3: an over-credited project has NEGATIVE net revenue; margin_pct must be None (not a
    # misleading positive % from margin/revenue with two negatives).
    TestSession = _fresh_session()
    db = TestSession()
    project_id, po_id = _client_project_po(db)
    client_id = db.execute(select(PurchaseOrder.client_id).where(
        PurchaseOrder.id == po_id)).scalar_one()

    inv = SalesInvoice(client_id=client_id, po_id=po_id, source_file_id=_stored_file(db).id,
                       invoice_number="INV-N", invoice_date=date(2026, 2, 1),
                       total_taxable_paise=10000, status="CONFIRMED")
    db.add(inv)
    db.flush()
    # A confirmed CN larger than the invoice -> net revenue goes negative.
    db.add(CreditNote(invoice_id=inv.id, client_id=client_id,
                      source_file_id=_stored_file(db).id, cn_number="CN-N",
                      cn_date=date(2026, 2, 2), total_taxable_paise=30000, status="CONFIRMED"))
    db.commit()

    line = service.project_pnl(db, project_id)
    db.close()
    assert line.revenue_paise == -20000  # 10000 − 30000
    assert line.margin_pct is None
