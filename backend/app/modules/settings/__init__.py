"""Settings module — admin-gated platform key/value settings CRUD.

Exports `router: APIRouter`; mounted by the app at `/api/v1` (so `/api/v1/settings...`).
"""
from __future__ import annotations

from app.modules.settings.routes import router

__all__ = ["router"]
