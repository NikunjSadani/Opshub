"""FastAPI entrypoint. Composes registered modules under /api/v1/<key>.

One Cloud Run service serves this API and (in prod) the built React SPA
same-origin. Modules self-register via ModuleSpec; adding a module is additive.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from app.config import get_settings
from app.modules.challan.routes import router as challan_router
from app.modules.files.routes import router as files_router
from app.modules.health.routes import SPEC as HEALTH
from app.modules.jobs.routes import router as jobs_router
from app.modules.masterdata.routes import router as masterdata_router
from app.modules.numbering.routes import router as numbering_router
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
    app.include_router(challan_router, prefix="/api/v1", tags=["challan"])
    app.include_router(jobs_router, prefix="/api/v1", tags=["jobs"])
    app.include_router(masterdata_router, prefix="/api/v1", tags=["masterdata"])
    app.include_router(numbering_router, prefix="/api/v1", tags=["numbering"])
    app.include_router(settings_router, prefix="/api/v1", tags=["settings"])
    _mount_spa(app, settings.static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: str) -> None:
    """Serve the built React SPA same-origin (added last, after the API routers).

    A real file is served if it exists; any other non-API path returns index.html
    so client-side routing works. Skipped when static_dir is unset (local dev).
    """
    root = Path(static_dir)
    index = root / "index.html"
    if not static_dir or not index.is_file():
        return

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
