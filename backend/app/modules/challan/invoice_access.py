"""Audit-log write + read helpers for the public challan-QR invoice viewer.

Kept separate from the HTTP layer so the recording primitive (one row per PIN
submission) and the dashboard aggregations are unit-testable without a request.

* `record_access` — insert one audit row (defensive: swallows + rolls back on
  failure so a logging problem can never propagate into the visitor's response).
* `hash_ip` / `client_ip_from_request` — derive the coarse `viewer_hash`.
* `resolve_client_id` — best-effort challan -> project -> client attribution.
* `summary` / `recent` — the two read shapes the operator dashboard consumes.

READS ONLY across the projects module boundary (ProjectClient / Project, via plain
selects); never writes anything but its own `challan_invoice_access` rows.
"""
from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, delete, exists, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.modules.challan.models import AccessOutcome, Challan, ChallanInvoiceAccess
from app.modules.projects.models import Project, ProjectClient

# A FIXED, NON-SECRET salt. `viewer_hash` exists only to (a) count approx-distinct
# viewers and (b) avoid storing a raw IP; it is NOT a security control, so a public,
# stable salt is fine (and keeps hashes comparable across restarts/workers).
_HASH_SALT = "opshub-challan-invoice-access-v1"

_MAX_RECENT = 200

# Coalesce window: a refresh/retry that produces an IDENTICAL (challan, outcome,
# viewer) within this many seconds writes NO new audit row. Bounds unbounded
# audit-table growth from a legit-holder (or attacker) looping POST /d/{token},
# while still logging genuinely-distinct events (a different viewer, a different
# outcome, or the same tuple after the window all still write).
COALESCE_WINDOW_SECONDS = 60


def hash_ip(ip: str | None) -> str | None:
    """Salted, truncated SHA-256 of a client IP (None for a falsy IP).

    Coarse-privacy fingerprint for approx-distinct viewer counting — deliberately
    NOT a security primitive (the salt is a public module constant)."""
    if not ip:
        return None
    return hashlib.sha256((_HASH_SALT + ip).encode("utf-8")).hexdigest()[:32]


def client_ip_from_request(request: Any) -> str | None:  # noqa: ANN401
    """Best-effort client IP: our Cloudflare worker's `x-client-ip`, else the first
    hop of `x-forwarded-for`, else the socket peer. None when nothing is available."""
    headers = getattr(request, "headers", None)
    if headers is not None:
        direct = (headers.get("x-client-ip") or "").strip()
        if direct:
            return direct[:64]
        forwarded = headers.get("x-forwarded-for") or ""
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop[:64]
    peer = getattr(request, "client", None)
    host = getattr(peer, "host", None)
    if host:
        return str(host).strip()[:64] or None
    return None


def resolve_client_id(db: Session, challan: Challan) -> int | None:
    """The client owning the challan's project, resolved by the snapshotted project
    code (ANY status — attribution is best-effort, not an access decision), else None."""
    code = (challan.project_code or "").strip().upper()
    if not code:
        return None
    return db.execute(
        select(Project.client_id).where(Project.code == code)
    ).scalar_one_or_none()


def record_access(
    db: Session,
    *,
    challan: Challan,
    client_id: int | None,
    outcome: str,
    viewer_hash: str | None,
) -> bool:
    """Record one PIN submission, COALESCING near-duplicate requests.

    Returns True if a row was written, False if it was coalesced away (an identical
    (challan_id, outcome, viewer_hash) already logged within COALESCE_WINDOW_SECONDS)
    or if the write failed. Defensive: on any failure it rolls back and returns False
    so a logging problem never surfaces to the caller. The caller commits.

    The dedup EXISTS probe hits the indexed `challan_id`; a NULL viewer_hash is matched
    with `is_(None)` (SQL `= NULL` never matches), so anonymous refreshes coalesce too."""
    try:
        cutoff = datetime.now(UTC) - timedelta(seconds=COALESCE_WINDOW_SECONDS)
        viewer_cond = (
            ChallanInvoiceAccess.viewer_hash.is_(None)
            if viewer_hash is None
            else ChallanInvoiceAccess.viewer_hash == viewer_hash
        )
        already = db.execute(
            select(
                exists().where(
                    ChallanInvoiceAccess.challan_id == challan.id,
                    ChallanInvoiceAccess.outcome == outcome,
                    viewer_cond,
                    ChallanInvoiceAccess.accessed_at >= cutoff,
                )
            )
        ).scalar()
        if already:
            return False
        row = ChallanInvoiceAccess(
            challan_id=challan.id,
            client_id=client_id,
            outcome=outcome,
            viewer_hash=viewer_hash,
        )
        db.add(row)
        db.flush()
        return True
    except Exception:
        # Keep the session usable for the caller's response path (the public route
        # holds no other pending writes, so discarding this add is safe).
        with contextlib.suppress(Exception):
            db.rollback()
        return False


