"""FastAPI entrypoint. Composes registered modules under /api/v1/<key>.

One Cloud Run service serves this API and (in prod) the built React SPA
same-origin. Modules self-register via ModuleSpec; adding a module is additive.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.modules.health.routes import SPEC as HEALTH
from app.platform.module_registry import REGISTRY, register_module

# --- register modules (order = nav order) ---
register_module(HEALTH)
# Phase 1 will add: Delivery Challan (document_automation)
# Phase 2 will add: Expense & Invoice (expense_invoice)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    for spec in REGISTRY:
        prefix = "" if spec.key == "health" else f"/{spec.key}"
        app.include_router(spec.router, prefix=f"/api/v1{prefix}", tags=[spec.key])
    return app


app = create_app()
