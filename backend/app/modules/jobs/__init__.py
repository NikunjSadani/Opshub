"""Jobs module — the async-job polling endpoint.

Mounts at `/api/v1` (NOT `/api/v1/jobs`) so the route lands at
`GET /api/v1/jobs/{job_id}`. See `routes.router`.
"""
from __future__ import annotations

from app.modules.jobs.routes import router

__all__ = ["router"]