def _day_bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """[date_from 00:00 UTC, date_to+1 00:00 UTC) — inclusive-date half-open range."""
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = (
        datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC)
        + timedelta(days=1)
    )
    return start, end


def _day_str(value: Any) -> str:  # noqa: ANN401
    """Normalize a dialect-specific day value to 'YYYY-MM-DD'.

    sqlite's `date()` yields the text 'YYYY-MM-DD'; postgres' `func.date(...)` yields a
    Python `date`. `str(...)[:10]` collapses both (and any datetime) to the ISO day."""
    return str(value)[:10]


def prune_old(db: Session, *, older_than_days: int) -> int:
    """Delete invoice-access rows older than `older_than_days` and return the count.

    Retention prune (defense-in-depth) for long-term audit-table growth. Caller commits;
    wired into the secret-gated sweep so it runs on the existing scheduled cadence."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = cast(
        "CursorResult[Any]",
        db.execute(
            delete(ChallanInvoiceAccess).where(ChallanInvoiceAccess.accessed_at < cutoff)
        ),
    )
    return int(result.rowcount or 0)


def _utc_day_expr(db: Session) -> Any:  # noqa: ANN401
    """A dialect-aware expression bucketing `accessed_at` to its UTC calendar day.

    Postgres stores timestamptz and renders in the SESSION tz, so we must convert to
    UTC before truncating (else a row logged 23:30 UTC could bucket to the next local
    day); sqlite stores the naive UTC value we wrote, so a plain date() is already UTC.
    Bucketing on UTC matches the [from 00:00 UTC, to+1 00:00 UTC) range bounds."""
    col = ChallanInvoiceAccess.accessed_at
    # get_bind() is the None-safe form of db.bind (same Engine here).
    if db.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("UTC", col))
    return func.date(col)


def summary(
    db: Session,
    *,
    date_from: date,
    date_to: date,
    client_id: int | None = None,
) -> dict[str, Any]:
    """Aggregate PIN-submission stats over an inclusive date range (optionally one
    client). Computed via SQL aggregates (GROUP BY / COUNT / COUNT DISTINCT) over the
    half-open [from 00:00 UTC, to+1 00:00 UTC) range — never loads the matching rows
    into Python, so the cost is bounded by the number of groups, not the log size."""
    start, end = _day_bounds(date_from, date_to)
    conds = [
        ChallanInvoiceAccess.accessed_at >= start,
        ChallanInvoiceAccess.accessed_at < end,
    ]
    if client_id is not None:
        conds.append(ChallanInvoiceAccess.client_id == client_id)

    failed_outcomes = list(AccessOutcome.FAILED)

    # --- totals + by_outcome: one COUNT per outcome ---
    by_outcome: dict[str, int] = {o: 0 for o in AccessOutcome.ALL}
    for outcome, cnt in db.execute(
        select(ChallanInvoiceAccess.outcome, func.count())
        .where(*conds)
        .group_by(ChallanInvoiceAccess.outcome)
    ).all():
        if outcome in by_outcome:
            by_outcome[outcome] = int(cnt or 0)

    total_pin_entries = sum(by_outcome.values())
    total_failed = sum(by_outcome[o] for o in AccessOutcome.FAILED)

    # --- approx distinct viewers (non-null hashes only) ---
    approx_viewers = int(
        db.execute(
            select(func.count(func.distinct(ChallanInvoiceAccess.viewer_hash))).where(
                *conds, ChallanInvoiceAccess.viewer_hash.is_not(None)
            )
        ).scalar()
        or 0
    )

    # --- by_client: one GROUP BY (client_id, outcome) folded in Python (SMALL) +
    #     one GROUP BY client_id for distinct viewers ---
    clients: dict[int | None, dict[str, int]] = {}

    def _bucket(cid: int | None) -> dict[str, int]:
        b = clients.get(cid)
        if b is None:
            b = {"pin_entries": 0, "views": 0, "failed": 0, "approx_viewers": 0}
            clients[cid] = b
        return b

    for cid, outcome, cnt in db.execute(
        select(
            ChallanInvoiceAccess.client_id, ChallanInvoiceAccess.outcome, func.count()
        )
        .where(*conds)
        .group_by(ChallanInvoiceAccess.client_id, ChallanInvoiceAccess.outcome)
    ).all():
        n = int(cnt or 0)
        b = _bucket(cid)
        b["pin_entries"] += n
        if outcome == AccessOutcome.VIEWED:
            b["views"] += n
        if outcome in failed_outcomes:
            b["failed"] += n

    for cid, vcnt in db.execute(
        select(
            ChallanInvoiceAccess.client_id,
            func.count(func.distinct(ChallanInvoiceAccess.viewer_hash)),
        )
        .where(*conds, ChallanInvoiceAccess.viewer_hash.is_not(None))
        .group_by(ChallanInvoiceAccess.client_id)
    ).all():
        _bucket(cid)["approx_viewers"] = int(vcnt or 0)

    # Resolve client name/code for the non-null buckets (single lookup).
    real_ids = [cid for cid in clients if cid is not None]
    names: dict[int, tuple[str, str]] = {}
    if real_ids:
        for pc_id, pc_name, pc_code in db.execute(
            select(ProjectClient.id, ProjectClient.name, ProjectClient.code).where(
                ProjectClient.id.in_(real_ids)
            )
        ).all():
            names[pc_id] = (pc_name, pc_code)

    by_client = []
    # Deterministic order: real clients by id asc, the null-client bucket last.
    for cid in sorted(clients, key=lambda c: (c is None, c or 0)):
        bucket = clients[cid]
        name_code = names.get(cid) if cid is not None else None
        by_client.append(
            {
                "client_id": cid,
                "client_name": name_code[0] if name_code else None,
                "client_code": name_code[1] if name_code else None,
                "pin_entries": bucket["pin_entries"],
                "views": bucket["views"],
                "failed": bucket["failed"],
                "approx_viewers": bucket["approx_viewers"],
            }
        )

    # --- trend: GROUP BY UTC calendar day, ascending, only days with rows ---
    day_expr = _utc_day_expr(db)
    view_sum = func.coalesce(
        func.sum(case((ChallanInvoiceAccess.outcome == AccessOutcome.VIEWED, 1), else_=0)),
        0,
    )
    failed_sum = func.coalesce(
        func.sum(case((ChallanInvoiceAccess.outcome.in_(failed_outcomes), 1), else_=0)),
        0,
    )
    trend = [
        {"date": _day_str(day), "views": int(v or 0), "failed": int(f or 0)}
        for day, v, f in db.execute(
            select(day_expr.label("day"), view_sum, failed_sum)
            .where(*conds)
            .group_by(day_expr)
            .order_by(day_expr)
        ).all()
    ]

    return {
        "total_pin_entries": total_pin_entries,
        "total_views": by_outcome[AccessOutcome.VIEWED],
        "total_not_available": by_outcome[AccessOutcome.NOT_AVAILABLE],
        "total_failed": total_failed,
        "approx_viewers": approx_viewers,
        "by_outcome": {o: by_outcome[o] for o in AccessOutcome.ALL},
        "by_client": by_client,
        "trend": trend,
    }


def recent(
    db: Session,
    *,
    limit: int = 50,
    client_id: int | None = None,
) -> list[dict[str, Any]]:
    """The newest PIN submissions (limit capped at 200). Never leaks viewer_hash/IP."""
    limit = max(1, min(int(limit), _MAX_RECENT))
    conds = []
    if client_id is not None:
        conds.append(ChallanInvoiceAccess.client_id == client_id)
    stmt = (
        select(
            ChallanInvoiceAccess.accessed_at,
            ChallanInvoiceAccess.outcome,
            Challan.number,
            ProjectClient.name,
        )
        .join(Challan, Challan.id == ChallanInvoiceAccess.challan_id)
        .outerjoin(ProjectClient, ProjectClient.id == ChallanInvoiceAccess.client_id)
        .where(*conds)
        .order_by(ChallanInvoiceAccess.accessed_at.desc(), ChallanInvoiceAccess.id.desc())
        .limit(limit)
    )
    return [
        {
            "challan_number": number,
            "client_name": client_name,
            "accessed_at": accessed_at,
            "outcome": outcome,
        }
        for accessed_at, outcome, number, client_name in db.execute(stmt).all()
    ]
