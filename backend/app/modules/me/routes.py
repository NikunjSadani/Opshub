"""`GET /me` — the authenticated user's identity + EFFECTIVE permissions.

This is the single contract the frontend reads to drive nav + action gating, and the
one the real Firebase provider will call after login. It reports the user's own view
of their access — never anyone else's — so it needs no special permission beyond being
an active, authenticated user.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.platform.auth import current_user
from app.platform.client_ip import client_ip_from_request
from app.platform.models import LoginEvent, PlatformPerm, User
from app.platform.module_registry import grantable_modules
from app.platform.rbac import has_platform, is_administrator, module_level

router = APIRouter()


def _as_utc(value: datetime) -> datetime:
    """Treat a tz-naive timestamp (sqlite round-trip) as UTC for a safe age comparison."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _touch_last_seen(db: Session, user: User) -> None:
    """Stamp `last_seen_at = now` on GET /me, THROTTLED (write only when unset or older
    than settings.last_seen_throttle_seconds). Best-effort: a failure here must never
    break /me, so any error rolls back and is swallowed."""
    try:
        now = datetime.now(UTC)
        throttle = get_settings().last_seen_throttle_seconds
        last = user.last_seen_at
        if last is None or (now - _as_utc(last)).total_seconds() >= throttle:
            user.last_seen_at = now
            db.commit()
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()


class MeOut(BaseModel):
    id: int
    email: str
    name: str
    role_id: int | None
    role_name: str | None
    is_administrator: bool
    # module_key -> level ("VIEW"|"OPERATE"|"MANAGE"); only modules the user can access.
    module_levels: dict[str, str]
    # platform permission keys the user holds ("iam", "settings").
    platform: list[str]


@router.get("/me", response_model=MeOut)
def read_me(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> MeOut:
    # Stamp "last active" (throttled, best-effort) before building the unchanged response.
    _touch_last_seen(db, user)
    levels: dict[str, str] = {}
    for spec in grantable_modules():
        level = module_level(user, spec.key)
        if level is not None:
            levels[spec.key] = level.value
    platform = [p.value for p in PlatformPerm if has_platform(user, p)]
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        role_id=user.role_id,
        role_name=user.role.name if user.role is not None else None,
        is_administrator=is_administrator(user),
        module_levels=levels,
        platform=platform,
    )


def _recent_duplicate_login(
    db: Session, *, user_id: int, ip: str | None, user_agent: str | None, window_seconds: int
) -> bool:
    """True if this user already has a `login_event` within `window_seconds` carrying the
    SAME (ip, user_agent). Mirrors the last_seen throttle: an indexed EXISTS on
    (user_id, occurred_at) with an ip/ua match, NULLs matched via `is_(None)` (SQL `= NULL`
    never matches). Best-effort — any failure returns False so record_login_event falls
    through to a normal insert and never 500s on the throttle check."""
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
        ip_cond = LoginEvent.ip.is_(None) if ip is None else LoginEvent.ip == ip
        ua_cond = (
            LoginEvent.user_agent.is_(None)
            if user_agent is None
            else LoginEvent.user_agent == user_agent
        )
        return bool(
            db.execute(
                select(
                    exists().where(
                        LoginEvent.user_id == user_id,
                        LoginEvent.occurred_at >= cutoff,
                        ip_cond,
                        ua_cond,
                    )
                )
            ).scalar()
        )
    except Exception:
        return False


@router.post("/auth/login-event", status_code=204)
def record_login_event(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Record an explicit sign-in for the authenticated user: one `login_event` row (IP via
    the Cloudflare-forwarded header, UA truncated to fit) + stamp `last_login_at = now`.
    Called by the SPA right after a successful Firebase login. Returns 204.

    COALESCED (like the /me last_seen throttle): a repeat sign-in with the SAME (ip,
    user_agent) inside settings.login_event_coalesce_seconds writes NO new row and does NOT
    re-stamp last_login_at, so an authenticated client can't loop this to flood login_event
    on the small prod DB. Still 204 either way — idempotent from the client's view."""
    now = datetime.now(UTC)
    ua_raw = request.headers.get("user-agent")
    ua = ua_raw[:400] if ua_raw else None
    ip = client_ip_from_request(request)
    coalesce = get_settings().login_event_coalesce_seconds
    if _recent_duplicate_login(
        db, user_id=user.id, ip=ip, user_agent=ua, window_seconds=coalesce
    ):
        return Response(status_code=204)
    db.add(LoginEvent(user_id=user.id, occurred_at=now, ip=ip, user_agent=ua))
    user.last_login_at = now
    db.commit()
    return Response(status_code=204)
