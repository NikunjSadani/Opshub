"""Projects service — client registration + per-client sequential project ids.

Integrity discipline (borrowed from the numbering engine, NOT its statutory
counter):

  * A project id is `<CLIENT_CODE>-<seq>`, where `seq` is the next per-client
    running number. Allocation LOCKS the parent client row (`with_for_update`)
    so concurrent creates under one client serialize and read a consistent
    `max(seq)`.
  * Two unique constraints are the DB backstop that holds even if the lock is
    ineffective (sqlite, a logic bug, a race): `project.code` globally and
    `(client_id, seq)` per client. On a collision we retry once against the
    freshly-observed ceiling.
  * Every mutation is audited inside the caller's transaction, so the trail
    commits atomically with the change.

⚠️ A `db.begin_nested()` MUST be used as `with db.begin_nested():` — a bare call
leaks a SAVEPOINT per insert and a large batch overflows commit recursion.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.projects.models import Project, ProjectClient, ProjectStatus
from app.platform import audit

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_MAX_NAME_LEN = 200
# The parent-client row-lock is the real serializer for per-client seq allocation;
# this retry is only a thin backstop for a rare lost race. Correctness assumes the
# DB runs at READ COMMITTED (Postgres default): each retry issues a FRESH SELECT for
# the ceiling, so it sees a committed racer's row. (Under REPEATABLE READ the retry
# would re-read the same snapshot and fail cleanly — never mint a duplicate.)
_MAX_CONTENTION_RETRIES = 5
_STATUSES: frozenset[str] = frozenset(s.value for s in ProjectStatus)


class ProjectError(Exception):
    """Raised for invalid projects operations (bad code/status, etc.)."""


class ProjectClientNotFound(ProjectError):
    """The referenced client is missing or inactive (route maps to 404)."""


class DuplicateClientCode(ProjectError):
    """The client code already exists, case-insensitively (route maps to 409)."""


class ProjectIdContention(ProjectError):
    """Transient: a per-client project id couldn't be allocated after retries.
    The route maps this to 409 — the caller should simply retry."""


def _clean_name(name: str) -> str:
    """Strip ends + collapse internal whitespace; reject over the column cap so a
    direct service caller can't exceed what the DB column stores."""
    cleaned = " ".join(name.split())
    if len(cleaned) > _MAX_NAME_LEN:
        raise ProjectError(f"name must be at most {_MAX_NAME_LEN} characters")
    return cleaned


def create_client(
    db: Session, *, name: str, code: str, actor_uid: str | None = None
) -> ProjectClient:
    """Register a client with a unique 3-letter uppercase code.

    `code` is normalized (strip + upper) then validated `^[A-Z]{3}$`; a bad shape
    raises `ProjectError`. Because the code is upper-cased before insert, 'bri'
    and 'BRI' collapse to one key, so the UNIQUE index rejects case-variant dups —
    surfaced as `DuplicateClientCode` (the route maps it to 409). Audited.
    """
    code = code.strip().upper()
    name = _clean_name(name)
    if not _CODE_RE.match(code):
        raise ProjectError("code must be exactly 3 letters A-Z")
    if not name:
        raise ProjectError("name is required")

    if (
        db.execute(select(ProjectClient).where(ProjectClient.code == code)).scalar_one_or_none()
        is not None
    ):
        raise DuplicateClientCode(f"client code {code} already exists")

    client = ProjectClient(name=name, code=code, created_by=actor_uid)
    try:
        # `with` releases the savepoint on success (no leak); a concurrent create
        # of the same code trips the UNIQUE index at flush and lands here.
        with db.begin_nested():
            db.add(client)
            db.flush()
    except IntegrityError as exc:
        raise DuplicateClientCode(f"client code {code} already exists") from exc

    audit.log(
        db,
        action="project.client_created",
        actor_uid=actor_uid,
        entity="project_client",
        entity_id=str(client.id),
        detail={"code": code, "name": name},
    )
    return client


def _max_seq(db: Session, client_id: int) -> int:
    """Highest `seq` currently allocated under `client_id`, or 0 if none."""
    result: int | None = db.execute(
        select(func.max(Project.seq)).where(Project.client_id == client_id)
    ).scalar()
    return result or 0


