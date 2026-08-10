"""Numbering engine — the statutory-integrity core.

Invariants (the whole reason this module exists):

  I1. NO DUPLICATES. Two ACTIVE (RESERVED|ISSUED) allocations can never share a
      `(series, fy, number)`. Enforced twice: a counter row-lock serializes
      allocation, and a unique partial index on non-void rows is the DB backstop
      that holds even if the lock is bypassed (sqlite, a logic bug, a race).
  I2. MONOTONIC, NO REUSE. `counter.last_number` only ever increases and never
      rolls back, so numbers are handed out in order and a voided number is
      never reissued (a cancelled document keeps its number as history).
  I3. SAFE RETRIES. An allocation carrying an `idempotency_key` resumes the same
      reservation instead of consuming a new number.
  I4. FY IN IST. The financial year (Apr–Mar) is computed in Asia/Kolkata, so a
      challan cut at 11pm on 31-Mar IST lands in the correct FY regardless of
      server timezone.

A series/FY must be EXPLICITLY configured (seeded) before its first allocation:
`allocate` refuses an unconfigured counter rather than defaulting to 1, so a
sequence migrated from a manual book at 188 can't silently reissue 000001 if the
operator forgets the mode-2 seed.

Lifecycle:  allocate -> RESERVED -> issue -> ISSUED
            RESERVED -> void -> VOID   ·   ISSUED -> void -> VOID
A RESERVED row whose document never got generated is swept to VOID by
`sweep_orphaned_reservations` (the counter still advanced — a documented gap is
acceptable; a duplicate is not).

Every mutation (reserve / issue / void / seed) is audited inside the caller's
transaction, so the trail commits atomically with the change.

Transaction contract: `allocate` returns a RESERVED row and the caller MUST
commit promptly (before generating the document) — the counter row-lock is held
until that commit. Do NOT hold the allocating transaction open across document
generation; reserve+commit, then generate, then `issue`+commit.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.numbering.models import (
    ACTIVE_STATUSES,
    AllocationStatus,
    NumberingAllocation,
    NumberingCounter,
)
from app.platform import audit

IST = ZoneInfo("Asia/Kolkata")
_ISSUER_PREFIX = "GIF/DC"  # GIF (Gifsy) / DC (Delivery Challan)
_NUMBER_WIDTH = 6
_MAX_NUMBER = 10**_NUMBER_WIDTH - 1  # 999999 — the sequence width ceiling
_MAX_CONTENTION_RETRIES = 8
_SERIES_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_FY_RE = re.compile(r"^([0-9]{2})-([0-9]{2})$")  # ASCII digits only (\d is Unicode-aware)


class NumberingError(Exception):
    """Raised for invalid numbering operations (bad transition, seed conflict)."""


# ------------------------------------------------------------------ validation

def _clean_series(series: str) -> str:
    """Normalize + validate a series code: strip, upper-case, 1–8 alphanumerics."""
    series = series.strip().upper()
    if not _SERIES_RE.match(series):
        raise NumberingError("series must be 1-8 letters/digits")
    return series


def _clean_fy(fy: str | None) -> str:
    """Validate an explicit FY label, or fall back to the current IST FY.

    A provided FY must be `NN-NN` with consecutive halves (e.g. "26-27"); an
    empty string is NOT silently coerced — it's rejected, so a blank never
    quietly reopens the current FY.
    """
    if fy is None:
        return current_fy()
    m = _FY_RE.match(fy)
    if not m or (int(m.group(2)) - int(m.group(1))) % 100 != 1:
        raise NumberingError("fy must be consecutive two-digit years, e.g. '26-27'")
    return fy


# --------------------------------------------------------------------------- FY

def fy_for(dt: datetime) -> str:
    """Indian financial year label (Apr–Mar) for `dt`, computed in IST.

    Returns e.g. "26-27" for any instant from 2026-04-01 to 2027-03-31 IST.
    A naive datetime is treated as UTC before converting (never guess local).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ist = dt.astimezone(IST)
    start_year = ist.year if ist.month >= 4 else ist.year - 1
    return f"{start_year % 100:02d}-{(start_year + 1) % 100:02d}"


