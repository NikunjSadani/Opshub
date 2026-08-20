"""FastAPI entrypoint. Composes registered modules under /api/v1/<key>.

One Cloud Run service serves this API and (in prod) the built React SPA
same-origin. Modules self-register via ModuleSpec; adding a module is additive.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, status
from fastapi.responses import FileResponse

from app.config import get_settings
from app.modules.billing.routes import router as billing_router
from app.modules.challan.routes import router as challan_router
from app.modules.expense.routes import router as expense_router
from app.modules.files.routes import router as files_router
from app.modules.finance.routes import router as finance_router
from app.modules.health.routes import SPEC as HEALTH
from app.modules.jobs.routes import router as jobs_router
from app.modules.masterdata.routes import router as masterdata_router
from app.modules.me.routes import router as me_router
from app.modules.numbering.routes import router as numbering_router
from app.modules.projects.routes import router as projects_router
from app.modules.roles.routes import router as roles_router
from app.modules.sales_orders.routes import router as sales_orders_router
from app.modules.settings.routes import router as settings_router
from app.modules.users.routes import router as users_router
from app.platform.module_registry import REGISTRY, ModuleSpec, register_module

# --- user-facing modules (drive nav + per-user module access) ---
register_module(HEALTH)
# Delivery Challan — nav entry only; its API routes are mounted separately below
# (challan_router at /api/v1/challan). The empty router keeps the registry's
# mount step a no-op for this module.
register_module(ModuleSpec(
    key="document_automation",
    title="Delivery Challan",
    router=APIRouter(),
    nav_group="Operations",
))
# Projects — nav entry only; its API routes are mounted separately below
# (projects_router at /api/v1). The empty router keeps the registry's mount step
# a no-op for this module (same pattern as Delivery Challan above).
register_module(ModuleSpec(
    key="projects",
    title="Projects",
    router=APIRouter(),
    nav_group="Operations",
))
# Expense & Invoice — nav entry only; its API routes are mounted separately below
# (expense_router at /api/v1). Same empty-router pattern as the modules above.
register_module(ModuleSpec(
    key="expense_invoice",
    title="Expense & Invoice",
    router=APIRouter(),
    nav_group="Operations",
))
# Sales Orders (Product Master, Purchase Orders, quote search) — nav entry only; its
# API routes mount separately below (sales_orders_router at /api/v1). Same pattern.
register_module(ModuleSpec(
    key="sales_orders",
    title="Purchase Orders",
    router=APIRouter(),
    nav_group="Operations",
))
# Billing / AR (client invoice capture, payments, advances) — nav entry only; its API
# mounts separately below (billing_router at /api/v1). Same empty-router pattern.
register_module(ModuleSpec(
    key="billing",
    title="Billing & AR",
    router=APIRouter(),
    nav_group="Operations",
))
# Finance (project + consolidated P&L, read-only) — nav entry only.
register_module(ModuleSpec(
    key="finance",
    title="Finance & P&L",
    router=APIRouter(),
    nav_group="Operations",
))


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
    app.include_router(expense_router, prefix="/api/v1", tags=["expense"])
    app.include_router(jobs_router, prefix="/api/v1", tags=["jobs"])
    app.include_router(masterdata_router, prefix="/api/v1", tags=["masterdata"])
    app.include_router(numbering_router, prefix="/api/v1", tags=["numbering"])
    app.include_router(projects_router, prefix="/api/v1", tags=["projects"])
    app.include_router(sales_orders_router, prefix="/api/v1", tags=["sales_orders"])
    app.include_router(billing_router, prefix="/api/v1", tags=["billing"])
    app.include_router(finance_router, prefix="/api/v1", tags=["finance"])
    app.include_router(settings_router, prefix="/api/v1", tags=["settings"])
    # User Management + Roles — platform primitives (admin-managed accounts and the
    # roles that grant access); NOT nav ModuleSpecs, mounted like files/settings above.
    app.include_router(users_router, prefix="/api/v1", tags=["users"])
    app.include_router(roles_router, prefix="/api/v1", tags=["roles"])
    # The current user's identity + effective permissions (any authenticated user).
    app.include_router(me_router, prefix="/api/v1", tags=["me"])
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
