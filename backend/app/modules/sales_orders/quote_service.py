"""Quote / price-book search — a READ-ONLY analytics view over accumulated PO lines.

The payoff for the line-item foundation: every priced `POLineItem` ever recorded
becomes searchable price history joined to its product, PO, client, and project.
This module never writes; it only reads across the spine.

Cross-module rule (mirrors ``expense.service.allocation_maps``): the sales_orders
module keeps NO ORM relationship to `Project`/`ProjectClient`, so the client name and
project code are resolved by an EXPLICIT join on the plain FK ids — not a relationship.

Money is per-UNIT integer paise. `margin_pct` is computed with `Decimal` and is `None`
when `sell_price_paise == 0` (never divide by zero). Lines on a CANCELLED PO are excluded.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import POLineItem, POStatus, Product, PurchaseOrder


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed `%`/`_` matches literally (paired with
    `escape="\\"` on `.ilike()`). The backslash is escaped first so it stays the escape
    char; without this a literal `%` in a token would match every row."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def margin_pct(cost_price_paise: int, sell_price_paise: int) -> float | None:
    """Gross margin on a line as a percentage of sell price, rounded to 2dp via `Decimal`.

    Returns `None` when `sell_price_paise == 0` (a giveaway/sample line) so a caller
    never divides by zero. May be negative when cost exceeds sell (a loss line)."""
    if sell_price_paise == 0:
        return None
    pct = (Decimal(sell_price_paise - cost_price_paise) / Decimal(sell_price_paise)) * 100
    return float(round(pct, 2))


def search(
    db: Session,
    *,
    q: str | None = None,
    category: str | None = None,
    client_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    budget_min_paise: int | None = None,
    budget_max_paise: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Row[Any]]:
    """Search the price book. Each row is one `POLineItem` joined to its `PurchaseOrder`,
    `ProjectClient`, `Project`, and `Product`, ordered most-recent-first (`po_date DESC`,
    then `id DESC` as a stable tiebreak).

    Filters (all optional, AND-combined):
      * ``q`` — whitespace-split into tokens; EVERY token must match (case-insensitive,
        LIKE-escaped) at least one of the product name/brand/model_number OR the line
        description. So "acme pump" narrows to lines matching both words.
      * ``category`` — exact (case-insensitive) product category.
      * ``client_id`` — the PO's client.
      * ``date_from`` / ``date_to`` — inclusive bounds on the PO date.
      * ``budget_min_paise`` / ``budget_max_paise`` — inclusive bounds on per-unit
        ``sell_price_paise``.

    Lines on a CANCELLED PO are always excluded.
    """
    stmt = (
        select(
            POLineItem.id.label("po_line_item_id"),
            POLineItem.product_id.label("product_id"),
            Product.name.label("product_name"),
            Product.brand.label("brand"),
            Product.model_number.label("model_number"),
            Product.category.label("category"),
            POLineItem.uom.label("uom"),
            PurchaseOrder.po_number.label("po_number"),
            PurchaseOrder.client_id.label("client_id"),
            ProjectClient.name.label("client_name"),
            Project.code.label("project_code"),
            PurchaseOrder.po_date.label("po_date"),
            POLineItem.ordered_qty.label("ordered_qty"),
            POLineItem.cost_price_paise.label("cost_price_paise"),
            POLineItem.sell_price_paise.label("sell_price_paise"),
            POLineItem.freight_paise.label("freight_paise"),
            POLineItem.packaging_paise.label("packaging_paise"),
            POLineItem.handling_paise.label("handling_paise"),
            POLineItem.other_paise.label("other_paise"),
            POLineItem.tax_rate.label("tax_rate"),
        )
        .join(PurchaseOrder, POLineItem.po_id == PurchaseOrder.id)
        .join(Product, POLineItem.product_id == Product.id)
        .join(ProjectClient, PurchaseOrder.client_id == ProjectClient.id)
        .join(Project, PurchaseOrder.project_id == Project.id)
        .where(PurchaseOrder.status != POStatus.CANCELLED.value)
        .order_by(PurchaseOrder.po_date.desc(), POLineItem.id.desc())
    )

    if q:
        # AND the tokens: each successive `.where` intersects, so every token must hit
        # some product-identity or description field.
        for token in q.split():
            like = f"%{_escape_like(token)}%"
            stmt = stmt.where(
                Product.name.ilike(like, escape="\\")
                | Product.brand.ilike(like, escape="\\")
                | Product.model_number.ilike(like, escape="\\")
                | POLineItem.description.ilike(like, escape="\\")
            )
    if category:
        stmt = stmt.where(func.lower(Product.category) == category.strip().lower())
    if client_id is not None:
        stmt = stmt.where(PurchaseOrder.client_id == client_id)
    if date_from is not None:
        stmt = stmt.where(PurchaseOrder.po_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(PurchaseOrder.po_date <= date_to)
    if budget_min_paise is not None:
        stmt = stmt.where(POLineItem.sell_price_paise >= budget_min_paise)
    if budget_max_paise is not None:
        stmt = stmt.where(POLineItem.sell_price_paise <= budget_max_paise)

    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).all())


def price_trend(db: Session, product_id: int, limit: int = 20) -> list[Row[Any]]:
    """One product's price history across POs, oldest-first (`po_date ASC`, then `id ASC`)
    so a caller can plot the cost/sell inflation trend over time. CANCELLED POs excluded."""
    stmt = (
        select(
            PurchaseOrder.po_date.label("po_date"),
            PurchaseOrder.po_number.label("po_number"),
            ProjectClient.name.label("client_name"),
            POLineItem.ordered_qty.label("ordered_qty"),
            POLineItem.cost_price_paise.label("cost_price_paise"),
            POLineItem.sell_price_paise.label("sell_price_paise"),
        )
        .join(PurchaseOrder, POLineItem.po_id == PurchaseOrder.id)
        .join(ProjectClient, PurchaseOrder.client_id == ProjectClient.id)
        .where(POLineItem.product_id == product_id)
        .where(PurchaseOrder.status != POStatus.CANCELLED.value)
        .order_by(PurchaseOrder.po_date.asc(), POLineItem.id.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).all())
