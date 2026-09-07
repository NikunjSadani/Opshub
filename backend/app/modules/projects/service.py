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
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.projects.models import (
    ClientAddress,
    ClientContact,
    ClientGstin,
    Project,
    ProjectClient,
    ProjectStatus,
)
from app.platform import audit

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^[A-Z]{3}$")
_MAX_NAME_LEN = 200
# Access PIN is the readable shared secret behind the challan-QR password. Because the
# challan number (the other half of the password) is PUBLIC — printed on the challan and
# echoed on the viewer form — the PIN is effectively the ONLY secret, so a short/numeric PIN
# is brute-forceable by anyone who holds a challan (DUAL security audit HIGH). Floor is 8 (was
# 4); prefer a system-generated alphanumeric PIN. Column is String(32).
_MIN_PIN_LEN = 8
_MAX_PIN_LEN = 32
# PAN is a 10-char org identifier (column is String(10)). We normalize + cap here so
# a too-long value raises a clean 422 instead of being truncated / 500ing at the DB.
_MAX_PAN_LEN = 10
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


class InvalidGstin(ProjectError):
    """A GSTIN is malformed (not 15 alphanumeric chars). Route maps to 422."""


class DuplicateClientGstin(ProjectError):
    """The GSTIN already exists (active or not) for this client. Route maps to 409."""


class ClientChildNotFound(ProjectError):
    """A referenced gstin/address/contact row is missing. Route maps to 404."""


class _Unset:
    """Marker for 'argument not supplied' in a partial (PATCH) update, so we can
    tell "leave unchanged" apart from "set to NULL" for a nullable column."""


# A single shared instance; `Final` so mypy treats it as a constant sentinel.
_UNSET: Final = _Unset()

_GSTIN_RE = re.compile(r"^[0-9A-Z]{15}$")


def _normalize_gstin(raw: str) -> str:
    """Strip + upper a GSTIN and enforce the 15-char alphanumeric shape.

    A malformed value raises `InvalidGstin` (route -> 422) BEFORE any DB write, so
    the uniqueness/default machinery only ever sees well-formed identifiers.
    """
    gstin = raw.strip().upper()
    if not _GSTIN_RE.match(gstin):
        raise InvalidGstin("gstin must be exactly 15 alphanumeric characters")
    return gstin


def _opt(value: str | None) -> str | None:
    """Trim an optional free-text field; an empty/whitespace value collapses to
    None so blank input never persists as an empty string."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _clean_name(name: str) -> str:
    """Strip ends + collapse internal whitespace; reject over the column cap so a
    direct service caller can't exceed what the DB column stores."""
    cleaned = " ".join(name.split())
    if len(cleaned) > _MAX_NAME_LEN:
        raise ProjectError(f"name must be at most {_MAX_NAME_LEN} characters")
    return cleaned


def _clean_pan(pan: str | None) -> str | None:
    """Normalize an optional PAN: strip + upper, blank -> None. A value longer than the
    column (10) raises `ProjectError` (route -> 422) rather than being silently
    truncated or 500ing at the DB. Shared by create + update so both validate identically."""
    if pan is None:
        return None
    cleaned = pan.strip().upper()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_PAN_LEN:
        raise ProjectError(f"pan must be at most {_MAX_PAN_LEN} characters")
    return cleaned


def _validate_credit_terms(days: int | None) -> None:
    """A credit-terms value, when supplied, must be a non-negative number of days."""
    if days is not None and days < 0:
        raise ProjectError("credit_terms_days must be >= 0")


def create_client(
    db: Session,
    *,
    name: str,
    code: str,
    pan: str | None = None,
    credit_terms_days: int | None = None,
    actor_uid: str | None = None,
) -> ProjectClient:
    """Register a client with a unique 3-letter uppercase code.

    `code` is normalized (strip + upper) then validated `^[A-Z]{3}$`; a bad shape
    raises `ProjectError`. Because the code is upper-cased before insert, 'bri'
    and 'BRI' collapse to one key, so the UNIQUE index rejects case-variant dups —
    surfaced as `DuplicateClientCode` (the route maps it to 409).

    Optional master fields `pan` (normalized strip+upper, capped at 10) and
    `credit_terms_days` (>= 0) may be captured at registration; both are otherwise
    editable later via `update_client`. Validated identically to the edit path. Audited.
    """
    code = code.strip().upper()
    name = _clean_name(name)
    if not _CODE_RE.match(code):
        raise ProjectError("code must be exactly 3 letters A-Z")
    if not name:
        raise ProjectError("name is required")
    pan_clean = _clean_pan(pan)
    _validate_credit_terms(credit_terms_days)

    if (
        db.execute(select(ProjectClient).where(ProjectClient.code == code)).scalar_one_or_none()
        is not None
    ):
        raise DuplicateClientCode(f"client code {code} already exists")

    client = ProjectClient(
        name=name,
        code=code,
        pan=pan_clean,
        credit_terms_days=credit_terms_days,
        created_by=actor_uid,
    )
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
        detail={
            "code": code,
            "name": name,
            "pan": pan_clean,
            "credit_terms_days": credit_terms_days,
        },
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


