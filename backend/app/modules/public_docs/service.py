"""Resolvers + a tiny in-process rate limiter for the public invoice viewer.

Kept separate from the HTTP/HTML layer so the security-relevant logic (which
challan resolves to which confirmed invoice, and the per-token failed-attempt
throttle) is unit-testable without a request.

READS ONLY across module boundaries: Challan, Project/ProjectClient, SalesInvoice.
Never writes. All cross-module lookups go through plain selects / the projects
resolver (the module-boundary rule).
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.models import SalesInvoice, SalesInvoiceStatus
from app.modules.challan.models import Challan
from app.modules.projects.models import ProjectClient
from app.modules.projects.service import resolve_active_project

# --- rate limit (per token, in-process) ---------------------------------------
# A deliberately simple throttle: after MAX_FAILED_ATTEMPTS wrong passwords within
# a rolling COOLDOWN_SECONDS window, that token is locked (429) until the window
# elapses. In-process only (per worker) — good enough as a brute-force speed-bump
# for a low-traffic public link; a distributed limiter is overkill here. Only
# RESOLVED tokens ever get an entry, so the map is bounded by real challans (an
# unknown/guessed token 404s before it can allocate state -> no memory-exhaustion
# vector from token spraying).
MAX_FAILED_ATTEMPTS = 5
COOLDOWN_SECONDS = 300  # 5 minutes

# token -> (failed_count, window_start_monotonic)
_attempts: dict[str, tuple[int, float]] = {}


def reset_rate_limits() -> None:
    """Clear all throttle state (test hook; also usable operationally)."""
    _attempts.clear()


def is_rate_limited(token: str) -> bool:
    """True if this token is currently locked out. Expired windows self-heal."""
    rec = _attempts.get(token)
    if rec is None:
        return False
    count, started = rec
    if time.monotonic() - started > COOLDOWN_SECONDS:
        _attempts.pop(token, None)
        return False
    return count >= MAX_FAILED_ATTEMPTS


def record_failure(token: str) -> None:
    """Count one failed password attempt for a (resolved) token."""
    now = time.monotonic()
    rec = _attempts.get(token)
    if rec is None or now - rec[1] > COOLDOWN_SECONDS:
        _attempts[token] = (1, now)
    else:
        _attempts[token] = (rec[0] + 1, rec[1])


def clear_failures(token: str) -> None:
    """Forget the failed-attempt history for a token (on a successful unlock)."""
    _attempts.pop(token, None)


# --- resolvers ----------------------------------------------------------------

def resolve_challan_by_token(db: Session, token: str) -> Challan | None:
    """The single challan bearing this opaque access token, else None."""
    if not token:
        return None
    return db.execute(
        select(Challan).where(Challan.access_token == token)
    ).scalar_one_or_none()


def resolve_client_for_challan(db: Session, challan: Challan) -> ProjectClient | None:
    """The client that owns the challan's (ACTIVE) project, else None.

    The challan snapshots a project CODE; we go code -> ACTIVE Project -> its
    ProjectClient. A project that is missing/ON_HOLD/CLOSED resolves to None, which
    the caller treats as "access not available" (never revealing why).
    """
    project = resolve_active_project(db, challan.project_code)
    if project is None:
        return None
    return db.get(ProjectClient, project.client_id)


def resolve_confirmed_invoice(db: Session, challan: Challan) -> SalesInvoice | None:
    """Late-bind the challan to its client invoice.

    Returns the newest CONFIRMED `SalesInvoice` whose `invoice_number` matches the
    challan's AND whose `client_id` is the challan's client, else None. Only
    CONFIRMED (immutable) invoices are ever served publicly; drafts/needs-review
    documents never leak. The (client_id, invoice_number) pair is unique per the
    billing UNIQUE constraint, so at most one row matches once the status filter is
    applied — the ordering is a belt-and-braces determinism guard.
    """
    if not challan.invoice_number:
        return None
    client = resolve_client_for_challan(db, challan)
    if client is None:
        return None
    return db.execute(
        select(SalesInvoice)
        .where(
            SalesInvoice.invoice_number == challan.invoice_number,
            SalesInvoice.client_id == client.id,
            SalesInvoice.status == SalesInvoiceStatus.CONFIRMED.value,
        )
        .order_by(SalesInvoice.created_at.desc(), SalesInvoice.id.desc())
    ).scalars().first()
