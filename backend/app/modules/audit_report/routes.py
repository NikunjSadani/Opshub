"""Admin-only "Audit & Access" report HTTP surface (mounted at `/api/v1`).

  GET /admin/audit/events     -> hash-chained audit-log rows (filters + pagination + CSV)
  GET /admin/audit/logins     -> explicit sign-in rows (filters + pagination + CSV)
  GET /admin/audit/integrity  -> recompute the audit hash-chain

Every route is gated on the IAM platform permission (`has_platform(user, PlatformPerm.IAM)`)
-> 403 otherwise. JSON pages return `{items, has_more}`; `?format=csv` on events/logins
returns an injection-guarded text/csv attachment instead.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.audit_report import service
from app.platform.auth import current_user
from app.platform.models import PlatformPerm, User
from app.platform.rbac import has_platform

router = APIRouter()


def _require_iam(user: User) -> None:
    """Gate: the Audit & Access report requires the IAM platform permission."""
    if not has_platform(user, PlatformPerm.IAM):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "the Audit & Access report requires the IAM permission"
        )


def _csv_response(body: bytes, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/audit/events", response_model=None)
def audit_events(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    entity: Annotated[str | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    format: Annotated[str | None, Query()] = None,
) -> dict[str, Any] | Response:
    """Audit-log rows newest-first with actor resolved. `?format=csv` exports the full
    filtered set (capped) instead of a JSON page."""
    _require_iam(user)
    if format == "csv":
        rows = service.export_events(
            db, actor=actor, action=action, entity=entity,
            date_from=date_from, date_to=date_to,
        )
        return _csv_response(service.events_csv(rows), "audit-events.csv")
    items, has_more = service.list_events(
        db, actor=actor, action=action, entity=entity,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )
    return {"items": items, "has_more": has_more}


@router.get("/admin/audit/logins", response_model=None)
def audit_logins(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    user_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    format: Annotated[str | None, Query()] = None,
) -> dict[str, Any] | Response:
    """Explicit sign-in rows newest-first with user resolved. `?format=csv` exports the
    full filtered set (capped) instead of a JSON page."""
    _require_iam(user)
    if format == "csv":
        rows = service.export_logins(
            db, user_id=user_id, date_from=date_from, date_to=date_to,
        )
        return _csv_response(service.logins_csv(rows), "audit-logins.csv")
    items, has_more = service.list_logins(
        db, user_id=user_id, date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )
    return {"items": items, "has_more": has_more}


@router.get("/admin/audit/integrity")
def audit_integrity(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    """Recompute the audit hash-chain -> {intact, entries_checked, broken_at_id}."""
    _require_iam(user)
    return service.integrity(db)
