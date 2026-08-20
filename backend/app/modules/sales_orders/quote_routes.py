"""Quote / price-book search API (mounted under `/api/v1` via the sales_orders router).

  GET /quote-search        -> search accumulated PO lines (price book)   [VIEW]
  GET /quote-search/trend  -> one product's price history over time      [VIEW]

Both are READ-ONLY VIEW capabilities: gated by `sales_orders` module access (>= View),
matching the `quote.search` action in the RBAC catalog. All logic lives in
`quote_service`; this file only shapes the query params and serializes rows.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.sales_orders import quote_service
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()


class QuoteRow(BaseModel):
    """One priced line in the price book, joined to its product/PO/client/project.

    Money is per-UNIT integer paise; freight/packaging/handling/other are per-LINE paise.
    `margin_pct` is the gross margin over sell (Decimal-rounded 2dp) or null when
    sell is 0. `ordered_qty` / `tax_rate` are serialized as strings to preserve the
    exact Decimal (no float rounding)."""

    po_line_item_id: int
    product_id: int
    product_name: str
    brand: str | None
    model_number: str | None
    category: str | None
    uom: str
    po_number: str
    client_id: int
    client_name: str
    project_code: str
    po_date: date
    ordered_qty: str
    cost_price_paise: int
    sell_price_paise: int
    margin_pct: float | None
    freight_paise: int
    packaging_paise: int
    handling_paise: int
    other_paise: int
    tax_rate: str


class TrendPoint(BaseModel):
    """One point on a product's price-history trend (oldest-first from the service)."""

    po_date: date
    po_number: str
    client_name: str
    ordered_qty: str
    cost_price_paise: int
    sell_price_paise: int


def _quote_row(r: Any) -> QuoteRow:
    return QuoteRow(
        po_line_item_id=r.po_line_item_id,
        product_id=r.product_id,
        product_name=r.product_name,
        brand=r.brand,
        model_number=r.model_number,
        category=r.category,
        uom=r.uom,
        po_number=r.po_number,
        client_id=r.client_id,
        client_name=r.client_name,
        project_code=r.project_code,
        po_date=r.po_date,
        ordered_qty=str(r.ordered_qty),
        cost_price_paise=r.cost_price_paise,
        sell_price_paise=r.sell_price_paise,
        margin_pct=quote_service.margin_pct(r.cost_price_paise, r.sell_price_paise),
        freight_paise=r.freight_paise,
        packaging_paise=r.packaging_paise,
        handling_paise=r.handling_paise,
        other_paise=r.other_paise,
        tax_rate=str(r.tax_rate),
    )


@router.get("/quote-search", response_model=list[QuoteRow])
def quote_search(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    category: Annotated[str | None, Query(max_length=120)] = None,
    client_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    budget_min_paise: Annotated[int | None, Query(ge=0)] = None,
    budget_max_paise: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[QuoteRow]:
    rbac.require_module(user, rbac.SALES_ORDERS)  # VIEW — quote.search capability
    rows = quote_service.search(
        db,
        q=q,
        category=category,
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        budget_min_paise=budget_min_paise,
        budget_max_paise=budget_max_paise,
        limit=limit,
        offset=offset,
    )
    return [_quote_row(r) for r in rows]


@router.get("/quote-search/trend", response_model=list[TrendPoint])
def quote_trend(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    product_id: Annotated[int, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TrendPoint]:
    rbac.require_module(user, rbac.SALES_ORDERS)  # VIEW — quote.search capability
    rows = quote_service.price_trend(db, product_id, limit)
    return [
        TrendPoint(
            po_date=r.po_date,
            po_number=r.po_number,
            client_name=r.client_name,
            ordered_qty=str(r.ordered_qty),
            cost_price_paise=r.cost_price_paise,
            sell_price_paise=r.sell_price_paise,
        )
        for r in rows
    ]
