"""Async-job polling contract.

`GET /jobs/{job_id}` (mounted at `/api/v1`, so `GET /api/v1/jobs/{job_id}`)
returns the pollable job state. Job ids are sequential integers, so a read is
scoped to the job's creator (or an Admin) — 404 (not 403) otherwise, so a
probing user can't confirm a job exists.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.platform.auth import current_user
from app.platform.jobs import Job
from app.platform.models import User
from app.platform.rbac import is_administrator

router = APIRouter()


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Poll a job. 404 if it does not exist OR the caller isn't its creator/Admin."""
    job = db.get(Job, job_id)
    if job is None or not (is_administrator(user) or job.created_by == user.firebase_uid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "done": job.done,
        "total": job.total,
        "result": job.result,
        "error": job.error,
    }
