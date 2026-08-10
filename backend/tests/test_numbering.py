"""Numbering engine — the statutory-integrity core, exercised hard.

Covers the invariants (no-duplicates DB backstop, monotonic/no-reuse, idempotent
retries, IST FY), the lifecycle transition guards, the mode-2 seed guard, the
reconcile sweeper, and the dual-audit regression fixes (configured-counter
requirement, TOCTOU-safe issue, guarded sweeper, issuance auditing, input
bounds). Uses an in-memory sqlite whose schema includes the model's partial
unique index + CHECK, so the backstops are really under test.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.modules.numbering import service
from app.modules.numbering.models import (
    AllocationStatus,
    NumberingAllocation,
    NumberingCounter,
)
from app.platform.models import AuditLog


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            NumberingCounter.__table__,
            NumberingAllocation.__table__,
            AuditLog.__table__,  # service mutations audit inside the caller's txn
        ],
    )
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _configure(db: Session, series: str = "L", fy: str = "26-27", start: int = 0) -> None:
    """Bring a series/FY into existence so allocate() is permitted."""
    service.seed_series(db, series, fy=fy, last_number=start)
    db.flush()


# ------------------------------------------------------------------- FY (IST)

def test_fy_label_format() -> None:
    assert service.fy_for(datetime(2026, 8, 10, tzinfo=UTC)) == "26-27"
    assert service.fy_for(datetime(2026, 4, 1, 0, 0, tzinfo=UTC)) == "26-27"


def test_fy_boundary_is_ist_not_utc() -> None:
    # 2027-03-31 20:00 UTC == 2027-04-01 01:30 IST -> new FY 27-28.
    assert service.fy_for(datetime(2027, 3, 31, 20, 0, tzinfo=UTC)) == "27-28"
    # 2027-03-31 17:00 UTC == 2027-03-31 22:30 IST -> still FY 26-27.
    assert service.fy_for(datetime(2027, 3, 31, 17, 0, tzinfo=UTC)) == "26-27"
    # Jan falls in the PRIOR April's FY.
    assert service.fy_for(datetime(2027, 1, 15, tzinfo=UTC)) == "26-27"


def test_naive_datetime_treated_as_utc() -> None:
    assert service.fy_for(datetime(2026, 8, 10, 12, 0)) == "26-27"


def test_format_number() -> None:
    assert service.format_number("L", "26-27", 189) == "GIF/DC/26-27/L/000189"
    assert service.format_number("L", "26-27", 1) == "GIF/DC/26-27/L/000001"


# ----------------------------------------------------------------- allocate

def test_allocate_requires_configured_series(db: Session) -> None:
    # No seed for this series/FY -> allocate must refuse, not start at 1.
    with pytest.raises(service.NumberingError, match="not configured"):
        service.allocate(db, "L", fy="26-27")


def test_allocate_is_monotonic_and_distinct(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    b = service.allocate(db, "L", fy="26-27")
    c = service.allocate(db, "L", fy="26-27")
    assert [a.number, b.number, c.number] == [1, 2, 3]
    assert a.formatted == "GIF/DC/26-27/L/000001"
    assert c.formatted == "GIF/DC/26-27/L/000003"
    assert all(x.status == AllocationStatus.RESERVED.value for x in (a, b, c))
    counter = db.execute(
        select(NumberingCounter).where(NumberingCounter.series == "L")
    ).scalar_one()
    assert counter.last_number == 3


def test_allocate_is_audited(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27", reserved_by="mis@x")
    actions = [r.action for r in db.execute(select(AuditLog)).scalars()]
    assert "numbering.reserve" in actions
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "numbering.reserve")
    ).scalar_one()
    assert row.entity_id == a.formatted and row.actor_uid == "mis@x"


def test_series_and_fy_are_independent_sequences(db: Session) -> None:
    _configure(db, "L", "26-27")
    _configure(db, "M", "26-27")
    _configure(db, "L", "27-28")
    l1 = service.allocate(db, "L", fy="26-27")
    m1 = service.allocate(db, "M", fy="26-27")
    l_next = service.allocate(db, "L", fy="27-28")
    assert l1.number == 1 and m1.number == 1 and l_next.number == 1


def test_series_is_normalized(db: Session) -> None:
    _configure(db, "L", "26-27")
    a = service.allocate(db, " l ", fy="26-27")  # stripped + upper-cased to "L"
    assert a.series == "L"


def test_idempotency_key_resumes_same_reservation(db: Session) -> None:
    _configure(db)
    first = service.allocate(db, "L", fy="26-27", idempotency_key="batch-1:row-7")
    again = service.allocate(db, "L", fy="26-27", idempotency_key="batch-1:row-7")
    assert first.id == again.id
    assert again.number == first.number
    counter = db.execute(
        select(NumberingCounter).where(NumberingCounter.series == "L")
    ).scalar_one()
    assert counter.last_number == 1  # no new number burned


def test_voided_key_frees_a_fresh_number(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27", idempotency_key="k")
    service.void(db, a, reason="aborted before generation")
    b = service.allocate(db, "L", fy="26-27", idempotency_key="k")
    assert b.id != a.id
    assert b.number == 2  # a new number, not a reuse of 1


def test_invalid_series_rejected(db: Session) -> None:
    with pytest.raises(service.NumberingError):
        service.allocate(db, "  ", fy="26-27")
    with pytest.raises(service.NumberingError):
        service.allocate(db, "A/B", fy="26-27")


def test_invalid_fy_rejected(db: Session) -> None:
    for bad in ["", "2026", "26-28", "../x", "99-01", "٢٦-٢٧"]:  # incl. Unicode digits
        with pytest.raises(service.NumberingError):
            service.seed_series(db, "L", fy=bad, last_number=0)


def test_idempotency_key_reused_across_series_is_rejected(db: Session) -> None:
    _configure(db, "L", "26-27")
    _configure(db, "M", "26-27")
    service.allocate(db, "L", fy="26-27", idempotency_key="shared")
    # Same key for a DIFFERENT series must NOT resume the L number.
    with pytest.raises(service.NumberingError, match="reused across sequences"):
        service.allocate(db, "M", fy="26-27", idempotency_key="shared")


def test_idempotency_key_reused_across_fy_is_rejected(db: Session) -> None:
    _configure(db, "L", "26-27")
    _configure(db, "L", "27-28")
    service.allocate(db, "L", fy="26-27", idempotency_key="k")
    with pytest.raises(service.NumberingError, match="reused across sequences"):
        service.allocate(db, "L", fy="27-28", idempotency_key="k")


def test_transition_on_non_persistent_object_raises_domain_error(db: Session) -> None:
    transient = NumberingAllocation(
        series="L", fy="26-27", number=1,
        formatted=service.format_number("L", "26-27", 1),
        status=AllocationStatus.RESERVED.value,
    )
    with pytest.raises(service.NumberingError, match="not persistent"):
        service.issue(db, transient, entity="challan", entity_id="1")


# --------------------------------------------------------- duplicate backstop

def test_partial_index_blocks_two_active_same_number(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    dup = NumberingAllocation(
        series="L", fy="26-27", number=a.number,
        formatted=a.formatted, status=AllocationStatus.ISSUED.value,
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_void_row_coexists_with_active_same_number(db: Session) -> None:
    db.add(NumberingAllocation(
        series="L", fy="26-27", number=5, formatted=service.format_number("L", "26-27", 5),
        status=AllocationStatus.VOID.value, void_reason="cancelled",
    ))
    db.add(NumberingAllocation(
        series="L", fy="26-27", number=5, formatted=service.format_number("L", "26-27", 5),
        status=AllocationStatus.ISSUED.value,
    ))
    db.flush()  # must NOT raise
    active = db.execute(
        select(NumberingAllocation).where(
            NumberingAllocation.number == 5,
            NumberingAllocation.status == AllocationStatus.ISSUED.value,
        )
    ).scalar_one()
    assert active.number == 5


def test_status_check_constraint_rejects_junk(db: Session) -> None:
    db.add(NumberingAllocation(
        series="L", fy="26-27", number=9,
        formatted=service.format_number("L", "26-27", 9), status="DELETED",
    ))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_allocate_retry_makes_progress_past_a_stolen_number(db: Session) -> None:
    _configure(db)
    service.allocate(db, "L", fy="26-27")  # -> 1, counter=1
    db.add(NumberingAllocation(
        series="L", fy="26-27", number=2, formatted=service.format_number("L", "26-27", 2),
        status=AllocationStatus.ISSUED.value,
    ))
    db.flush()
    nxt = service.allocate(db, "L", fy="26-27")
    assert nxt.number == 3  # skipped the stolen 2, no duplicate, no infinite loop


# ------------------------------------------------------------- transitions

def test_reserve_issue_flow_is_audited(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    service.issue(db, a, entity="challan", entity_id="42", actor_uid="mis@x")
    assert a.status == AllocationStatus.ISSUED.value
    assert a.entity_id == "42" and a.issued_at is not None
    actions = [r.action for r in db.execute(select(AuditLog)).scalars()]
    assert "numbering.issue" in actions


def test_issue_after_void_is_blocked(db: Session) -> None:
    """TOCTOU: a number voided after reservation must NOT be resurrected to ISSUED,
    even off a stale object whose in-memory status is still RESERVED."""
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    db.flush()
    # Simulate a concurrent void committed elsewhere: mutate the DB row directly,
    # bypassing `a`'s identity-map state (which still says RESERVED).
    db.execute(
        NumberingAllocation.__table__.update()
        .where(NumberingAllocation.id == a.id)
        .values(status=AllocationStatus.VOID.value, void_reason="admin cancel")
    )
    assert a.status == AllocationStatus.RESERVED.value  # stale in-memory view
    with pytest.raises(service.NumberingError, match="cannot issue from VOID"):
        service.issue(db, a, entity="challan", entity_id="1")


def test_cannot_issue_twice(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    service.issue(db, a, entity="challan", entity_id="42")
    with pytest.raises(service.NumberingError):
        service.issue(db, a, entity="challan", entity_id="42")


def test_void_requires_reason(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    with pytest.raises(service.NumberingError):
        service.void(db, a, reason="  ")


def test_cannot_void_twice(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")
    service.void(db, a, reason="first")
    with pytest.raises(service.NumberingError):
        service.void(db, a, reason="second")


# -------------------------------------------------------------- seed (M2)

def test_seed_sets_start_then_allocate_continues(db: Session) -> None:
    service.seed_series(db, "L", fy="26-27", last_number=188)
    nxt = service.allocate(db, "L", fy="26-27")
    assert nxt.number == 189
    assert nxt.formatted == "GIF/DC/26-27/L/000189"


def test_seed_cannot_go_below_active_ceiling(db: Session) -> None:
    _configure(db)
    service.allocate(db, "L", fy="26-27")  # 1
    service.allocate(db, "L", fy="26-27")  # 2
    with pytest.raises(service.NumberingError):
        service.seed_series(db, "L", fy="26-27", last_number=1)  # would risk reissue


def test_seed_forward_is_allowed(db: Session) -> None:
    _configure(db)
    service.allocate(db, "L", fy="26-27")  # 1
    service.seed_series(db, "L", fy="26-27", last_number=500)
    nxt = service.allocate(db, "L", fy="26-27")
    assert nxt.number == 501


def test_seed_rejects_negative(db: Session) -> None:
    with pytest.raises(service.NumberingError):
        service.seed_series(db, "L", fy="26-27", last_number=-1)


def test_seed_rejects_above_width(db: Session) -> None:
    with pytest.raises(service.NumberingError):
        service.seed_series(db, "L", fy="26-27", last_number=1_000_000)


def test_seed_is_audited(db: Session) -> None:
    service.seed_series(db, "L", fy="26-27", last_number=188, actor_uid="admin@x")
    row = db.execute(
        select(AuditLog).where(AuditLog.action == "numbering.seed")
    ).scalar_one()
    assert row.entity_id == "L/26-27"


# ---------------------------------------------------------------- sweeper

def test_sweeper_guarded_update_spares_a_concurrently_issued_row(db: Session) -> None:
    """The sweeper's atomic guarded UPDATE must NOT clobber a row that became
    ISSUED after being identified as stale (lost-update regression)."""
    _configure(db)
    stale_issued = service.allocate(db, "L", fy="26-27")
    stale_orphan = service.allocate(db, "L", fy="26-27")
    for a in (stale_issued, stale_orphan):
        a.reserved_at = datetime.now(UTC) - timedelta(hours=48)
    db.flush()
    # This one gets ISSUED before the sweep -> guarded WHERE status='RESERVED'
    # no longer matches it.
    service.issue(db, stale_issued, entity="challan", entity_id="C-1")
    db.flush()

    swept = service.sweep_orphaned_reservations(db, older_than=timedelta(hours=24))
    db.flush()

    swept_by_id = {s.id: s for s in swept}
    assert stale_orphan.id in swept_by_id
    assert stale_issued.id not in swept_by_id
    # Returned rows reflect the committed VOID state (not the pre-update snapshot).
    assert swept_by_id[stale_orphan.id].status == AllocationStatus.VOID.value
    # Re-read from DB: the issued row is untouched, the orphan is void.
    fresh_issued = db.get(NumberingAllocation, stale_issued.id)
    fresh_orphan = db.get(NumberingAllocation, stale_orphan.id)
    db.refresh(fresh_issued)
    db.refresh(fresh_orphan)
    assert fresh_issued.status == AllocationStatus.ISSUED.value
    assert fresh_orphan.status == AllocationStatus.VOID.value
    # The sweeper's statutory RESERVED->VOID is audited.
    swept_audits = [
        r for r in db.execute(select(AuditLog)).scalars()
        if r.action == "numbering.void" and (r.detail or {}).get("swept")
    ]
    assert len(swept_audits) == 1
    assert swept_audits[0].actor_uid == "system:sweeper"


def test_sweeper_spares_fresh_reservations(db: Session) -> None:
    _configure(db)
    fresh = service.allocate(db, "L", fy="26-27")
    db.flush()
    swept = service.sweep_orphaned_reservations(db, older_than=timedelta(hours=24))
    assert fresh.id not in {s.id for s in swept}


def test_swept_number_is_not_reused(db: Session) -> None:
    _configure(db)
    a = service.allocate(db, "L", fy="26-27")  # 1
    a.reserved_at = datetime.now(UTC) - timedelta(hours=48)
    db.flush()
    service.sweep_orphaned_reservations(db, older_than=timedelta(hours=24))
    db.flush()
    nxt = service.allocate(db, "L", fy="26-27")
    assert nxt.number == 2  # 1 stays consumed (gap), never reissued
