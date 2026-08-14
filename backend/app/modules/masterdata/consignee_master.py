"""Consignee-party golden-record service (keyed on GSTIN).

`resolve_or_create` is the upload entry point: a NEW GSTIN auto-creates a record
(no admin verification); an EXISTING GSTIN with differing details returns the
STORED record plus a deviation report — it NEVER silently overwrites. Admins may
also create/edit records directly (`create_party` / `update_party`), and an
accepted deviation is applied via `apply_incoming`.

Every mutation is audited inside the caller's transaction. GSTIN validity reuses
`normalize.valid_gstin` (a golden record must have a well-formed key); text
comparisons reuse `match_key` (case/whitespace-insensitive) and numeric fields
compare digits-only, so cosmetic formatting differences are NOT flagged as
deviations.

⚠️ A `db.begin_nested()` MUST be used as `with db.begin_nested():` — a bare call
leaks a SAVEPOINT per insert and a large batch overflows commit recursion.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.masterdata.models import ConsigneeParty
from app.modules.masterdata.normalize import (
    collapse_ws,
    gstin_matches_state,
    match_key,
    valid_gstin,
)
from app.platform import audit

_VALID_SOURCES: frozenset[str] = frozenset({"MANUAL", "UPLOAD"})


class ConsigneeMasterError(Exception):
    """Raised for invalid consignee-party operations (e.g. malformed GSTIN key)."""


class DuplicateConsigneeGstin(ConsigneeMasterError):
    """The GSTIN already exists (route maps to 409)."""


@dataclass
class Deviation:
    """One field where an incoming value differs from the stored golden record."""

    field: str
    stored: str
    incoming: str


@dataclass
class ResolveResult:
    """Outcome of `resolve_or_create`: the record, whether it was created, and any
    deviations between the incoming payload and an existing stored record."""

    party: ConsigneeParty
    created: bool
    deviations: list[Deviation] = field(default_factory=list)


def normalize_gstin(g: str) -> str:
    """Canonical GSTIN key: strip + upper. The stored/looked-up form."""
    return g.strip().upper()


def _digits(value: str) -> str:
    """Digits-only projection for formatting-insensitive numeric comparison."""
    return "".join(ch for ch in value if ch.isdigit())


def _require_state_match(gstin: str, state: str) -> None:
    """A RECOGNIZED state name must agree with the GSTIN's leading state code, so a
    golden record can't store a self-contradictory (GSTIN, state) pair that would
    later be snapshotted onto a statutory challan. Lenient: an unrecognized /
    abbreviated state name passes (see `gstin_matches_state`)."""
    if state and not gstin_matches_state(gstin, state):
        raise ConsigneeMasterError(
            f"state '{state}' does not match the GSTIN state code {gstin[:2]}"
        )


def resolve_or_create(
    db: Session,
    *,
    gstin: str,
    name: str,
    address_line1: str = "",
    address_line2: str = "",
    pincode: str = "",
    state: str = "",
    phone: str = "",
    source: str = "UPLOAD",
    actor_uid: str | None = None,
    dry_run: bool = False,
) -> ResolveResult:
    """Resolve a consignee party by GSTIN, auto-creating an unknown one.

    * Unknown GSTIN -> create (with `source`), audit `consignee_party.created`,
      return `created=True, deviations=[]`. A concurrent create of the same GSTIN
      trips the UNIQUE key; we re-select the winner and treat it as found.
    * Known GSTIN -> compute deviations (text via `match_key`, pincode/phone via
      digits-only) for each non-empty incoming value that differs from stored, do
      NOT overwrite, return `created=False` + the deviations.

    `dry_run=True` performs the SAME validation + deviation computation but WRITES
    NOTHING (no insert, no audit): an unknown GSTIN returns a transient, unsaved
    `ConsigneeParty` with `created=True`; a known GSTIN returns the stored record +
    deviations. The upload validator uses this so a batch that ultimately FAILS
    never leaves auto-created golden records behind — the real write happens only
    at generation time.

    Raises `ConsigneeMasterError` if the GSTIN is not a well-formed key, or (on the
    create path) if a recognized incoming state disagrees with the GSTIN code.
    """
    gstin = normalize_gstin(gstin)
    if not valid_gstin(gstin):
        raise ConsigneeMasterError("GSTIN is invalid (bad state code or check digit)")
    if source not in _VALID_SOURCES:
        raise ConsigneeMasterError(f"source must be one of {sorted(_VALID_SOURCES)}")

    name = collapse_ws(name)
    address_line1 = collapse_ws(address_line1)
    address_line2 = collapse_ws(address_line2)
    pincode = collapse_ws(pincode)
    state = collapse_ws(state)
    phone = collapse_ws(phone)

    existing = db.execute(
        select(ConsigneeParty).where(ConsigneeParty.gstin == gstin)
    ).scalar_one_or_none()

    if existing is None:
        # Only guard on CREATE: an existing party was validated when created, and a
        # mismatched incoming state on a KNOWN gstin is surfaced as a deviation.
        _require_state_match(gstin, state)
        party = ConsigneeParty(
            gstin=gstin,
            name=name,
            address_line1=address_line1,
            address_line2=address_line2,
            pincode=pincode,
            state=state,
            phone=phone,
            source=source,
            created_by=actor_uid,
            updated_by=actor_uid,
        )
        if dry_run:
            # Transient, never added to the session — returned only so the caller
            # can see created=True. No insert, no audit, no flush.
            return ResolveResult(party=party, created=True, deviations=[])
        try:
            with db.begin_nested():
                db.add(party)
                db.flush()
        except IntegrityError:
            # A racing create grabbed this GSTIN; re-select the winner and treat
            # it as an existing record (fall through to deviation reporting).
            existing = db.execute(
                select(ConsigneeParty).where(ConsigneeParty.gstin == gstin)
            ).scalar_one_or_none()
            if existing is None:
                raise
        else:
            audit.log(
                db,
                action="consignee_party.created",
                actor_uid=actor_uid,
                entity="md_consignee_party",
                entity_id=str(party.id),
                detail={"gstin": gstin, "name": name, "source": source},
            )
            return ResolveResult(party=party, created=True, deviations=[])

    deviations = _compute_deviations(
        existing,
        name=name,
        address_line1=address_line1,
        address_line2=address_line2,
        pincode=pincode,
        state=state,
        phone=phone,
    )
    return ResolveResult(party=existing, created=False, deviations=deviations)


def _compute_deviations(
    party: ConsigneeParty,
    *,
    name: str,
    address_line1: str,
    address_line2: str,
    pincode: str,
    state: str,
    phone: str,
) -> list[Deviation]:
    """Non-empty incoming values that genuinely differ from the stored record.

    Text fields compare via `match_key` (case/whitespace-insensitive); pincode and
    phone compare digits-only. An empty incoming value is never a deviation.
    """
    deviations: list[Deviation] = []
    text_fields = (
        ("name", party.name, name),
        ("address_line1", party.address_line1, address_line1),
        ("address_line2", party.address_line2, address_line2),
        ("state", party.state, state),
    )
    for fname, stored, incoming in text_fields:
        if incoming and match_key(stored) != match_key(incoming):
            deviations.append(Deviation(field=fname, stored=stored, incoming=incoming))

    numeric_fields = (
        ("pincode", party.pincode, pincode),
        ("phone", party.phone, phone),
    )
    for fname, stored, incoming in numeric_fields:
        if incoming and _digits(stored) != _digits(incoming):
            deviations.append(Deviation(field=fname, stored=stored, incoming=incoming))

    return deviations


def apply_incoming(
    db: Session,
    *,
    party: ConsigneeParty,
    name: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    pincode: str | None = None,
    state: str | None = None,
    phone: str | None = None,
    actor_uid: str | None = None,
) -> ConsigneeParty:
    """Update the stored record from provided (non-None) values — used when a user
    or admin ACCEPTS a deviation. Only genuinely-changed fields are written, so a
    no-op edit neither bumps `updated_at` nor writes an audit row. Audited only on a
    real change (`consignee_party.updated`).

    A provided `state` is held to the same GSTIN↔state consistency as create, so an
    edit (or an accepted state-deviation) can never leave a self-contradictory
    (GSTIN, state) pair to be snapshotted onto a statutory challan (e.g. GSTIN code
    27/Maharashtra with state 'Gujarat')."""
    if state is not None:
        _require_state_match(party.gstin, collapse_ws(state))
    changed: dict[str, str] = {}
    for fname, incoming in (
        ("name", name),
        ("address_line1", address_line1),
        ("address_line2", address_line2),
        ("pincode", pincode),
        ("state", state),
        ("phone", phone),
    ):
        if incoming is None:
            continue  # field omitted — leave stored value untouched
        new = collapse_ws(incoming)
        if getattr(party, fname) != new:
            setattr(party, fname, new)
            changed[fname] = new
    if not changed:
        return party  # no-op: don't bump updated_at, reorder the list, or audit
    party.updated_by = actor_uid
    db.flush()
    audit.log(
        db,
        action="consignee_party.updated",
        actor_uid=actor_uid,
        entity="md_consignee_party",
        entity_id=str(party.id),
        detail={"gstin": party.gstin, "changed": changed},
    )
    return party


def create_party(
    db: Session,
    *,
    gstin: str,
    name: str,
    address_line1: str = "",
    address_line2: str = "",
    pincode: str = "",
    state: str = "",
    phone: str = "",
    actor_uid: str | None = None,
) -> ConsigneeParty:
    """Manual admin create (source=MANUAL). Validates the GSTIN key; a duplicate
    GSTIN raises `DuplicateConsigneeGstin`. Audited `consignee_party.created`."""
    gstin = normalize_gstin(gstin)
    if not valid_gstin(gstin):
        raise ConsigneeMasterError("GSTIN is invalid (bad state code or check digit)")

    name = collapse_ws(name)
    if not name:
        raise ConsigneeMasterError("name is required")
    _require_state_match(gstin, collapse_ws(state))

    if db.execute(
        select(ConsigneeParty).where(ConsigneeParty.gstin == gstin)
    ).scalar_one_or_none() is not None:
        raise DuplicateConsigneeGstin(f"GSTIN {gstin} already exists")

    party = ConsigneeParty(
        gstin=gstin,
        name=name,
        address_line1=collapse_ws(address_line1),
        address_line2=collapse_ws(address_line2),
        pincode=collapse_ws(pincode),
        state=collapse_ws(state),
        phone=collapse_ws(phone),
        source="MANUAL",
        created_by=actor_uid,
        updated_by=actor_uid,
    )
    try:
        with db.begin_nested():
            db.add(party)
            db.flush()
    except IntegrityError as exc:
        raise DuplicateConsigneeGstin(f"GSTIN {gstin} already exists") from exc

    audit.log(
        db,
        action="consignee_party.created",
        actor_uid=actor_uid,
        entity="md_consignee_party",
        entity_id=str(party.id),
        detail={"gstin": gstin, "name": name, "source": "MANUAL"},
    )
    return party


def update_party(
    db: Session,
    *,
    party: ConsigneeParty,
    name: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    pincode: str | None = None,
    state: str | None = None,
    phone: str | None = None,
    actor_uid: str | None = None,
) -> ConsigneeParty:
    """Admin edit of the golden record (GSTIN is immutable). Audited."""
    return apply_incoming(
        db,
        party=party,
        name=name,
        address_line1=address_line1,
        address_line2=address_line2,
        pincode=pincode,
        state=state,
        phone=phone,
        actor_uid=actor_uid,
    )
