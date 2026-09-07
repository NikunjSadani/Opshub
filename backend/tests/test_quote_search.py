"""Quote / price-book search — the READ-ONLY analytics view over accumulated PO lines.

Exercises the real app wiring (sales_orders router + RBAC + dev-auth override), seeding
a client, project, two products, and POs with lines across two dates plus a CANCELLED PO
that must never surface. Covers keyword/category/budget/date filters, recency ordering,
CANCELLED exclusion, margin math (incl. sell=0 -> null), the trend endpoint, and RBAC.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module registers on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import POLineItem, Product, PurchaseOrder
from app.modules.sales_orders.routes import router as sales_orders_router
from app.platform.auth import current_user
from app.platform.models import Role, User
from app.platform.roles_builtin import ensure_builtin_roles


def _seed_price_book(db: Session) -> None:
    """A client/project, two products, and three POs (one CANCELLED) with priced lines."""
    client = ProjectClient(name="Acme Corp", code="ACM")
    db.add(client)
    db.flush()
    project = Project(client_id=client.id, seq=1, code="ACM-001", name="Rollout")
    db.add(project)
    db.flush()

    widget = Product(name="Widget", brand="BrandX", model_number="WX-100",
                     category="Hardware", uom="PCS")
    gadget = Product(name="Gadget", brand="BrandY", model_number="GY-200",
                     category="Electronics", uom="PCS")
    db.add_all([widget, gadget])
    db.flush()

    po_old = PurchaseOrder(po_number="PO-OLD", client_id=client.id, project_id=project.id,
                           po_date=date(2026, 1, 10), status="CONFIRMED")
    po_new = PurchaseOrder(po_number="PO-NEW", client_id=client.id, project_id=project.id,
                           po_date=date(2026, 6, 15), status="CONFIRMED")
    po_canc = PurchaseOrder(po_number="PO-CANC", client_id=client.id, project_id=project.id,
                            po_date=date(2026, 7, 1), status="CANCELLED")
    db.add_all([po_old, po_new, po_canc])
    db.flush()

    # Each line carries an ADMIN-ONLY `sell_price_paise` (actual) DISTINCT from the
    # visible `client_sell_price_paise` (client-quoted), so the masking + the client-price
    # budget filter are both provable: a leak would show the actual, not the client price.
    db.add_all([
        # Oldest PO: one Widget line, actual margin (10000-8000)/10000 = 20%; client 11000.
        POLineItem(po_id=po_old.id, product_id=widget.id, description="Widget unit",
                   uom="PCS", ordered_qty=Decimal("10"), cost_price_paise=8000,
                   sell_price_paise=10000, client_sell_price_paise=11000,
                   tax_rate=Decimal("18")),
        # Newest PO: a pricier Widget line, actual margin (12000-9000)/12000 = 25%; client 13000.
        # Its ACTUAL freight (800) DIVERGES from the visible client freight (500) so the
        # freight masking is provable — a leak would show the actual freight, not the client.
        POLineItem(po_id=po_new.id, product_id=widget.id, description="Widget premium",
                   uom="PCS", ordered_qty=Decimal("5"), cost_price_paise=9000,
                   sell_price_paise=12000, client_sell_price_paise=13000,
                   freight_paise=800, client_freight_paise=500,
                   tax_rate=Decimal("18")),
        # Newest PO: a giveaway Gadget line (actual sell 0) -> margin must be null; client 0.
        POLineItem(po_id=po_new.id, product_id=gadget.id, description="Gadget sample",
                   uom="PCS", ordered_qty=Decimal("2"), cost_price_paise=5000,
                   sell_price_paise=0, client_sell_price_paise=0, tax_rate=Decimal("18")),
        # CANCELLED PO: an absurd line that must never appear in any result.
        POLineItem(po_id=po_canc.id, product_id=widget.id, description="Widget cancelled",
                   uom="PCS", ordered_qty=Decimal("100"), cost_price_paise=1,
                   sell_price_paise=999999, client_sell_price_paise=999999,
                   tax_rate=Decimal("18")),
    ])
    db.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:  # enforce FKs like Postgres
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    seed.add(User(firebase_uid="admin", email="admin@t.local", name="Admin",
                  role_id=roles["Administrator"].id, active=True))
    # Viewer holds sales_orders VIEW (== quote.search); Finance has NO sales_orders grant.
    seed.add(User(firebase_uid="viewer", email="viewer@t.local", name="Viewer",
                  role_id=roles["Viewer"].id, active=True))
    seed.add(User(firebase_uid="finance", email="finance@t.local", name="Finance",
                  role_id=roles["Finance"].id, active=True))
    seed.commit()
    _seed_price_book(seed)
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(sales_orders_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _db
    app.state.TestSession = TestSession
    yield TestClient(app)
    engine.dispose()


def _as(client: TestClient, uid: str) -> None:
    db = client.app.state.TestSession()
    user = db.execute(
        select(User)
        .options(
            selectinload(User.role).selectinload(Role.module_permissions),
            selectinload(User.role).selectinload(Role.platform_permissions),
        )
        .where(User.firebase_uid == uid)
    ).scalar_one()
    db.close()
    client.app.dependency_overrides[current_user] = lambda: user


def test_keyword_matches_brand_and_model(client: TestClient) -> None:
    _as(client, "viewer")
    # Brand token hits both Widget lines (product BrandX), not the Gadget line.
    r = client.get("/api/v1/quote-search", params={"q": "BrandX"})
    assert r.status_code == 200, r.text
    assert {row["product_name"] for row in r.json()} == {"Widget"}
    assert len(r.json()) == 2
    # Model-number token hits only the Gadget line.
    r2 = client.get("/api/v1/quote-search", params={"q": "GY-200"})
    assert [row["product_name"] for row in r2.json()] == ["Gadget"]


def test_multi_token_keyword_is_anded(client: TestClient) -> None:
    _as(client, "viewer")
    # "premium widget" must match both tokens against name/brand/model/description.
    r = client.get("/api/v1/quote-search", params={"q": "premium widget"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    # Viewer sees the client-quoted price (the actual sell is masked to null).
    assert r.json()[0]["client_sell_price_paise"] == 13000


def test_category_filter(client: TestClient) -> None:
    _as(client, "viewer")
    r = client.get("/api/v1/quote-search", params={"category": "electronics"})
    assert r.status_code == 200
    assert [row["product_name"] for row in r.json()] == ["Gadget"]
    r2 = client.get("/api/v1/quote-search", params={"category": "Hardware"})
    assert len(r2.json()) == 2


def test_budget_band_filters_on_client_price(client: TestClient) -> None:
    _as(client, "viewer")
    # The budget band now bounds the CLIENT-quoted price, not the admin-only actual.
    # client prices are {11000, 13000, 0}; >= 12000 keeps only the 13000 line.
    r = client.get("/api/v1/quote-search", params={"budget_min_paise": 12000})
    assert r.status_code == 200
    assert [row["client_sell_price_paise"] for row in r.json()] == [13000]
    # Proof it is NOT filtering on the actual sell: that line's actual is 12000, so a
    # band of >= 12500 (above the actual, below the client price) must still keep it.
    r2 = client.get("/api/v1/quote-search", params={"budget_min_paise": 12500})
    assert [row["client_sell_price_paise"] for row in r2.json()] == [13000]


def test_date_range_filter(client: TestClient) -> None:
    _as(client, "viewer")
    r = client.get("/api/v1/quote-search", params={"date_from": "2026-06-01"})
    assert r.status_code == 200
    # Only the newest PO's two lines fall in range.
    assert {row["po_number"] for row in r.json()} == {"PO-NEW"}
    assert len(r.json()) == 2


def test_recency_ordering_and_cancelled_excluded(client: TestClient) -> None:
    _as(client, "viewer")
    r = client.get("/api/v1/quote-search")
    assert r.status_code == 200
    rows = r.json()
    # Three live lines only; the CANCELLED PO's line never appears.
    assert len(rows) == 3
    assert "PO-CANC" not in {row["po_number"] for row in rows}
    assert 999999 not in {row["sell_price_paise"] for row in rows}
    # Newest PO first (po_date DESC), oldest last.
    assert rows[0]["po_date"] == "2026-06-15"
    assert rows[-1]["po_date"] == "2026-01-10"


def test_margin_pct_math_including_sell_zero(client: TestClient) -> None:
    # Margin is over the ADMIN-ONLY actual sell, so it is only visible to an IAM admin.
    _as(client, "admin")
    rows = client.get("/api/v1/quote-search").json()
    margins = {row["sell_price_paise"]: row["margin_pct"] for row in rows}
    assert margins[10000] == 20.0
    assert margins[12000] == 25.0
    assert margins[0] is None  # sell == 0 -> never divide by zero


def test_trend_ordered_oldest_first(client: TestClient) -> None:
    _as(client, "viewer")
    # Resolve the Widget product id via a search, then pull its trend.
    hit = client.get("/api/v1/quote-search", params={"q": "WX-100"}).json()[0]
    product_id = hit["product_id"]
    r = client.get("/api/v1/quote-search/trend", params={"product_id": product_id})
    assert r.status_code == 200
    points = r.json()
    # Two live POs for the Widget (CANCELLED excluded), oldest-first.
    assert [p["po_number"] for p in points] == ["PO-OLD", "PO-NEW"]
    assert [p["cost_price_paise"] for p in points] == [8000, 9000]


def test_ordered_qty_serialized_as_decimal_string(client: TestClient) -> None:
    _as(client, "viewer")
    rows = client.get("/api/v1/quote-search", params={"budget_min_paise": 11000}).json()
    row = rows[0]
    assert Decimal(row["ordered_qty"]) == Decimal("5")
    assert Decimal(row["tax_rate"]) == Decimal("18")


def test_admin_sees_actual_sell_and_margin(client: TestClient) -> None:
    _as(client, "admin")
    rows = client.get("/api/v1/quote-search", params={"q": "premium widget"}).json()
    assert len(rows) == 1
    row = rows[0]
    # An IAM admin sees the actual sell, its true margin, the actual freight, AND the
    # client-quoted price + client freight.
    assert row["sell_price_paise"] == 12000
    assert row["margin_pct"] == 25.0
    assert row["client_sell_price_paise"] == 13000
    assert row["freight_paise"] == 800          # ACTUAL freight — admin sees it
    assert row["client_freight_paise"] == 500   # client freight — visible


def test_non_admin_masks_actual_sell_margin_and_freight(client: TestClient) -> None:
    _as(client, "viewer")
    rows = client.get("/api/v1/quote-search").json()
    assert rows, "viewer should still get the (client-priced) rows"
    for row in rows:
        # The actual sell, true margin AND actual freight are masked; the client-quoted
        # price + client freight are returned. (Freight leak was the auth-audit HIGH.)
        assert row["sell_price_paise"] is None
        assert row["margin_pct"] is None
        assert row["freight_paise"] is None
        assert isinstance(row["client_sell_price_paise"], int)
        assert isinstance(row["client_freight_paise"], int)
    # And the divergent actual freight (800) never appears anywhere in a viewer's payload.
    premium = next(r for r in rows if r["client_sell_price_paise"] == 13000)
    assert premium["client_freight_paise"] == 500
    assert 800 not in {row["freight_paise"] for row in rows}


def test_trend_masks_actual_sell_for_non_admin(client: TestClient) -> None:
    _as(client, "admin")
    product_id = client.get(
        "/api/v1/quote-search", params={"q": "WX-100"}
    ).json()[0]["product_id"]

    admin_pts = client.get(
        "/api/v1/quote-search/trend", params={"product_id": product_id}
    ).json()
    assert [p["sell_price_paise"] for p in admin_pts] == [10000, 12000]
    assert [p["client_sell_price_paise"] for p in admin_pts] == [11000, 13000]

    _as(client, "viewer")
    view_pts = client.get(
        "/api/v1/quote-search/trend", params={"product_id": product_id}
    ).json()
    # Non-admin: actual sell masked, client price still visible.
    assert all(p["sell_price_paise"] is None for p in view_pts)
    assert [p["client_sell_price_paise"] for p in view_pts] == [11000, 13000]


def test_rbac_denies_user_without_sales_orders(client: TestClient) -> None:
    _as(client, "finance")  # Finance role has expense only, no sales_orders grant
    r = client.get("/api/v1/quote-search")
    assert r.status_code == 403, r.text
    r2 = client.get("/api/v1/quote-search/trend", params={"product_id": 1})
    assert r2.status_code == 403
