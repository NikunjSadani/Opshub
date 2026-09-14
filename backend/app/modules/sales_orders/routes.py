"""Sales Orders API aggregator (mounted at `/api/v1`).

The module is split by concern so each area owns its own file (no collisions):
* `products_routes` / `products_service`   — Product Master
* `po_routes` / `po_service`               — Purchase Orders + line items + amendments
* `quote_routes` / `quote_service`         — quote / price-book search

RBAC module key ``sales_orders``: ``po.create`` / ``po.amend`` = OPERATE,
``po.short_close`` / ``po.void`` / ``product.manage`` = MANAGE, ``quote.search`` = VIEW.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.modules.sales_orders import (
    po_routes,
    products_routes,
    project_products_routes,
    quote_routes,
)

MODULE_KEY = "sales_orders"

router = APIRouter()
router.include_router(products_routes.router)
router.include_router(project_products_routes.router)
router.include_router(po_routes.router)
router.include_router(quote_routes.router)
