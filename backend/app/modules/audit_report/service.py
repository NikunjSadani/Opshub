"""Read model + CSV export for the admin-only "Audit & Access" report.

Three read shapes, all admin-gated at the HTTP layer:
  * `list_events`   — hash-chained audit-log rows (newest-first), actor_uid resolved to
                      the owning user (email/name), with actor/action/entity/date filters.
  * `list_logins`   — explicit sign-in rows (`login_event`), user resolved, date filters.
  * `integrity`     — recompute the audit hash-chain (delegates to app.platform.audit).

Timestamps are emitted as UTC ISO-8601 strings (the model's `ts` maps to the response
field `created_at` the frontend already reads). CSV cells are spreadsheet-injection
guarded with the same helper shape the challan/finance exports use (a leading
`= + - @` / control char is prefixed with an apostrophe; RFC-4180 quote-doubling).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.platform import audit
from app.platform.models import AuditLog, LoginEvent, User

# Hard ceiling on any single page / export, independent of the requested limit.
MAX_LIMIT = 200
# CSV export walks up to this many filtered rows (bounds an unbounded export).
MAX_CSV_ROWS = 10_000


# ------------------------------------------------------------------- time helpers

def _iso(value: datetime | None) -> str | None:
    """UTC ISO-8601 for a stored timestamp, treating a tz-naive value (sqlite round-trip)
    as UTC so the wire format is stable across dialects. None passes through."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _apply_day_range(
    stmt: Any, col: Any, date_from: date | None, date_to: date | None  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Inclusive UTC calendar-day range: [date_from 00:00, date_to+1 00:00). Either bound
    may be omitted (open-ended)."""
    if date_from is not None:
        start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
        stmt = stmt.where(col >= start)
    if date_to is not None:
        end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC) + timedelta(days=1)
        stmt = stmt.where(col < end)
    return stmt


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))


def _actor(user: User | None) -> dict[str, str] | None:
    return None if user is None else {"email": user.email, "name": user.name}


# ------------------------------------------------------------------- events

def list_events(
    db: Session,
    *,
    actor: str | None = None,
    action: str | None = None,
    entity: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Audit-log rows newest-first (`ts` desc), each with its resolved actor. `actor`
    matches actor_uid OR the resolved user's email/name (case-insensitive contains);
    `action`/`entity` are exact matches; the date range filters on `ts`. Returns
    (items, has_more) where has_more reflects rows beyond this page."""
    limit, offset = _clamp(limit, offset)
    rows = _fetch_events(
        db, actor=actor, action=action, entity=entity,
        date_from=date_from, date_to=date_to, limit=limit + 1, offset=offset,
    )
    has_more = len(rows) > limit
    return [_event_item(log, usr) for log, usr in rows[:limit]], has_more


def _fetch_events(
    db: Session,
    *,
    actor: str | None,
    action: str | None,
    entity: str | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> list[tuple[AuditLog, User | None]]:
    # LEFT JOIN so rows with an unknown/null actor_uid still appear (actor -> None).
    stmt = select(AuditLog, User).outerjoin(User, User.firebase_uid == AuditLog.actor_uid)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if entity:
        stmt = stmt.where(AuditLog.entity == entity)
    stmt = _apply_day_range(stmt, AuditLog.ts, date_from, date_to)
    if actor:
        # Escape LIKE metacharacters so a literal `%`/`_` in the user-supplied actor
        # matches literally (not as a wildcard); pair with escape="\\" on each `.like()`.
        needle = f"%{_escape_like(actor.strip().lower())}%"
        stmt = stmt.where(
            or_(
                _lower(AuditLog.actor_uid).like(needle, escape="\\"),
                _lower(User.email).like(needle, escape="\\"),
                _lower(User.name).like(needle, escape="\\"),
            )
        )
    stmt = stmt.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).offset(offset).limit(limit)
    return [(log, usr) for log, usr in db.execute(stmt).all()]


def _event_item(log: AuditLog, usr: User | None) -> dict[str, Any]:
    return {
        "id": log.id,
        "created_at": _iso(log.ts),  # model `ts` -> response `created_at`
        "action": log.action,
        "entity": log.entity,
        "entity_id": log.entity_id,
        "detail": log.detail,
        "ip": log.ip,
        "actor": _actor(usr),
    }


# ------------------------------------------------------------------- logins

