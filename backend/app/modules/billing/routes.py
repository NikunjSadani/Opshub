"""Billing API aggregator (mounted at `/api/v1`). Split by concern so parallel
build lanes own disjoint files:
* `invoice_routes`    — client-invoice upload → extract → match → review → confirm
* `creditnote_routes` — client credit-note upload → extract → tally
* `ar_routes`         — payments, advances, advance application, AR tracker

RBAC module key ``billing``. See `docs/plans/PROJECT-SPINE-DESIGN.md` §5e.
"""
from __future__ import annotations

from fastapi import APIRouter

from app.modules.billing import ar_routes, creditnote_routes, invoice_routes

MODULE_KEY = "billing"

router = APIRouter()
router.include_router(invoice_routes.router)
router.include_router(creditnote_routes.router)
router.include_router(ar_routes.router)
