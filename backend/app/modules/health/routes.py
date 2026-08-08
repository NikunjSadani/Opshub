"""Health module — public liveness + a registry echo. No auth, no DB dependency."""
from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.platform.module_registry import REGISTRY, ModuleSpec

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "app": s.app_name, "env": s.env}


@router.get("/modules")
def modules() -> list[dict[str, object]]:
    """The registered modules (drives the dashboard nav + module-access keys)."""
    return [
        {"key": m.key, "title": m.title, "nav_group": m.nav_group, "coming_soon": m.coming_soon}
        for m in REGISTRY
    ]


SPEC = ModuleSpec(key="health", title="Health", router=router, nav_group="_system")