def current_fy() -> str:
    return fy_for(datetime.now(UTC))


def format_number(series: str, fy: str, number: int) -> str:
    """`GIF/DC/26-27/L/000189` — FY embedded, series letter, zero-padded number."""
    return f"{_ISSUER_PREFIX}/{fy}/{series}/{number:0{_NUMBER_WIDTH}d}"


# -------------------------------------------------------------------- allocate

def _lock_counter(db: Session, series: str, fy: str) -> NumberingCounter | None:
    """Return the (series, fy) counter row locked FOR UPDATE, or None if absent.

    Does NOT create the row: an unconfigured series must be seeded first.
    """
    return db.execute(
        select(NumberingCounter)
        .where(NumberingCounter.series == series, NumberingCounter.fy == fy)
        .with_for_update()
    ).scalar_one_or_none()


def _lock_or_create_counter(db: Session, series: str, fy: str) -> NumberingCounter:
    """Return the (series, fy) counter row, locked FOR UPDATE, creating it at 0.

    Used by seeding — the one place allowed to bring a series into existence. On
    Postgres the row-lock serializes concurrent writers; on the create path a
    concurrent insert is contained with a SAVEPOINT + unique(series, fy): the
    loser rolls back its savepoint and re-selects the winner's row (locked).
    """
    counter = _lock_counter(db, series, fy)
    if counter is not None:
        return counter

    counter = NumberingCounter(series=series, fy=fy, last_number=0)
    sp = db.begin_nested()
    try:
        db.add(counter)
        db.flush()
        return counter
    except IntegrityError:
        sp.rollback()  # someone created it first — re-select and lock the winner
    locked = _lock_counter(db, series, fy)
    if locked is None:  # pragma: no cover - the winning row must exist post-race
        raise NumberingError(f"counter vanished for {series}/{fy}")
    return locked


def _resume_for_key(
    db: Session, idempotency_key: str, series: str, fy: str
) -> tuple[NumberingAllocation | None, NumberingAllocation | None]:
    """Resolve a request's idempotency key against existing allocations.

    `idempotency_key` is globally unique, so a key seen before could belong to a
    DIFFERENT series/fy — resuming it blindly would stamp a document with the
    wrong series or financial year. Returns `(resume, to_free)`:
      * (row, None)  — an active reservation for THIS series/fy: resume it (I3).
      * (None, row)  — a voided key row for THIS series/fy: free it, re-reserve.
      * (None, None) — key unseen: allocate fresh.
    Raises if the key is held by a different series/fy (misuse across sequences).
    """
    existing = db.execute(
        select(NumberingAllocation).where(
            NumberingAllocation.idempotency_key == idempotency_key
        )
    ).scalar_one_or_none()
    if existing is None:
        return None, None
    if existing.series != series or existing.fy != fy:
        raise NumberingError(
            f"idempotency_key reused across sequences "
            f"({existing.series}/{existing.fy} vs {series}/{fy})"
        )
    if existing.status != AllocationStatus.VOID.value:
        return existing, None
    return None, existing


