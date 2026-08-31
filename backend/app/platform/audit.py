"""Append-only, hash-chained audit.

Each row's `row_hash = sha256(prev_hash + canonical(payload))`, so any tamper
(edit/delete of an earlier row) breaks the chain and is detectable. The app DB
role must be granted INSERT/SELECT only on `audit_log` — never UPDATE/DELETE.

`log()` is mandatory on every mutating handler (WoW: everything traceable).
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.platform.models import AuditLog


def _iso_utc(ts: datetime) -> str:
    """Canonical UTC isoformat for hashing.

    The hash MUST be reproducible on read-back, but sqlite drops tzinfo on a
    DateTime(timezone=True) round-trip and Postgres renders timestamptz in the
    session TZ — so a naive `ts.isoformat()` differs write-vs-verify and the
    chain falsely reports tampering. Normalizing to explicit UTC on BOTH sides
    (treating a naive value as UTC) makes the serialized timestamp identical.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat()


def _canonical(payload: dict[str, Any]) -> str:
    # Deterministic serialization so the hash is reproducible/verifiable.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_row_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode("utf-8")).hexdigest()


def log(
    db: Session,
    *,
    action: str,
    actor_uid: str | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Append one audit row, chained to the previous row's hash.

    Concurrency-safe: `prev_hash` is UNIQUE, so if another append lands first the
    insert fails and we retry against the new head — the chain can't fork. Each
    attempt runs in a SAVEPOINT so the caller's other uncommitted work is kept.
    """
    ts = datetime.now(UTC)
    payload = {
        "ts": _iso_utc(ts),
        "actor_uid": actor_uid,
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "detail": detail or {},
        "ip": ip,
    }
    for _attempt in range(8):
        last = db.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(1)
        ).scalar_one_or_none()
        prev_hash = last.row_hash if last else ""
        row = AuditLog(
            ts=ts,
            actor_uid=actor_uid,
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail or {},
            ip=ip,
            prev_hash=prev_hash,
            row_hash=compute_row_hash(prev_hash, payload),
        )
        try:
            # `with` RELEASES the savepoint on success — a bare begin_nested() that
            # only rolls back on error leaks a savepoint per call, and thousands in
            # one transaction (e.g. a big challan batch) overflow the commit recursion.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            continue  # another append grabbed this prev_hash; re-read head and retry
        return row
    raise RuntimeError("audit chain contention: could not append after retries")


class ChainStatus(NamedTuple):
    """Outcome of a full chain recomputation.

    `intact` is True when every row's prev/row hash reproduces; `entries_checked` is how
    many rows were examined; `broken_at_id` is the id of the FIRST row whose hash fails
    (None when intact). Verification stops at the first break, so `entries_checked` counts
    the rows examined up to and including that row."""

    intact: bool
    entries_checked: int
    broken_at_id: int | None


def verify_chain_detailed(db: Session) -> ChainStatus:
    """Recompute the whole chain in id order, reporting how far it got and where (if
    anywhere) it broke. Single source of truth for integrity — `verify_chain` wraps this."""
    prev_hash = ""
    checked = 0
    for row in db.execute(select(AuditLog).order_by(AuditLog.id.asc())).scalars():
        checked += 1
        payload = {
            "ts": _iso_utc(row.ts),
            "actor_uid": row.actor_uid,
            "action": row.action,
            "entity": row.entity,
            "entity_id": row.entity_id,
            "detail": row.detail,
            "ip": row.ip,
        }
        if row.prev_hash != prev_hash or row.row_hash != compute_row_hash(prev_hash, payload):
            return ChainStatus(intact=False, entries_checked=checked, broken_at_id=row.id)
        prev_hash = row.row_hash
    return ChainStatus(intact=True, entries_checked=checked, broken_at_id=None)


def verify_chain(db: Session) -> bool:
    """Recompute the whole chain; True if intact (used by a periodic integrity check)."""
    return verify_chain_detailed(db).intact