def create_project(
    db: Session,
    *,
    client_id: int,
    name: str,
    start_date: date | None = None,
    description: str | None = None,
    actor_uid: str | None = None,
) -> Project:
    """Create a project under `client_id`, minting `<CLIENT_CODE>-<seq:03d>`.

    Loads the client under a row-lock (`with_for_update`) so concurrent creates
    under one client serialize, then computes `seq = max(existing seq) + 1`. The
    UNIQUE on `project.code` and `(client_id, seq)` is the backstop; on a race the
    insert is retried once against the re-read ceiling. Audited. Returns the row.

    Idempotent on (client_id, name): a double-submit / network retry returns the
    already-created project instead of minting a second one (which would burn a
    sequence number). Distinct names still create distinct projects.
    """
    name = _clean_name(name)
    if not name:
        raise ProjectError("name is required")

    last_err: IntegrityError | None = None
    for _attempt in range(_MAX_CONTENTION_RETRIES):
        client = db.execute(
            select(ProjectClient).where(ProjectClient.id == client_id).with_for_update()
        ).scalar_one_or_none()
        if client is None or not client.active:
            raise ProjectClientNotFound(f"client {client_id} not found or inactive")

        # Idempotency guard: a double-submit / retry with an identical
        # (client_id, name) must NOT mint a second project and burn a sequence
        # number. Return the existing row instead. Checked UNDER the client
        # row-lock so a concurrent retry serializes behind the first commit and
        # still dedupes (not just the sequential-retry case). Distinct names
        # still mint a fresh project as normal.
        existing = db.execute(
            select(Project)
            .where(Project.client_id == client_id, Project.name == name)
            .order_by(Project.seq.desc())
        ).scalars().first()
        if existing is not None:
            return existing

        seq = _max_seq(db, client_id) + 1
        code = f"{client.code}-{seq:03d}"
        project = Project(
            client_id=client_id,
            seq=seq,
            code=code,
            name=name,
            start_date=start_date,
            description=description,
            status=ProjectStatus.ACTIVE.value,
            created_by=actor_uid,
        )
        try:
            with db.begin_nested():
                db.add(project)
                db.flush()  # trips the unique backstop if code/(client,seq) collide
        except IntegrityError as err:
            last_err = err
            continue  # a racing writer took our seq — re-read the ceiling and retry
        audit.log(
            db,
            action="project.created",
            actor_uid=actor_uid,
            entity="project",
            entity_id=code,
            detail={"client_id": client_id, "seq": seq, "code": code, "name": name},
        )
        return project
    # Transient contention: log the raw error server-side, surface a generic
    # retryable message (route -> 409) rather than leaking DB/constraint internals.
    logger.warning("project id contention for client %s: %s", client_id, last_err)
    raise ProjectIdContention("could not allocate a project id — please retry")


def resolve_active_project(db: Session, code: str) -> Project | None:
    """Return the ACTIVE project whose human id matches `code`, else None.

    Used by the challan generator to enforce "Project ID must pre-exist + be
    Active". `code` is normalized (strip + upper) to match stored `<CODE>-<seq>`.
    A missing project, or one that is ON_HOLD/CLOSED, resolves to None so the
    caller can raise a blocking validation error.
    """
    normalized = code.strip().upper()
    if not normalized:
        return None
    return db.execute(
        select(Project).where(
            Project.code == normalized,
            Project.status == ProjectStatus.ACTIVE.value,
        )
    ).scalar_one_or_none()


def set_status(
    db: Session, *, project: Project, status: str, actor_uid: str | None = None
) -> Project:
    """Change a project's status to one of ACTIVE / ON_HOLD / CLOSED. Audited."""
    if status not in _STATUSES:
        raise ProjectError(f"status must be one of {sorted(_STATUSES)}")
    project.status = status
    db.flush()
    audit.log(
        db,
        action="project.status_changed",
        actor_uid=actor_uid,
        entity="project",
        entity_id=project.code,
        detail={"status": status},
    )
    return project
