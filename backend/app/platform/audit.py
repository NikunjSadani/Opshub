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
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.platform.models import AuditLog


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
        "ts": ts.isoformat(),
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
        sp = db.begin_nested()
        try:
            db.add(row)
            db.flush()
            return row
        except IntegrityError:
            sp.rollback()  # another append grabbed this prev_hash; re-read head and retry
    raise RuntimeError("audit chain contention: could not append after retries")


def verify_chain(db: Session) -> bool:
    """Recompute the whole chain; True if intact (used by a periodic integrity check)."""
    prev_hash = ""
    for row in db.execute(select(AuditLog).order_by(AuditLog.id.asc())).scalars():
        payload = {
            "ts": row.ts.isoformat(),
            "actor_uid": row.actor_uid,
            "action": row.action,
            "entity": row.entity,
            "entity_id": row.entity_id,
            "detail": row.detail,
            "ip": row.ip,
        }
        if row.prev_hash != prev_hash or row.row_hash != compute_row_hash(prev_hash, payload):
            return False
        prev_hash = row.row_hash
    return True
