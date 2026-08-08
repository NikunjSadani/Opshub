"""FastAPI entrypoint. Composes registered modules under /api/v1/<key>.

One Cloud Run service serves this API and (in prod) the built React SPA
same-origin. Modules self-register via ModuleSpec; adding a module is additive.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.modules.files.routes import router as files_router
from app.modules.health.routes import SPEC as HEALTH
from app.modules.jobs.routes import router as jobs_router
from app.modules.settings.routes import router as settings_router
from app.platform.module_registry import REGISTRY, register_module

# --- user-facing modules (drive nav + per-user module access) ---
register_module(HEALTH)
# Phase 1 will add: Delivery Challan (document_automation)
# Phase 2 will add: Expense & Invoice (expense_invoice)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    # module routers (from the registry)
    for spec in REGISTRY:
        prefix = "" if spec.key == "health" else f"/{spec.key}"
        app.include_router(spec.router, prefix=f"/api/v1{prefix}", tags=[spec.key])
    # platform primitive routers (infrastructure, not nav modules)
    app.include_router(files_router, prefix="/api/v1/files", tags=["files"])
    app.include_router(jobs_router, prefix="/api/v1", tags=["jobs"])
    app.include_router(settings_router, prefix="/api/v1", tags=["settings"])
    return app


app = create_app()