def get_active_project(db: Session, project_id: int) -> Project | None:
    """Return the ACTIVE project with numeric id `project_id`, else None.

    Mirrors ``resolve_active_project`` (which keys on the human ``code``) but keys on
    the primary key — used by the expense upload to enforce "the cost-allocation
    project must pre-exist AND be Active". An unknown project, or one that is
    ON_HOLD/CLOSED, resolves to None so the caller can raise a blocking 400.
    """
    return db.execute(
        select(Project).where(
            Project.id == project_id,
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


def update_project(
    db: Session,
    *,
    project: Project,
    name: str | _Unset = _UNSET,
    start_date: date | None | _Unset = _UNSET,
    description: str | None | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> Project:
    """Patch a project's editable typed details so a user can correct a mistake
    (e.g. a typo in the name). Only supplied args change; a nullable field can be
    explicitly cleared by passing `None` (distinct from the `_UNSET` "leave
    unchanged" default). Validates + trims exactly like the create path (`_clean_name`
    / `_opt`). Audited.

    Deliberately NARROW: `code`, `client_id` and `seq` are system-assigned identity
    (the code is globally unique and derived from the client) and are NOT editable
    here — those are not correctable details. Status has its own path (`set_status`).
    `name` cannot be cleared (the column is NOT NULL); passing `None` is a 422.
    """
    changed: dict[str, object | None] = {}
    if not isinstance(name, _Unset):
        if name is None:
            raise ProjectError("name is required")
        cleaned = _clean_name(name)
        if not cleaned:
            raise ProjectError("name is required")
        project.name = cleaned
        changed["name"] = cleaned
    if not isinstance(start_date, _Unset):
        project.start_date = start_date
        changed["start_date"] = start_date.isoformat() if start_date else None
    if not isinstance(description, _Unset):
        project.description = _opt(description)
        changed["description"] = project.description

    db.flush()
    audit.log(
        db,
        action="project.updated",
        actor_uid=actor_uid,
        entity="project",
        entity_id=project.code,
        detail={"changed": changed},
    )
    return project


# --------------------------------------------------------------- client master
#
# The promoted `project_client` carries child GSTINs / addresses / contacts. All
# edits are gated by `client.manage` at the route; the service enforces the data
# invariants: GSTIN normalization + uniqueness, "at most one default per client
# per child type", and soft-delete (never a hard DELETE).


def get_client(db: Session, client_id: int) -> ProjectClient | None:
    """Return the client row by id (active or not), or None. Used by detail + as
    the parent lookup for every child mutation."""
    return db.execute(
        select(ProjectClient).where(ProjectClient.id == client_id)
    ).scalar_one_or_none()


def update_client(
    db: Session,
    *,
    client: ProjectClient,
    name: str | _Unset = _UNSET,
    pan: str | None | _Unset = _UNSET,
    credit_terms_days: int | None | _Unset = _UNSET,
    active: bool | _Unset = _UNSET,
    access_pin: str | None | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> ProjectClient:
    """Patch a client's editable master fields. Only supplied args change; a
    nullable field can be explicitly cleared by passing `None` (distinct from the
    `_UNSET` "leave unchanged" default). PAN is normalized (strip+upper). Audited.

    `access_pin` is the challan-QR shared secret: an empty/blank/`None` value CLEARS
    it; a set value must be 4-32 chars. The audit trail records that the pin was
    set/cleared — never the value itself (it is a readable shared secret).
    """
    changed: dict[str, object | None] = {}
    if not isinstance(name, _Unset):
        cleaned = _clean_name(name)
        if not cleaned:
            raise ProjectError("name is required")
        client.name = cleaned
        changed["name"] = cleaned
    if not isinstance(pan, _Unset):
        client.pan = _clean_pan(pan)
        changed["pan"] = client.pan
    if not isinstance(credit_terms_days, _Unset):
        _validate_credit_terms(credit_terms_days)
        client.credit_terms_days = credit_terms_days
        changed["credit_terms_days"] = credit_terms_days
    if not isinstance(active, _Unset):
        client.active = active
        changed["active"] = active
    if not isinstance(access_pin, _Unset):
        pin = access_pin.strip() if access_pin else ""
        if not pin:  # empty/blank/None -> clear the shared secret
            client.access_pin = None
            changed["access_pin"] = "cleared"
        else:
            if not (_MIN_PIN_LEN <= len(pin) <= _MAX_PIN_LEN):
                raise ProjectError(
                    f"access_pin must be {_MIN_PIN_LEN}-{_MAX_PIN_LEN} characters"
                )
            client.access_pin = pin
            # Never audit the value itself — it's a readable shared secret.
            changed["access_pin"] = "set"

    db.flush()
    audit.log(
        db,
        action="client.updated",
        actor_uid=actor_uid,
        entity="project_client",
        entity_id=str(client.id),
        detail={"changed": changed},
    )
    return client


def _unset_sibling_defaults(
    db: Session,
    model: type[ClientGstin] | type[ClientAddress] | type[ClientContact],
    *,
    client_id: int,
    keep_id: int | None = None,
) -> None:
    """Clear `is_default` on the client's existing default row of this type, so a NEW
    default can be written without tripping the partial unique index `WHERE is_default`.

    Call this BEFORE flushing the new/updated default (demote-first): the index only ever
    permits one default per client, so insert-then-demote would collide. `keep_id` excludes
    the row being promoted (an update); omit it on an add (nothing to keep yet). Flushed here
    so the demote is materialised before the caller writes the new default."""
    stmt = update(model).where(model.client_id == client_id, model.is_default.is_(True))
    if keep_id is not None:
        stmt = stmt.where(model.id != keep_id)
    db.execute(stmt.values(is_default=False).execution_options(synchronize_session=False))
    db.flush()


# ------------------------------------------------------------------ gstins


def add_gstin(
    db: Session,
    *,
    client: ProjectClient,
    gstin: str,
    legal_name: str | None = None,
    state_code: str | None = None,
    is_default: bool = False,
    actor_uid: str | None = None,
) -> ClientGstin:
    """Attach a GSTIN to a client. Normalizes + validates the 15-char shape, derives
    `state_code` from the first two digits when not supplied, rejects a duplicate
    `(client, gstin)` (`DuplicateClientGstin` -> 409), and enforces one default.
    Audited.
    """
    normalized = _normalize_gstin(gstin)
    derived_state = _opt(state_code) or normalized[:2]
    if is_default:  # demote-first so the partial unique index never sees two defaults
        _unset_sibling_defaults(db, ClientGstin, client_id=client.id)
    row = ClientGstin(
        client_id=client.id,
        gstin=normalized,
        legal_name=_opt(legal_name),
        state_code=derived_state.upper(),
        is_default=is_default,
        created_by=actor_uid,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        raise DuplicateClientGstin(
            f"gstin {normalized} already exists for this client"
        ) from exc
    audit.log(
        db,
        action="client.gstin_added",
        actor_uid=actor_uid,
        entity="client_gstin",
        entity_id=str(row.id),
        detail={"client_id": client.id, "gstin": normalized, "is_default": is_default},
    )
    return row


def get_gstin(db: Session, gstin_id: int) -> ClientGstin | None:
    return db.execute(
        select(ClientGstin).where(ClientGstin.id == gstin_id)
    ).scalar_one_or_none()


def update_gstin(
    db: Session,
    *,
    row: ClientGstin,
    gstin: str | _Unset = _UNSET,
    legal_name: str | None | _Unset = _UNSET,
    state_code: str | None | _Unset = _UNSET,
    is_default: bool | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> ClientGstin:
    """Patch a GSTIN row. A changed gstin value is re-normalized + re-checked for
    uniqueness; setting `is_default=True` demotes the client's other GSTINs. Audited."""
    # Demote-first if promoting to default, BEFORE mutating fields — so the helper's flush
    # doesn't early-flush a pending gstin change outside the dup-check savepoint.
    if not isinstance(is_default, _Unset) and is_default:
        _unset_sibling_defaults(db, ClientGstin, client_id=row.client_id, keep_id=row.id)
    if not isinstance(gstin, _Unset):
        row.gstin = _normalize_gstin(gstin)
    if not isinstance(legal_name, _Unset):
        row.legal_name = _opt(legal_name)
    if not isinstance(state_code, _Unset):
        cleaned_state = _opt(state_code)
        row.state_code = cleaned_state.upper() if cleaned_state else None
    if not isinstance(is_default, _Unset):
        row.is_default = is_default
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise DuplicateClientGstin(
            f"gstin {row.gstin} already exists for this client"
        ) from exc
    audit.log(
        db,
        action="client.gstin_updated",
        actor_uid=actor_uid,
        entity="client_gstin",
        entity_id=str(row.id),
        detail={"client_id": row.client_id, "is_default": row.is_default},
    )
    return row


def deactivate_gstin(
    db: Session, *, row: ClientGstin, actor_uid: str | None = None
) -> ClientGstin:
    """Soft-delete a GSTIN (`active=False`); never a hard delete. Audited."""
    row.active = False
    row.is_default = False  # a retired GSTIN must not stay the default
    db.flush()
    audit.log(
        db,
        action="client.gstin_deactivated",
        actor_uid=actor_uid,
        entity="client_gstin",
        entity_id=str(row.id),
        detail={"client_id": row.client_id},
    )
    return row


# ---------------------------------------------------------------- addresses


def add_address(
    db: Session,
    *,
    client: ProjectClient,
    line1: str,
    gstin_id: int | None = None,
    label: str | None = None,
    line2: str | None = None,
    city: str | None = None,
    state: str | None = None,
    pincode: str | None = None,
    is_default: bool = False,
    actor_uid: str | None = None,
) -> ClientAddress:
    """Attach a billing/shipping address. An optional `gstin_id` must belong to the
    same client (else `ClientChildNotFound`). Enforces one default. Audited."""
    cleaned_line1 = _opt(line1)
    if not cleaned_line1:
        raise ProjectError("line1 is required")
    if gstin_id is not None:
        linked = get_gstin(db, gstin_id)
        if linked is None or linked.client_id != client.id or not linked.active:
            raise ClientChildNotFound(f"gstin {gstin_id} not found (or inactive) for this client")
    if is_default:  # demote-first so the partial unique index never sees two defaults
        _unset_sibling_defaults(db, ClientAddress, client_id=client.id)
    row = ClientAddress(
        client_id=client.id,
        gstin_id=gstin_id,
        label=_opt(label),
        line1=cleaned_line1,
        line2=_opt(line2),
        city=_opt(city),
        state=_opt(state),
        pincode=_opt(pincode),
        is_default=is_default,
        created_by=actor_uid,
    )
    db.add(row)
    db.flush()
    audit.log(
        db,
        action="client.address_added",
        actor_uid=actor_uid,
        entity="client_address",
        entity_id=str(row.id),
        detail={"client_id": client.id, "is_default": is_default},
    )
    return row


def get_address(db: Session, address_id: int) -> ClientAddress | None:
    return db.execute(
        select(ClientAddress).where(ClientAddress.id == address_id)
    ).scalar_one_or_none()


def update_address(
    db: Session,
    *,
    row: ClientAddress,
    gstin_id: int | None | _Unset = _UNSET,
    label: str | None | _Unset = _UNSET,
    line1: str | _Unset = _UNSET,
    line2: str | None | _Unset = _UNSET,
    city: str | None | _Unset = _UNSET,
    state: str | None | _Unset = _UNSET,
    pincode: str | None | _Unset = _UNSET,
    is_default: bool | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> ClientAddress:
    """Patch an address row; setting `is_default=True` demotes the client's others.
    A changed `gstin_id` is re-validated against the same client. Audited."""
    if not isinstance(gstin_id, _Unset):
        if gstin_id is not None:
            linked = get_gstin(db, gstin_id)
            if linked is None or linked.client_id != row.client_id or not linked.active:
                raise ClientChildNotFound(
                    f"gstin {gstin_id} not found (or inactive) for this client")
        row.gstin_id = gstin_id
    if not isinstance(label, _Unset):
        row.label = _opt(label)
    if not isinstance(line1, _Unset):
        cleaned = _opt(line1)
        if not cleaned:
            raise ProjectError("line1 is required")
        row.line1 = cleaned
    if not isinstance(line2, _Unset):
        row.line2 = _opt(line2)
    if not isinstance(city, _Unset):
        row.city = _opt(city)
    if not isinstance(state, _Unset):
        row.state = _opt(state)
    if not isinstance(pincode, _Unset):
        row.pincode = _opt(pincode)
    if not isinstance(is_default, _Unset):
        if is_default:  # demote-first
            _unset_sibling_defaults(db, ClientAddress, client_id=row.client_id, keep_id=row.id)
        row.is_default = is_default
    db.flush()
    audit.log(
        db,
        action="client.address_updated",
        actor_uid=actor_uid,
        entity="client_address",
        entity_id=str(row.id),
        detail={"client_id": row.client_id, "is_default": row.is_default},
    )
    return row


def deactivate_address(
    db: Session, *, row: ClientAddress, actor_uid: str | None = None
) -> ClientAddress:
    """Soft-delete an address (`active=False`); never a hard delete. Audited."""
    row.active = False
    row.is_default = False
    db.flush()
    audit.log(
        db,
        action="client.address_deactivated",
        actor_uid=actor_uid,
        entity="client_address",
        entity_id=str(row.id),
        detail={"client_id": row.client_id},
    )
    return row


# ----------------------------------------------------------------- contacts


def add_contact(
    db: Session,
    *,
    client: ProjectClient,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    designation: str | None = None,
    is_default: bool = False,
    actor_uid: str | None = None,
) -> ClientContact:
    """Attach a point-of-contact. Enforces one default per client. Audited."""
    cleaned_name = _opt(name)
    if not cleaned_name:
        raise ProjectError("name is required")
    if is_default:  # demote-first so the partial unique index never sees two defaults
        _unset_sibling_defaults(db, ClientContact, client_id=client.id)
    row = ClientContact(
        client_id=client.id,
        name=cleaned_name,
        email=_opt(email),
        phone=_opt(phone),
        designation=_opt(designation),
        is_default=is_default,
        created_by=actor_uid,
    )
    db.add(row)
    db.flush()
    audit.log(
        db,
        action="client.contact_added",
        actor_uid=actor_uid,
        entity="client_contact",
        entity_id=str(row.id),
        detail={"client_id": client.id, "is_default": is_default},
    )
    return row


def get_contact(db: Session, contact_id: int) -> ClientContact | None:
    return db.execute(
        select(ClientContact).where(ClientContact.id == contact_id)
    ).scalar_one_or_none()


def update_contact(
    db: Session,
    *,
    row: ClientContact,
    name: str | _Unset = _UNSET,
    email: str | None | _Unset = _UNSET,
    phone: str | None | _Unset = _UNSET,
    designation: str | None | _Unset = _UNSET,
    is_default: bool | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> ClientContact:
    """Patch a contact row; setting `is_default=True` demotes the client's others.
    Audited."""
    if not isinstance(name, _Unset):
        cleaned = _opt(name)
        if not cleaned:
            raise ProjectError("name is required")
        row.name = cleaned
    if not isinstance(email, _Unset):
        row.email = _opt(email)
    if not isinstance(phone, _Unset):
        row.phone = _opt(phone)
    if not isinstance(designation, _Unset):
        row.designation = _opt(designation)
    if not isinstance(is_default, _Unset):
        if is_default:  # demote-first
            _unset_sibling_defaults(db, ClientContact, client_id=row.client_id, keep_id=row.id)
        row.is_default = is_default
    db.flush()
    audit.log(
        db,
        action="client.contact_updated",
        actor_uid=actor_uid,
        entity="client_contact",
        entity_id=str(row.id),
        detail={"client_id": row.client_id, "is_default": row.is_default},
    )
    return row


def deactivate_contact(
    db: Session, *, row: ClientContact, actor_uid: str | None = None
) -> ClientContact:
    """Soft-delete a contact (`active=False`); never a hard delete. Audited."""
    row.active = False
    row.is_default = False
    db.flush()
    audit.log(
        db,
        action="client.contact_deactivated",
        actor_uid=actor_uid,
        entity="client_contact",
        entity_id=str(row.id),
        detail={"client_id": row.client_id},
    )
    return row