def allocate(
    db: Session,
    series: str,
    *,
    fy: str | None = None,
    idempotency_key: str | None = None,
    reserved_by: str | None = None,
) -> NumberingAllocation:
    """Reserve the next number for `series` in the current (or given) FY.

    The series/FY must already be configured (seeded) — an unconfigured counter
    raises `NumberingError` rather than starting at 1.

    Idempotent: a repeated `idempotency_key` whose reservation is still active
    returns that SAME allocation (no new number). If the prior allocation for the
    key was voided, a fresh number is reserved and the key is moved to the new
    row atomically.

    Returns a RESERVED allocation. Caller commits promptly, generates the
    document, then calls `issue`.
    """
    series = _clean_series(series)
    fy = _clean_fy(fy)

    to_free: NumberingAllocation | None = None
    if idempotency_key is not None:
        resumed, to_free = _resume_for_key(db, idempotency_key, series, fy)
        if resumed is not None:
            return resumed  # I3: resume the live reservation for THIS series/fy

    last_err: IntegrityError | None = None
    for attempt in range(_MAX_CONTENTION_RETRIES):
        counter = _lock_counter(db, series, fy)
        if counter is None:
            raise NumberingError(
                f"series {series}/{fy} is not configured — seed the starting "
                "number before allocating"
            )
        ceiling = counter.last_number
        if attempt > 0:
            # A previous try hit the backstop: the counter read was stale (lock
            # ineffective / a racing writer). Consult the TRUE active ceiling so
            # the retry lands on a genuinely-free number instead of looping.
            ceiling = max(ceiling, _max_active_number(db, series, fy))
        number = ceiling + 1
        if number > _MAX_NUMBER:
            raise NumberingError(
                f"series {series}/{fy} exhausted the {_NUMBER_WIDTH}-digit range"
            )
        alloc = NumberingAllocation(
            series=series,
            fy=fy,
            number=number,
            formatted=format_number(series, fy, number),
            status=AllocationStatus.RESERVED.value,
            idempotency_key=idempotency_key,
            reserved_by=reserved_by,
        )
        sp = db.begin_nested()
        try:
            if to_free is not None:
                to_free.idempotency_key = None  # free inside the same savepoint
                db.flush()
            db.add(alloc)
            db.flush()  # trips the partial unique index if `number` is already active
        except IntegrityError as err:
            sp.rollback()
            last_err = err
            # A concurrent replay of the same key may have won the race; if an
            # active reservation now holds our key, resume it (I3) instead of
            # burning another number.
            if idempotency_key is not None:
                winner, _ = _resume_for_key(db, idempotency_key, series, fy)
                if winner is not None:
                    return winner
            continue
        counter.last_number = number  # advance the high-water mark (I2: monotonic)
        db.flush()
        audit.log(
            db,
            action="numbering.reserve",
            actor_uid=reserved_by,
            entity="numbering_allocation",
            entity_id=alloc.formatted,
            detail={"series": series, "fy": fy, "number": number},
        )
        return alloc
    raise NumberingError(f"numbering contention on {series}/{fy}: {last_err}")


def _max_active_number(db: Session, series: str, fy: str) -> int:
    """Highest number currently held by an ACTIVE (non-void) allocation, or 0."""
    result = db.execute(
        select(func.max(NumberingAllocation.number)).where(
            NumberingAllocation.series == series,
            NumberingAllocation.fy == fy,
            NumberingAllocation.status.in_([s.value for s in ACTIVE_STATUSES]),
        )
    ).scalar()
    return result or 0


# ------------------------------------------------------------ state transitions

def _require_persistent(alloc: NumberingAllocation) -> None:
    """A transition re-reads via `db.refresh`, which needs a persistent row.

    Turn SQLAlchemy's raw InvalidRequestError on a pending/detached object into a
    domain error so callers get a consistent failure type on a critical path.
    """
    if not inspect(alloc).persistent:
        raise NumberingError("allocation is not persistent in this session")


def issue(
    db: Session,
    alloc: NumberingAllocation,
    *,
    entity: str,
    entity_id: str,
    actor_uid: str | None = None,
) -> NumberingAllocation:
    """RESERVED -> ISSUED, binding the number to a generated document.

    Re-reads the row under a row-lock and re-validates status inside this
    transaction (TOCTOU-safe): a number VOIDed concurrently by an admin can NOT
    be resurrected to ISSUED off a stale in-memory object.
    """
    _require_persistent(alloc)
    db.refresh(alloc, with_for_update=True)
    if alloc.status != AllocationStatus.RESERVED.value:
        raise NumberingError(f"cannot issue from {alloc.status}")
    alloc.status = AllocationStatus.ISSUED.value
    alloc.entity = entity
    alloc.entity_id = entity_id
    alloc.issued_at = datetime.now(UTC)
    db.flush()
    audit.log(
        db,
        action="numbering.issue",
        actor_uid=actor_uid,
        entity="numbering_allocation",
        entity_id=alloc.formatted,
        detail={"bound_to": entity, "bound_id": entity_id},
    )
    return alloc