def list_logins(
    db: Session,
    *,
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """Explicit sign-in rows newest-first (`occurred_at` desc), each with its resolved
    user. Optional user_id + inclusive UTC date range. Returns (items, has_more)."""
    limit, offset = _clamp(limit, offset)
    rows = _fetch_logins(
        db, user_id=user_id, date_from=date_from, date_to=date_to,
        limit=limit + 1, offset=offset,
    )
    has_more = len(rows) > limit
    return [_login_item(ev, usr) for ev, usr in rows[:limit]], has_more


def _fetch_logins(
    db: Session,
    *,
    user_id: int | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> list[tuple[LoginEvent, User | None]]:
    stmt = select(LoginEvent, User).outerjoin(User, User.id == LoginEvent.user_id)
    if user_id is not None:
        stmt = stmt.where(LoginEvent.user_id == user_id)
    stmt = _apply_day_range(stmt, LoginEvent.occurred_at, date_from, date_to)
    stmt = (
        stmt.order_by(LoginEvent.occurred_at.desc(), LoginEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [(ev, usr) for ev, usr in db.execute(stmt).all()]


def _login_item(ev: LoginEvent, usr: User | None) -> dict[str, Any]:
    return {
        "id": ev.id,
        "occurred_at": _iso(ev.occurred_at),
        "ip": ev.ip,
        "user_agent": ev.user_agent,
        "user": _actor(usr),
    }


def export_events(
    db: Session,
    *,
    actor: str | None = None,
    action: str | None = None,
    entity: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    """The full filtered event set (newest-first) for CSV export, capped at MAX_CSV_ROWS."""
    rows = _fetch_events(
        db, actor=actor, action=action, entity=entity,
        date_from=date_from, date_to=date_to, limit=MAX_CSV_ROWS, offset=0,
    )
    return [_event_item(log, usr) for log, usr in rows]


def export_logins(
    db: Session,
    *,
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    """The full filtered sign-in set (newest-first) for CSV export, capped at MAX_CSV_ROWS."""
    rows = _fetch_logins(
        db, user_id=user_id, date_from=date_from, date_to=date_to,
        limit=MAX_CSV_ROWS, offset=0,
    )
    return [_login_item(ev, usr) for ev, usr in rows]


# ------------------------------------------------------------------- integrity

def integrity(db: Session) -> dict[str, Any]:
    """Recompute the audit hash-chain -> {intact, entries_checked, broken_at_id}."""
    status = audit.verify_chain_detailed(db)
    return {
        "intact": status.intact,
        "entries_checked": status.entries_checked,
        "broken_at_id": status.broken_at_id,
    }


# ------------------------------------------------------------------- CSV export

def events_csv(items: list[dict[str, Any]]) -> bytes:
    """Render audit events to injection-guarded CSV. `detail` is compact JSON (inherently
    formula-safe — a cell that opens with `{`), and every other text-derived cell is
    passed through the leading-char guard."""
    header = "ID,Timestamp,Action,Entity,Entity ID,Detail,IP,Actor Email,Actor Name"
    lines = [header]
    for it in items:
        actor = it.get("actor") or {}
        lines.append(",".join((
            str(it["id"]),
            _csv_field(it.get("created_at") or ""),
            _csv_field(it.get("action") or ""),
            _csv_field(it.get("entity") or ""),
            _csv_field(it.get("entity_id") or ""),
            _csv_field(_detail_str(it.get("detail"))),
            _csv_field(it.get("ip") or ""),
            _csv_field(str(actor.get("email") or "")),
            _csv_field(str(actor.get("name") or "")),
        )))
    return ("\n".join(lines) + "\n").encode("utf-8")


def logins_csv(items: list[dict[str, Any]]) -> bytes:
    """Render sign-in rows to injection-guarded CSV."""
    header = "ID,Occurred At,IP,User Agent,User Email,User Name"
    lines = [header]
    for it in items:
        usr = it.get("user") or {}
        lines.append(",".join((
            str(it["id"]),
            _csv_field(it.get("occurred_at") or ""),
            _csv_field(it.get("ip") or ""),
            _csv_field(it.get("user_agent") or ""),
            _csv_field(str(usr.get("email") or "")),
            _csv_field(str(usr.get("name") or "")),
        )))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _detail_str(detail: Any) -> str:  # noqa: ANN401
    """Compact, deterministic JSON for the detail cell (empty string for a falsy detail)."""
    if not detail:
        return ""
    return json.dumps(detail, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _csv_field(value: str) -> str:
    """Quote a CSV field and neutralize spreadsheet formula/DDE injection (RFC-4180
    quote-doubling; a leading `= + - @` / control char is prefixed with `'`). Mirrors the
    finance/challan CSV guard."""
    value = value.replace('"', '""')
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return f'"{value}"'


def _lower(col: Any) -> Any:  # noqa: ANN401
    """`lower(coalesce(col, ''))` — NULL matches as empty so the OR-filter never drops a
    row just because (say) email is NULL for an unresolved actor."""
    return func.lower(func.coalesce(col, ""))


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed `%`/`_` matches literally (paired with
    `escape="\\"` on the `.like()`). Without this a literal `%` in `actor` matches every
    row. The backslash itself is escaped first so it stays the escape char. Mirrors the
    projects module's helper of the same name."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ------------------------------------------------------------------- retention

def prune_login_events(db: Session, *, older_than_days: int) -> int:
    """Delete `login_event` rows older than now-UTC − `older_than_days`; return the count.

    Retention prune (defense-in-depth) bounding long-term login_event growth on the small
    prod DB, wired into the secret-gated numbering sweep so it runs on the scheduled cadence.
    Caller commits. NOTE: `audit_log` is deliberately NEVER pruned — it is the tamper-evident
    compliance record and must be kept intact."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = cast(
        "CursorResult[Any]",
        db.execute(delete(LoginEvent).where(LoginEvent.occurred_at < cutoff)),
    )
    return int(result.rowcount or 0)