def void(
    db: Session,
    alloc: NumberingAllocation,
    *,
    reason: str,
    actor_uid: str | None = None,
) -> NumberingAllocation:
    """RESERVED|ISSUED -> VOID. Terminal; the number is retained, never reused.

    Re-reads under a row-lock so a double-void or a void racing an issue is
    resolved against the committed state, not a stale object.
    """
    if not reason or not reason.strip():
        raise NumberingError("void requires a reason")
    _require_persistent(alloc)
    db.refresh(alloc, with_for_update=True)
    if alloc.status == AllocationStatus.VOID.value:
        raise NumberingError("allocation already void")
    alloc.status = AllocationStatus.VOID.value
    alloc.void_reason = reason.strip()
    alloc.voided_at = datetime.now(UTC)
    db.flush()
    audit.log(
        db,
        action="numbering.void",
        actor_uid=actor_uid,
        entity="numbering_allocation",
        entity_id=alloc.formatted,
        detail={"reason": alloc.void_reason},
    )
    return alloc


# ------------------------------------------------------------------- seed (M2)

def seed_series(
    db: Session,
    series: str,
    *,
    fy: str | None = None,
    last_number: int,
    actor_uid: str | None = None,
) -> NumberingCounter:
    """Mode-2 custom-start: set the high-water mark for (series, fy).

    Used when migrating from an existing manual sequence (e.g. set to 188 so the
    next issue is 189), and the required first step to CONFIGURE a new series.
    Guarded so it can NEVER cause a duplicate: the new value must be >= the
    highest ACTIVE number already allocated for (series, fy) and >= the current
    high-water mark (I2: monotonic, no going backwards under issued numbers). A
    fat-finger past the sequence width is rejected up front.
    """
    series = _clean_series(series)
    fy = _clean_fy(fy)
    if last_number < 0:
        raise NumberingError("last_number must be >= 0")
    if last_number > _MAX_NUMBER:
        raise NumberingError(f"last_number exceeds the {_NUMBER_WIDTH}-digit range")

    counter = _lock_or_create_counter(db, series, fy)
    floor = max(counter.last_number, _max_active_number(db, series, fy))
    if last_number < floor:
        raise NumberingError(
            f"seed {last_number} below current floor {floor} for {series}/{fy} "
            "(would risk reissuing an existing number)"
        )
    counter.last_number = last_number
    db.flush()
    audit.log(
        db,
        action="numbering.seed",
        actor_uid=actor_uid,
        entity="numbering_counter",
        entity_id=f"{series}/{fy}",
        detail={"last_number": last_number},
    )
    return counter


# ---------------------------------------------------------- reconcile sweeper

def sweep_orphaned_reservations(
    db: Session, *, older_than: timedelta, now: datetime | None = None
) -> list[NumberingAllocation]:
    """Void RESERVED allocations older than `older_than` that were never issued.

    Uses a SINGLE guarded, atomic UPDATE (`WHERE status='RESERVED' AND
    reserved_at < cutoff`) — the status predicate is re-evaluated at write time,
    so a reservation ISSUED in the window between selecting and voiding is NOT
    clobbered (no lost-update). Each void is audited (RESERVED→VOID is a
    statutory transition), and the returned rows are expired so a caller reads
    the committed VOID state, not the pre-update snapshot. Caller commits.
    """
    now = now or datetime.now(UTC)
    cutoff = now - older_than
    reason = "orphaned-reservation (never issued)"
    stmt = (
        update(NumberingAllocation)
        .where(
            NumberingAllocation.status == AllocationStatus.RESERVED.value,
            NumberingAllocation.reserved_at < cutoff,
        )
        .values(
            status=AllocationStatus.VOID.value,
            void_reason=reason,
            voided_at=now,
        )
        .returning(NumberingAllocation)
        .execution_options(synchronize_session=False)
    )
    swept = list(db.execute(stmt).scalars().all())
    for alloc in swept:
        audit.log(
            db,
            action="numbering.void",
            actor_uid="system:sweeper",
            entity="numbering_allocation",
            entity_id=alloc.formatted,
            detail={"reason": reason, "swept": True},
        )
        db.expire(alloc)  # so attribute reads reflect the committed VOID state
    return swept
