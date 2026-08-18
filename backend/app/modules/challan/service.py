"""Challan orchestration — validate, then reserve -> render -> issue -> package.

The transaction discipline here is what CLOSES the numbering engine's lock-window
constraint: numbers for the whole batch are reserved and **committed in one short
transaction BEFORE any PDF is rendered**, so the counter row-lock is never held
across document generation. Rendering + issuing then run with no numbering lock.

  validate(db, rows)                 -> ValidationResult (errors + warnings + challans)
  generate(db, batch, renderer, ...) -> reserve(commit) -> render -> issue -> zip/merge

Increment 15: the consignee is TYPED INLINE and resolved against the GSTIN-keyed
golden record (`masterdata.consignee_master`) — an unknown GSTIN auto-creates a
party AT GENERATION TIME (never during validation of a batch that might fail), and
a known GSTIN with differing details yields a non-blocking DEVIATION WARNING while
the STORED golden record (never the typed value) is snapshotted. Every challan
references an ACTIVE Project (printed). Two different `challan_group`s that ship to
the same consignee + destination + date are a non-blocking WARNING (possible split).

Consignor + consignee + project are re-resolved and SNAPSHOTTED onto each challan,
so later edits never rewrite an issued document. Untrusted cell values are escaped
by the renderer (bind-as-data).
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.modules.challan import parsing, render, review_report
from app.modules.challan.models import (
    BatchStatus,
    Challan,
    ChallanBatch,
    ChallanBatchDecision,
    ChallanLineItem,
    ChallanStatus,
    DecisionChoice,
)
from app.modules.challan.schema import (
    COLLISION_FIELDS,
    ChallanView,
    ConsigneeView,
    ConsignorView,
    Contradiction,
    LineView,
    ParsedChallan,
    ParsedLine,
    RawRow,
    RowError,
    ShipToView,
    ValidationResult,
)
from app.modules.files.models import StoredFile
from app.modules.masterdata import consignee_master
from app.modules.masterdata.consignee_master import ConsigneeMasterError
from app.modules.masterdata.models import ConsigneeParty, Consignor, HsnCode
from app.modules.masterdata.normalize import gstin_matches_state, match_key
from app.modules.numbering import service as numbering
from app.modules.numbering.models import AllocationStatus, NumberingAllocation
from app.modules.numbering.service import IST
from app.modules.projects import service as projects
from app.modules.projects.models import Project
from app.platform import audit
from app.platform.models import Setting
from app.platform.storage import Storage, get_storage

logger = logging.getLogger(__name__)

MODULE_KEY = "document_automation"
# Per-batch caps. MAX_BATCH_CHALLANS is the meaningful seatbelt (one upload can't
# burn an unbounded slice of the never-reused statutory sequence). MAX_BATCH_ROWS is
# only a high pathological backstop (a "one challan, a million lines" file) sized so
# the challan cap always binds first — a 2000-challan batch of multi-line challans is
# never falsely rejected by a row limit.
MAX_BATCH_ROWS = 50000
MAX_BATCH_CHALLANS = 2000
# Postgres int8 (BigInteger) ceiling. Per-CELL money is capped in parsing
# (_MAX_MONEY_PAISE), but a group's SUM of line amounts (Challan.total_paise) is
# otherwise unbounded — enough near-max lines overflow the int8 column and raise at
# INSERT, FAILING the batch and voiding numbers on an input the validator accepted.
# So we reject an overflowing group total at VALIDATION, before any number is reserved.
_MAX_INT8 = 2**63 - 1  # 9,223,372,036,854,775,807
_AMOUNT_TOL_CAP_PAISE = 50000  # Rs 500 — absolute ceiling on the amount-sanity band
# A GENERATING batch that hasn't advanced its progress heartbeat (`updated_at`) for
# this long looks genuinely stuck (its worker died) and may be recovered. Set well
# above the time to render a SINGLE challan, so a live-but-slow render is never
# mistaken for a dead worker.
_STUCK_AFTER = timedelta(minutes=2)


def _as_utc(dt: datetime | None) -> datetime:
    """Coerce a stored datetime to tz-aware UTC (SQLite returns naive). A missing
    timestamp is treated as the epoch so a never-stamped row reads as 'long idle'."""
    if dt is None:
        return datetime.fromtimestamp(0, tz=UTC)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class ChallanError(Exception):
    """Raised for unrecoverable generation problems (e.g. unconfigured series)."""


# ------------------------------------------------------------------ validation

def validate(
    db: Session,
    rows: list[RawRow],
    *,
    skip_error_groups: frozenset[str] = frozenset(),
    exclude_batch_id: int | None = None,
) -> ValidationResult:
    """Structural checks (no DB) + semantic checks (master data / projects /
    consignee golden record), then group.

    Returns every blocking ERROR (for a complete English report) plus non-blocking
    WARNINGS (consignee deviations, possible splits), or — when there are no errors
    — the parsed challans grouped by `challan_group`. No golden records are written
    here (consignee resolution runs `dry_run`); the real write is at generation.

    `skip_error_groups` (a RESUME concern) drops errors on groups that are already
    fully issued: on a retry of a partially-generated batch, a master-data change to
    an ALREADY-DONE group must not block finishing the rest. Errors on batch-level
    rows (row 0) and on the remaining (not-yet-issued) groups are kept; if every
    group is already issued, all errors drop (nothing left to generate, just repackage).
    """
    if len(rows) > MAX_BATCH_ROWS:
        return ValidationResult(errors=[RowError(
            0, "", f"batch has {len(rows)} rows; the per-batch limit is {MAX_BATCH_ROWS}")])
    groups = {r.cells.get("challan_group", "") for r in rows} - {""}
    if len(groups) > MAX_BATCH_CHALLANS:
        return ValidationResult(errors=[RowError(
            0, "", f"batch has {len(groups)} challans; the limit is {MAX_BATCH_CHALLANS}")])
    errors: list[RowError] = list(parsing.structural_row_errors(rows))
    warnings: list[RowError] = []
    bad = {e.row_number for e in errors}

    if _active_consignor(db) is None:
        errors.append(RowError(
            0, "", "exactly one active consignor must be configured in master data"))

    projects_cache: dict[str, Project | None] = {}
    hsns: dict[str, HsnCode | None] = {}

    for row in rows:
        if row.row_number in bad:
            continue  # can't semantically check a structurally-broken row
        c = row.cells
        if _project(db, c["project_id"], projects_cache) is None:
            errors.append(RowError(row.row_number, "project_id",
                                   f"project '{c['project_id']}' does not exist or is not Active"))
        hsn = _hsn(db, c["hsn"], hsns)
        if hsn is None:
            errors.append(RowError(row.row_number, "hsn",
                                   f"unknown or inactive HSN '{c['hsn']}'"))
        else:
            if c.get("gst_rate"):
                supplied = parsing.parse_qty(c["gst_rate"])
                if supplied is not None and hsn.gst_rate is not None and supplied != hsn.gst_rate:
                    errors.append(RowError(row.row_number, "gst_rate",
                                           f"gst_rate {supplied} does not match HSN {hsn.hsn} "
                                           f"rate {hsn.gst_rate}"))
            _amount_sanity(row, hsn, errors)

    _amount_presence_consistency(rows, errors)
    warnings.extend(_collision_warnings(rows))

    # Consignee identity is checked per-GSTIN over the structurally-clean rows: the
    # same GSTIN must carry the same details (error), and a known GSTIN whose details
    # differ from the stored golden record yields CONTRADICTIONS to resolve.
    clean_rows = [r for r in rows if r.row_number not in bad]
    _group_total_overflow_errors(clean_rows, errors)
    _consignee_consistency_errors(clean_rows, errors)
    contradictions = _detect_contradictions(db, clean_rows, errors)
    warnings.extend(_duplicate_register_warnings(db, clean_rows, exclude_batch_id))

    if skip_error_groups:
        errors = _filter_resume_errors(rows, errors, skip_error_groups)

    warnings.sort(key=lambda e: (e.row_number, e.column))
    if errors:
        errors.sort(key=lambda e: (e.row_number, e.column))
        return ValidationResult(errors=errors, warnings=warnings)
    return ValidationResult(
        warnings=warnings, contradictions=contradictions,
        challans=_build_challans(rows, hsns))


def _consignee_by_gstin(rows: list[RawRow]) -> dict[str, list[RawRow]]:
    """Group rows by normalized consignee GSTIN (blank GSTIN skipped)."""
    groups: dict[str, list[RawRow]] = {}
    for row in rows:
        gstin = consignee_master.normalize_gstin(row.cells.get("consignee_gstin", ""))
        if gstin:
            groups.setdefault(gstin, []).append(row)
    return groups


def _consignee_consistency_errors(rows: list[RawRow], errors: list[RowError]) -> None:
    """The SAME GSTIN must carry the SAME consignee details across the upload (it is
    one legal party). Consistency is tracked per FIELD against the first NON-EMPTY
    value seen for that field (NOT the first row, which may leave an optional field
    blank) — mirroring `_merged_consignee`'s first-non-empty pick, so the single
    merged value that gets snapshotted is provably the one agreed value.

    A blank is 'not specified' and never conflicts; two DIFFERENT non-empty values
    for one field are the blocking error — INCLUDING when an earlier row left the
    field blank. (A first-row-only comparison would let a later differing value slip
    through unflagged and be silently dropped by the merge, printing an address the
    operator never entered for that group.) This also guarantees each field has a
    single 'your value' to decide on in the contradiction review.
    """
    for gstin, grp in _consignee_by_gstin(rows).items():
        seen: dict[str, tuple[str, int]] = {}  # field short -> (first non-empty value, row)
        for row in grp:
            for short, key, numeric in _CONSIGNEE_DIFF_SPECS:
                value = row.cells.get(key, "").strip()
                if not value:
                    continue
                prev = seen.get(short)
                if prev is None:
                    seen[short] = (value, row.row_number)
                    continue
                prev_value, prev_row = prev
                same = (_digits(value) == _digits(prev_value) if numeric
                        else match_key(value) == match_key(prev_value))
                if not same:
                    errors.append(RowError(
                        row.row_number, f"consignee_{short}",
                        f"consignee {short} '{value}' differs from row {prev_row} "
                        f"('{prev_value}') for the same GSTIN {gstin}; the same GSTIN "
                        "must carry the same consignee details across the upload"))


def _merged_consignee(grp: list[RawRow]) -> dict[str, str]:
    """One representative consignee field-set for a GSTIN: the first non-empty value
    per field across its rows (non-empties agree — consistency is enforced by
    `_consignee_consistency_errors`, which flags any later differing value)."""
    merged: dict[str, str] = {}
    for _short, key, _numeric in _CONSIGNEE_DIFF_SPECS:
        merged[key] = ""
        for row in grp:
            value = row.cells.get(key, "").strip()
            if value:
                merged[key] = value
                break
    return merged


def _detect_contradictions(
    db: Session, rows: list[RawRow], errors: list[RowError]
) -> list[Contradiction]:
    """For each distinct KNOWN GSTIN whose uploaded details differ from the stored
    golden record, one Contradiction per differing field. An invalid GSTIN / state
    mismatch is a blocking error. Read-only (`dry_run`) — nothing is written here."""
    contradictions: list[Contradiction] = []
    for gstin, grp in _consignee_by_gstin(rows).items():
        merged = _merged_consignee(grp)
        # A state that contradicts the GSTIN's own state code is a blocking ERROR, not
        # a decidable contradiction: it can never be stored (would be a self-contradictory
        # golden record) nor printed on a statutory challan even "this upload only".
        if merged["consignee_state"] and not gstin_matches_state(gstin, merged["consignee_state"]):
            errors.append(RowError(
                grp[0].row_number, "consignee_state",
                f"consignee state '{merged['consignee_state']}' does not match the GSTIN "
                f"state code {gstin[:2]}"))
            continue
        try:
            result = consignee_master.resolve_or_create(
                db,
                gstin=gstin,
                name=merged["consignee_name"],
                address_line1=merged["consignee_address_line1"],
                address_line2=merged["consignee_address_line2"],
                pincode=merged["consignee_pincode"],
                state=merged["consignee_state"],
                phone=merged["consignee_phone"],
                source="UPLOAD",
                dry_run=True,
            )
        except ConsigneeMasterError as err:
            column = ("consignee_state" if "does not match the GSTIN" in str(err)
                      else "consignee_gstin")
            errors.append(RowError(grp[0].row_number, column, str(err)))
            continue
        for dev in result.deviations:
            contradictions.append(Contradiction(
                gstin=gstin, consignee_name=merged["consignee_name"],
                field=dev.field, stored=dev.stored, uploaded=dev.incoming))
    return contradictions


def _first_row_per_group(rows: list[RawRow]) -> tuple[dict[str, RawRow], list[str]]:
    """The first RawRow of each `challan_group`, plus the groups in first-seen order."""
    first_row: dict[str, RawRow] = {}
    order: list[str] = []
    for row in rows:
        key = row.cells.get("challan_group", "").strip()
        if key and key not in first_row:
            first_row[key] = row
            order.append(key)
    return first_row, order


def _identity_value(field: str, raw: str) -> str:
    """Normalize a collision-identity field. `challan_date` is parsed to ISO so two
    equivalent date spellings (16-05-2026 vs 2026-05-16) collide; text via match_key."""
    if field == "challan_date":
        parsed = parsing.parse_date(raw)
        return parsed.isoformat() if parsed is not None else match_key(raw)
    return match_key(raw)


def _collision_warnings(rows: list[RawRow]) -> list[RowError]:
    """Warn when two DIFFERENT groups share the same shipment identity tuple
    (consignee GSTIN + ship-to + date) — a likely "should this be one challan?"
    mistake. Non-blocking: legitimately-separate same-day shipments are allowed."""
    first_row, order = _first_row_per_group(rows)

    buckets: dict[tuple[str, ...], list[str]] = {}
    for key in order:
        cells = first_row[key].cells
        identity = tuple(_identity_value(f, cells.get(f, "")) for f in COLLISION_FIELDS)
        if all(part == "" for part in identity):
            continue  # nothing to compare (fully blank shipment fields)
        buckets.setdefault(identity, []).append(key)

    warnings: list[RowError] = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        for key in members:
            others = ", ".join(m for m in members if m != key)
            warnings.append(RowError(
                first_row[key].row_number, "challan_group",
                f"challan '{key}' ships to the same consignee + destination + date as "
                f"{others}; confirm these are separate challans, not one",
                severity="WARNING"))
    return warnings


def _duplicate_register_warnings(
    db: Session, rows: list[RawRow], exclude_batch_id: int | None
) -> list[RowError]:
    """Warn (NON-blocking) when a group would re-issue against a challan ALREADY in
    the register with the same (consignee GSTIN, challan group, date) — the tell of an
    accidental re-run / re-upload of a batch that already generated. The operator can
    still proceed (a genuine same-day repeat to the same consignee is allowed).

    The batch being validated is EXCLUDED so a RESUME of its own already-issued groups
    never warns against itself. One bounded query (scoped to the uploaded GSTINs +
    dates), matched in Python — never a full-register scan.
    """
    first_row, order = _first_row_per_group(rows)
    wanted: list[tuple[str, date, str]] = []  # (gstin, date, group label)
    gstins: set[str] = set()
    dates: set[date] = set()
    for key in order:
        cells = first_row[key].cells
        gstin = consignee_master.normalize_gstin(cells.get("consignee_gstin", ""))
        challan_date = parsing.parse_date(cells.get("challan_date", ""))
        if not gstin or challan_date is None:
            continue
        wanted.append((gstin, challan_date, key))
        gstins.add(gstin)
        dates.add(challan_date)
    if not wanted:
        return []
    stmt = select(
        Challan.consignee_gstin, Challan.challan_date, Challan.group_key, Challan.number
    ).where(
        Challan.status == ChallanStatus.ISSUED.value,
        Challan.consignee_gstin.in_(gstins),
        Challan.challan_date.in_(dates),
    )
    if exclude_batch_id is not None:
        stmt = stmt.where(Challan.batch_id != exclude_batch_id)
    existing: dict[tuple[str, date, str], str] = {
        (gstin, cdate, group): number
        for gstin, cdate, group, number in db.execute(stmt).all()
    }
    warnings: list[RowError] = []
    for gstin, challan_date, key in wanted:
        number = existing.get((gstin, challan_date, key))
        if number:
            warnings.append(RowError(
                first_row[key].row_number, "challan_group",
                f"challan '{key}' matches an already-issued challan {number} in the "
                "register (same consignee GSTIN, group, and date) — confirm this isn't "
                "a duplicate re-issue",
                severity="WARNING"))
    return warnings


# Consignee fields compared for same-GSTIN consistency + the merged snapshot:
# (short name used in the warning/decision column, upload cell key, compare-numerically?).
# Text fields compare via match_key; the numeric pair (pincode/phone) via digits-only,
# mirroring the golden-record deviation semantics.
_CONSIGNEE_DIFF_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("name", "consignee_name", False),
    ("address_line1", "consignee_address_line1", False),
    ("address_line2", "consignee_address_line2", False),
    ("state", "consignee_state", False),
    ("pincode", "consignee_pincode", True),
    ("phone", "consignee_phone", True),
)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _amount_presence_consistency(rows: list[RawRow], errors: list[RowError]) -> None:
    """Every line in a challan must be uniformly priced OR value-free — never mixed.

    A mixed challan would sum a PARTIAL total (only the priced lines), which both
    under-reports the shipment value and mis-drives the statutory e-way flag.
    """
    groups: dict[str, list[RawRow]] = {}
    for row in rows:
        key = row.cells.get("challan_group", "").strip()
        if key:
            groups.setdefault(key, []).append(row)
    for key, members in groups.items():
        priced = [bool(r.cells.get("amount", "").strip()) for r in members]
        if any(priced) and not all(priced):
            for row, has_amount in zip(members, priced, strict=True):
                if not has_amount:
                    errors.append(RowError(
                        row.row_number, "amount",
                        f"challan '{key}' mixes priced and value-free lines; "
                        "every line must have an amount or none must"))


def _group_total_overflow_errors(rows: list[RawRow], errors: list[RowError]) -> None:
    """A group's summed line amount must fit the int8 `total_paise` column.

    Each cell is already capped (`_MAX_MONEY_PAISE`), but a group with enough
    near-max priced lines sums past int8's ceiling, which would raise at INSERT and
    FAIL the batch AFTER numbers were reserved (burning statutory numbers). Reject
    the overflow here, at validation, with a clear row error on the group's first
    priced row — before any number is reserved.
    """
    totals: dict[str, int] = {}
    first_row: dict[str, int] = {}
    for row in rows:
        key = row.cells.get("challan_group", "").strip()
        raw = row.cells.get("amount", "").strip()
        if not key or not raw:
            continue
        amount = parsing.parse_paise(raw)
        if amount is None:
            continue  # a malformed amount is already flagged by structural checks
        totals[key] = totals.get(key, 0) + amount
        first_row.setdefault(key, row.row_number)
    for key, total in totals.items():
        if total > _MAX_INT8:
            errors.append(RowError(
                first_row[key], "amount",
                f"challan '{key}' total amount is too large; the summed line amount "
                "exceeds the maximum a challan can carry"))


def _amount_sanity(row: RawRow, hsn: HsnCode, errors: list[RowError]) -> None:
    """If an amount is given with a numeric rate, it must equal the tax-INCLUSIVE
    figure rate*qty*(1+gst). Skipped when the amount is blank (value-free line) or
    the rate is free text. The HSN's GST rate is authoritative here."""
    c = row.cells
    if not c.get("amount", "").strip():
        return  # value-free line — nothing to check
    _, rate_paise = parsing.parse_rate(c.get("rate", ""))
    qty = parsing.parse_qty(c["quantity"])
    amount = parsing.parse_paise(c["amount"])
    if rate_paise is None or qty is None or amount is None or qty <= 0:
        return
    gst = hsn.gst_rate or Decimal(0)
    gross = Decimal(rate_paise) * qty * (1 + gst / Decimal(100))
    expected = int(gross.to_integral_value(rounding=ROUND_HALF_UP))
    # 1% band (catches "forgot the GST") but capped at Rs 500 absolute, so a large
    # line can't hide a material typo below its 1% (expected is computed exactly).
    tol = min(max(100, int(Decimal(expected) * Decimal("0.01"))), _AMOUNT_TOL_CAP_PAISE)
    if abs(amount - expected) > tol:
        errors.append(RowError(row.row_number, "amount",
                               f"amount {amount / 100:.2f} != rate x qty +{gst}% GST "
                               f"({expected / 100:.2f})"))


def _active_consignor(db: Session) -> Consignor | None:
    """The single active consignor (the fixed dispatching entity), or None if the
    count isn't exactly one (0 = unconfigured, >1 = ambiguous)."""
    rows = db.execute(
        select(Consignor).where(Consignor.active.is_(True)).limit(2)
    ).scalars().all()
    return rows[0] if len(rows) == 1 else None


def _project(db: Session, code: str, cache: dict[str, Project | None]) -> Project | None:
    key = code.strip().upper()
    if key not in cache:
        cache[key] = projects.resolve_active_project(db, key)
    return cache[key]


def _hsn(db: Session, hsn: str, cache: dict[str, HsnCode | None]) -> HsnCode | None:
    if hsn not in cache:
        cache[hsn] = db.execute(
            select(HsnCode).where(HsnCode.hsn == hsn, HsnCode.active.is_(True))
        ).scalar_one_or_none()
    return cache[hsn]


def _filter_resume_errors(
    rows: list[RawRow], errors: list[RowError], skip_groups: frozenset[str]
) -> list[RowError]:
    """Drop re-validation errors for already-issued groups (a resume). Batch-level
    (row 0) errors and errors on the remaining not-yet-issued groups are kept. If
    every group is already issued, all errors drop — nothing is left to generate."""
    row_group = {r.row_number: r.cells.get("challan_group", "").strip() for r in rows}
    remaining = {g for g in row_group.values() if g} - skip_groups
    if not remaining:
        return []
    return [e for e in errors
            if e.row_number == 0 or row_group.get(e.row_number, "") in remaining]


def _issued_group_keys(db: Session, batch: ChallanBatch) -> frozenset[str]:
    """The `Challan Group` labels with a genuinely ISSUED challan for this batch.

    VOID challans are EXCLUDED: a voided group's allocation is also void, so a retry
    re-mints a number and re-issues it — that group must therefore be RE-VALIDATED,
    never skipped as 'done' (else a voided challan could be re-issued bypassing its
    HSN/project validation)."""
    return frozenset(
        db.execute(
            select(Challan.group_key).where(
                Challan.batch_id == batch.id,
                Challan.status == ChallanStatus.ISSUED.value,
            )
        ).scalars()
    ) - {""}


def _build_challans(
    rows: list[RawRow], hsns: dict[str, HsnCode | None]
) -> list[ParsedChallan]:
    """Group clean rows by `challan_group` (first-seen order) into ParsedChallans.

    Amount is OPTIONAL (None -> value-free line). GST is taken from the HSN (the
    authoritative rate, already cross-checked against any supplied gst_rate).
    Consignee fields are carried as TYPED; resolution/snapshot happens at generate.
    """
    groups: dict[str, ParsedChallan] = {}
    order: list[str] = []
    for row in rows:
        c = row.cells
        # Canonicalize the group key (strip) so the idempotency key, the stored
        # `Challan.group_key`, and the resume error-filter all compare identically —
        # not merely because the parser happens to strip cells.
        key = c["challan_group"].strip()
        if key not in groups:
            challan_date = parsing.parse_date(c["challan_date"])
            if challan_date is None:  # pragma: no cover - structurally pre-validated
                raise ChallanError(f"unparseable challan_date on row {row.row_number}")
            groups[key] = ParsedChallan(
                group_key=key,
                project_id=c["project_id"],
                ship_to_enterprise=c.get("ship_to_enterprise", ""),
                ship_to_name=c["ship_to_name"],
                ship_to_address_line1=c["ship_to_address_line1"],
                ship_to_address_line2=c.get("ship_to_address_line2", ""),
                ship_to_city=c.get("ship_to_city", ""),
                ship_to_state=c["ship_to_state"],
                ship_to_pincode=c.get("ship_to_pincode", ""),
                ship_to_phone=c.get("ship_to_phone", ""),
                consignee_name=c["consignee_name"],
                consignee_address_line1=c.get("consignee_address_line1", ""),
                consignee_address_line2=c.get("consignee_address_line2", ""),
                consignee_pincode=c.get("consignee_pincode", ""),
                consignee_state=c.get("consignee_state", ""),
                consignee_phone=c.get("consignee_phone", ""),
                consignee_gstin=c["consignee_gstin"],
                challan_date=challan_date,
                po_number=c.get("po_number", ""),
                invoice_number=c.get("invoice_number", ""),
            )
            order.append(key)
        pc = groups[key]
        rate_text, rate_paise = parsing.parse_rate(c.get("rate", ""))
        hsn = hsns.get(c["hsn"])
        pc.lines.append(ParsedLine(
            description=c["description"],
            hsn=c["hsn"],
            quantity=parsing.parse_qty(c["quantity"]) or Decimal(0),
            rate_text=rate_text,
            rate_paise=rate_paise,
            amount_paise=parsing.parse_paise(c["amount"]) if c.get("amount", "").strip() else None,
            gst_rate=hsn.gst_rate if hsn is not None else None,
        ))
    return [groups[k] for k in order]


# ------------------------------------------------------------------ generation

def generate(
    db: Session,
    batch: ChallanBatch,
    renderer: render.Renderer,
    *,
    series: str,
    actor_uid: str | None = None,
) -> ChallanBatch:
    """Reserve (commit) -> render -> issue -> package. Re-validates defensively.

    On validation failure: writes an English issue report and marks the batch
    FAILED_VALIDATION — no numbers are reserved. On success: every challan is
    ISSUED with a bound number and a stored PDF, plus a ZIP and a merged PDF.

    The whole body is wrapped so ANY unexpected error (missing source file, audit
    contention, a DB blip in the parse/reserve phase) marks the batch FAILED and
    voids any orphaned reservations, instead of leaving it wedged in the committed
    GENERATING state that `generate_batch` refuses to retry.
    """
    reservations: list[tuple[ParsedChallan, int]] = []
    try:
        return _generate_inner(
            db, batch, renderer, series=series, actor_uid=actor_uid,
            reservations=reservations,
        )
    except Exception as err:  # noqa: BLE001 - no failure may leave the batch GENERATING
        return _fail_generation(db, batch, reservations, actor_uid, err)


def _generate_inner(
    db: Session,
    batch: ChallanBatch,
    renderer: render.Renderer,
    *,
    series: str,
    actor_uid: str | None,
    reservations: list[tuple[ParsedChallan, int]],
) -> ChallanBatch:
    """The generate body (see `generate`). Appends each reservation to the passed
    `reservations` list so the wrapper can void orphans if an early phase throws."""
    storage = get_storage()
    rows, structural = parsing.parse_workbook(_source_bytes(db, batch, storage))
    # RESUME reconcile: on a retry, re-validation ignores errors for groups whose
    # challans are already issued (done + immutable) — so a master-data change to a
    # completed portion of the batch can't wedge finishing the rest. A change to a
    # still-remaining group is kept and blocks (correctly). A structural parse error
    # is never group-scoped, so it still blocks.
    skip_groups = _issued_group_keys(db, batch)
    result = (ValidationResult(errors=structural) if structural
              else validate(db, rows, skip_error_groups=skip_groups,
                            exclude_batch_id=batch.id))
    if not result.ok:
        report = _store(db, storage, _issue_report_bytes(result.issues),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        # A RETRY that still fails on a REMAINING (not-yet-issued) group must NOT bury
        # the already-issued statutory documents by zeroing counts as FAILED_VALIDATION.
        # Mark FAILED (retryable), preserve the issued count, and name the blocked rows.
        already_issued = db.execute(
            select(func.count()).select_from(Challan).where(
                Challan.batch_id == batch.id,
                Challan.status == ChallanStatus.ISSUED.value,
            )
        ).scalar() or 0
        if already_issued:
            blocked = sorted({e.row_number for e in result.errors if e.row_number})
            where = f" (rows {blocked})" if blocked else ""
            batch.status = BatchStatus.FAILED.value
            batch.message = (
                f"{already_issued} challan(s) already issued; the remaining challans "
                f"still fail validation{where} — resolve the master data and retry")
            _audit(db, "challan.batch_failed", actor_uid, batch.id,
                   {"already_issued": already_issued, "errors": len(result.errors)})
        else:
            batch.status = BatchStatus.FAILED_VALIDATION.value
            batch.challan_count = 0  # a clean re-validation failure invalidates earlier counts
            batch.line_count = 0
            batch.message = _issue_message(result)
            _audit(db, "challan.batch_failed_validation", actor_uid, batch.id,
                   {"errors": len(result.errors), "warnings": len(result.warnings)})
        db.commit()
        return batch

    batch.status = BatchStatus.GENERATING.value
    db.flush()

    # The operator's review decisions drive both the per-field snapshot (THIS_UPLOAD
    # and UPDATE_MASTER print the uploaded value; REJECT + undecided print stored) and
    # the golden-record write. The master write is deferred to the SUCCESS path below,
    # so a batch that fails to generate never leaves the shared master mutated.
    choice_map, uploaded_map = _decision_maps(db, batch)
    merged_by_gstin = {
        gstin: _merged_consignee(grp) for gstin, grp in _consignee_by_gstin(rows).items()
    }

    # --- RESERVE phase: one short transaction, COMMITTED before any render. ---
    try:
        for pc in result.challans:
            fy = numbering.fy_for(datetime.combine(pc.challan_date, time(12, 0), tzinfo=IST))
            alloc = numbering.allocate(
                db, series, fy=fy,
                idempotency_key=f"batch:{batch.id}:group:{pc.group_key}",
                reserved_by=actor_uid,
            )
            reservations.append((pc, alloc.id))
    except numbering.NumberingError as err:
        db.rollback()
        # NumberingError messages are our own operator-facing strings (e.g. "series L
        # is not configured — seed the starting number"), safe to surface. The audit
        # detail records only the error type, never a raw exception string.
        logger.warning("challan batch %s numbering failed", batch.id, exc_info=err)
        batch.status = BatchStatus.FAILED.value
        batch.message = f"could not reserve numbers: {err}"
        _audit(db, "challan.batch_failed", actor_uid, batch.id, {"error_type": "NumberingError"})
        db.commit()
        return batch
    db.commit()  # <-- reservations durable; numbering lock released BEFORE rendering

    # --- RENDER + ISSUE + PACKAGE: no numbering lock held. Guarded so a render/
    # storage failure marks the batch FAILED and voids the orphaned reservations
    # instead of wedging it in GENERATING. Resume-safe: a challan already issued
    # for an allocation (a prior run) is reused, never double-created. ---
    threshold_paise = _eway_threshold_paise(db)
    try:
        pdfs: list[tuple[str, bytes]] = []
        for pc, alloc_id in reservations:
            challan = _existing_challan(db, alloc_id)
            if challan is None:
                challan = _persist_challan(db, batch, pc, alloc_id, threshold_paise,
                                           actor_uid, choice_map, uploaded_map, merged_by_gstin)
                pdf = renderer.render_pdf(render.build_challan_html(_build_view(challan)))
                stored = _store(db, storage, pdf, f"{_safe(challan.number)}.pdf",
                                "challan-pdf", actor_uid, "application/pdf")
                challan.pdf_file_id = stored.id
                numbering.issue(db, _allocation(db, alloc_id), entity="challan",
                                entity_id=str(challan.id), actor_uid=actor_uid)
                # Progress HEARTBEAT: stamp the batch row each issued challan so
                # `updated_at` reflects real generation progress, not just when
                # GENERATING was entered. This is what lets the stuck-sweep / recover
                # distinguish a live-but-slow render from a dead worker (a long render
                # keeps bumping this; a lost worker stops) — without it, a healthy
                # large batch would be swept to FAILED mid-flight.
                batch.updated_at = datetime.now(UTC)
                db.commit()
            pdfs.append((f"{_safe(challan.number)}.pdf", _stored_bytes(db, storage, challan)))

        zip_file = _store(db, storage, render.zip_files(pdfs), f"batch-{batch.id}.zip",
                          "challan-zip", actor_uid, "application/zip")
        merged = _store(db, storage, render.merge_pdfs([p for _, p in pdfs]),
                        f"batch-{batch.id}-merged.pdf", "challan-merged", actor_uid,
                        "application/pdf")
        # Every challan is issued — NOW write the operator's UPDATE_MASTER choices to
        # the shared golden record (idempotent on a resumed run), so a failed batch
        # never leaves the master mutated without a document to show for it.
        _apply_master_updates(db, choice_map, uploaded_map, actor_uid)
        batch.zip_file_id = zip_file.id
        batch.merged_pdf_file_id = merged.id
        batch.challan_count = len(reservations)
        batch.line_count = sum(len(pc.lines) for pc, _ in reservations)
        batch.status = BatchStatus.COMPLETED.value
        batch.message = None
        _audit(db, "challan.batch_completed", actor_uid, batch.id,
               {"challans": batch.challan_count, "lines": batch.line_count})
        db.commit()
        return batch
    except Exception as err:  # noqa: BLE001 - any generation failure must not wedge the batch
        return _fail_generation(db, batch, reservations, actor_uid, err)


def _fail_generation(
    db: Session,
    batch: ChallanBatch,
    reservations: list[tuple[ParsedChallan, int]],
    actor_uid: str | None,
    err: Exception,
) -> ChallanBatch:
    """Mark a partially-generated batch FAILED and void its orphaned reservations.

    Challans already ISSUED (committed per-challan) are kept — their numbers are
    legitimately used. Only the still-RESERVED numbers for this batch are voided
    so the sweeper/register stay honest. The batch can be retried.
    """
    db.rollback()
    for _pc, alloc_id in reservations:
        alloc = db.get(NumberingAllocation, alloc_id)
        if alloc is not None and alloc.status == AllocationStatus.RESERVED.value:
            numbering.void(db, alloc, reason="batch generation failed", actor_uid=actor_uid)
    batch.status = BatchStatus.FAILED.value
    # The raw exception can embed internal detail (DB SQL/params, storage refs, file
    # paths) — it is logged server-side, NEVER surfaced in the user-visible message or
    # the audit trail. The operator gets a generic, actionable message.
    logger.error("challan batch %s generation failed", batch.id, exc_info=err)
    batch.message = ("generation failed due to an internal error — please retry; "
                     "if it persists, contact support")
    _audit(db, "challan.batch_failed", actor_uid, batch.id, {"error_type": type(err).__name__})
    db.commit()
    return batch


def recover_stuck_batch(
    db: Session, batch: ChallanBatch, *, actor_uid: str | None = None,
    now: datetime | None = None,
) -> ChallanBatch:
    """Reset a batch STUCK in GENERATING back to a retryable FAILED state.

    A batch enters GENERATING and its worker renders+issues; if that worker dies
    (crash, OOM during render, or a deploy that drops the in-process job), nothing
    else moves the batch out of GENERATING and it can't be retried — the operator's
    only apparent recourse would be re-uploading, which would mint DUPLICATE statutory
    numbers for the already-issued groups. This reconciles it to FAILED so the
    resume-aware retry can finish it in place (issued groups skipped; still-RESERVED
    numbers RESUMED via the numbering idempotency key, never re-minted).

    GUARDED on the progress heartbeat: a batch whose `updated_at` advanced within the
    last `_STUCK_AFTER` is still making progress (a live worker) and is REFUSED — so a
    recover racing a healthy render can't flip a live batch to FAILED (which would let
    a second worker resume the same reservations and void the first's, or tempt a
    duplicate-minting re-upload). Raises `ChallanError` if the batch isn't GENERATING
    or hasn't been idle long enough to look stuck.
    """
    if batch.status != BatchStatus.GENERATING.value:
        raise ChallanError(f"batch is {batch.status}, not stuck in generation")
    now = now or datetime.now(UTC)
    if _as_utc(batch.updated_at) > now - _STUCK_AFTER:
        raise ChallanError(
            "batch is still making progress (it advanced in the last "
            f"{int(_STUCK_AFTER.total_seconds() // 60)} min) — it doesn't look stuck; "
            "if it truly isn't advancing, wait a moment and try again")
    issued = db.execute(
        select(func.count()).select_from(Challan).where(
            Challan.batch_id == batch.id,
            Challan.status == ChallanStatus.ISSUED.value,
        )
    ).scalar() or 0
    batch.status = BatchStatus.FAILED.value
    if issued:
        batch.message = (f"generation was interrupted; {issued} challan(s) already "
                         "issued — retry to finish the remaining challans")
    else:
        batch.message = ("generation was interrupted before any challan was issued "
                         "— retry to generate")
    _audit(db, "challan.batch_recovered", actor_uid, batch.id, {"already_issued": issued})
    db.commit()
    return batch


def sweep_stuck_batches(
    db: Session, *, older_than: timedelta, now: datetime | None = None
) -> list[int]:
    """Reset batches wedged in GENERATING past `older_than` back to FAILED (retryable).

    The unattended counterpart of `recover_stuck_batch`: a deploy that evicts the
    running container drops in-process generation jobs, leaving their batches stuck in
    GENERATING. `older_than` is measured against the progress HEARTBEAT (`updated_at`,
    bumped per issued challan in the render loop), NOT the time GENERATING was entered —
    so a live-but-slow render keeps advancing the heartbeat and is never swept, while a
    dead worker's batch stops advancing and is healed. Issued challans are kept and
    still-RESERVED numbers are left for the idempotent resume / the numbering sweeper. A
    SINGLE guarded atomic UPDATE (status re-checked at write time) so a batch that
    legitimately COMPLETED in the window is never clobbered. Caller commits.
    """
    now = now or datetime.now(UTC)
    cutoff = now - older_than
    message = ("generation was interrupted (the worker was lost) — retry to finish "
               "this batch")
    stmt = (
        update(ChallanBatch)
        .where(
            ChallanBatch.status == BatchStatus.GENERATING.value,
            ChallanBatch.updated_at < cutoff,
        )
        .values(status=BatchStatus.FAILED.value, message=message)
        .returning(ChallanBatch.id)
        .execution_options(synchronize_session=False)
    )
    ids = [int(i) for i in db.execute(stmt).scalars().all()]
    for batch_id in ids:
        _audit(db, "challan.batch_recovered", "system:sweeper", batch_id, {"swept": True})
    return ids


def _persist_challan(
    db: Session,
    batch: ChallanBatch,
    pc: ParsedChallan,
    alloc_id: int,
    threshold_paise: int,
    actor_uid: str | None,
    choice_map: dict[tuple[str, str], str],
    uploaded_map: dict[tuple[str, str], str],
    merged_by_gstin: dict[str, dict[str, str]],
) -> Challan:
    """Build the Challan + line items with fresh snapshots.

    The consignee is resolved against the GSTIN golden record HERE. A brand-new GSTIN
    is auto-created from the MERGED (first-non-empty-per-field across the whole batch)
    values, so a value one group left blank and another filled is never lost. Each
    snapshotted field honours the operator's review decision: THIS_UPLOAD and
    UPDATE_MASTER print the uploaded value; REJECT and any undecided field print the
    stored value. The project is re-checked Active; ship-to address is joined.
    """
    consignor = _active_consignor(db)
    project = projects.resolve_active_project(db, pc.project_id)
    if consignor is None or project is None:  # pragma: no cover - pre-validated
        raise ChallanError("consignor/project changed after validation")
    merged = merged_by_gstin.get(consignee_master.normalize_gstin(pc.consignee_gstin), {})
    res = consignee_master.resolve_or_create(
        db,
        gstin=pc.consignee_gstin,
        name=merged.get("consignee_name") or pc.consignee_name,
        address_line1=merged.get("consignee_address_line1", pc.consignee_address_line1),
        address_line2=merged.get("consignee_address_line2", pc.consignee_address_line2),
        pincode=merged.get("consignee_pincode", pc.consignee_pincode),
        state=merged.get("consignee_state", pc.consignee_state),
        phone=merged.get("consignee_phone", pc.consignee_phone),
        source="UPLOAD",
        actor_uid=actor_uid,
    )
    party = res.party
    rc = _resolved_consignee(party, choice_map, uploaded_map)
    alloc = _allocation(db, alloc_id)
    total = pc.total_paise  # int | None (None => value-free challan)
    challan = Challan(
        batch_id=batch.id,
        allocation_id=alloc.id,
        number=alloc.formatted,
        series=alloc.series,
        fy=alloc.fy,
        number_int=alloc.number,
        challan_date=pc.challan_date,
        project_code=project.code,
        group_key=pc.group_key,
        consignor_name=consignor.name,
        consignor_gstin=consignor.gstin,
        consignor_state=consignor.state,
        consignor_address=consignor.address,
        consignor_phone=consignor.phone,
        consignee_brand="",
        consignee_name=rc["name"],
        consignee_gstin=party.gstin,
        consignee_state=rc["state"],
        consignee_address=_join_consignee_values(rc),
        consignee_phone=rc["phone"],
        ship_to_name=pc.ship_to_name,
        ship_to_address=_join_ship_to(pc),
        ship_to_address_line1=pc.ship_to_address_line1,
        ship_to_address_line2=pc.ship_to_address_line2,
        ship_to_city=pc.ship_to_city,
        ship_to_pincode=pc.ship_to_pincode,
        ship_to_state=pc.ship_to_state,
        ship_to_enterprise=pc.ship_to_enterprise,
        ship_to_number=pc.ship_to_phone,
        ship_to_contact="",
        po_number=pc.po_number,
        invoice_number=pc.invoice_number,
        eway_required=total is not None and total > threshold_paise,
        total_paise=total,
        status=ChallanStatus.ISSUED.value,
        created_by=actor_uid,
    )
    for i, line in enumerate(pc.lines, start=1):
        challan.lines.append(ChallanLineItem(
            line_no=i,
            description=line.description,
            hsn=line.hsn,
            quantity=line.quantity,
            rate_text=line.rate_text,
            rate_paise=line.rate_paise,
            amount_paise=line.amount_paise,
            gst_rate=line.gst_rate,
        ))
    db.add(challan)
    db.flush()
    return challan


# --------------------------------------------------------------------- void

def void_challan(
    db: Session, challan: Challan, *, reason: str, actor_uid: str | None = None
) -> Challan:
    """Void an issued challan and its bound number (both terminal, retained)."""
    if challan.status == ChallanStatus.VOID.value:
        raise ChallanError("challan already void")
    alloc = _allocation(db, challan.allocation_id)
    try:
        numbering.void(db, alloc, reason=reason, actor_uid=actor_uid)
    except numbering.NumberingError as err:
        raise ChallanError(str(err)) from err  # e.g. a concurrent double-void -> 409
    challan.status = ChallanStatus.VOID.value
    challan.void_reason = reason.strip()
    _audit(db, "challan.void", actor_uid, challan.id, {"number": challan.number, "reason": reason})
    db.commit()
    return challan


# ------------------------------------------------------------------- helpers

def _join_ship_to(pc: ParsedChallan) -> str:
    """Join the split ship-to fields into one printed address block."""
    parts = [pc.ship_to_address_line1, pc.ship_to_address_line2, pc.ship_to_city,
             pc.ship_to_state, pc.ship_to_pincode]
    return ", ".join(p.strip() for p in parts if p.strip())


# Consignee golden-record fields snapshotted onto a challan (attr name == decision
# `field`). Order also drives the joined printed address (line1/line2/pincode).
_CONSIGNEE_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "name", "address_line1", "address_line2", "pincode", "state", "phone",
)


def _decision_maps(
    db: Session, batch: ChallanBatch
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """(choice_map, uploaded_map) keyed by (gstin, field) from the batch's review
    decisions, so generation can honour each field-level choice."""
    choice_map: dict[tuple[str, str], str] = {}
    uploaded_map: dict[tuple[str, str], str] = {}
    for d in db.execute(
        select(ChallanBatchDecision).where(ChallanBatchDecision.batch_id == batch.id)
    ).scalars():
        choice_map[(d.gstin, d.field)] = d.choice
        uploaded_map[(d.gstin, d.field)] = d.uploaded_value
    return choice_map, uploaded_map


def _apply_master_updates(
    db: Session,
    choice_map: dict[tuple[str, str], str],
    uploaded_map: dict[tuple[str, str], str],
    actor_uid: str | None,
) -> None:
    """Write every UPDATE_MASTER decision's uploaded value onto its golden record
    (once per GSTIN). A brand-new GSTIN has no record yet — it is created from the
    upload at persist, so there is nothing to update here."""
    updates: dict[str, dict[str, str]] = {}
    for (gstin, field), choice in choice_map.items():
        if choice == DecisionChoice.UPDATE_MASTER.value:
            updates.setdefault(gstin, {})[field] = uploaded_map[(gstin, field)]
    for gstin, fields in updates.items():
        # Row-lock the golden record so two batches applying UPDATE_MASTER to the same
        # GSTIN serialize (no lost update); a no-op on SQLite, real on Postgres.
        party = db.execute(
            select(ConsigneeParty).where(ConsigneeParty.gstin == gstin).with_for_update()
        ).scalar_one_or_none()
        if party is None:  # pragma: no cover - a known-GSTIN contradiction implies it exists
            continue
        consignee_master.apply_incoming(db, party=party, actor_uid=actor_uid, **fields)
    db.flush()


_PRINT_UPLOADED = frozenset({DecisionChoice.THIS_UPLOAD.value, DecisionChoice.UPDATE_MASTER.value})


def _resolved_consignee(
    party: ConsigneeParty,
    choice_map: dict[tuple[str, str], str],
    uploaded_map: dict[tuple[str, str], str],
) -> dict[str, str]:
    """The consignee field values to snapshot: the uploaded value when the operator
    chose to print it (THIS_UPLOAD or UPDATE_MASTER), else the stored golden-record
    value (REJECT and any undecided/PENDING field always fall back to STORED — never
    an unvetted upload). The snapshot does NOT depend on the master having been
    pre-updated, so the golden-record write can safely happen only on batch success."""
    out: dict[str, str] = {}
    for field in _CONSIGNEE_SNAPSHOT_FIELDS:
        if choice_map.get((party.gstin, field)) in _PRINT_UPLOADED:
            out[field] = uploaded_map[(party.gstin, field)]
        else:
            out[field] = getattr(party, field)
    return out


def _join_consignee_values(rc: dict[str, str]) -> str:
    """Join the resolved address lines + pincode into one printed block."""
    parts = [rc["address_line1"], rc["address_line2"], rc["pincode"]]
    return ", ".join(p.strip() for p in parts if p.strip())


def _source_bytes(db: Session, batch: ChallanBatch, storage: Storage) -> bytes:
    if batch.source_file_id is None:
        raise ChallanError("batch has no source file")
    sf = db.get(StoredFile, batch.source_file_id)
    if sf is None:
        raise ChallanError("source file missing")
    with storage.open(sf.storage_ref) as fh:
        return fh.read()


def _allocation(db: Session, alloc_id: int) -> NumberingAllocation:
    alloc = db.get(NumberingAllocation, alloc_id)
    if alloc is None:  # pragma: no cover - reserved in this run
        raise ChallanError("allocation vanished")
    return alloc


def _existing_challan(db: Session, alloc_id: int) -> Challan | None:
    """A challan already issued for this allocation (resume-safe re-run)."""
    return db.execute(
        select(Challan).where(Challan.allocation_id == alloc_id)
    ).scalar_one_or_none()


def _stored_bytes(db: Session, storage: Storage, challan: Challan) -> bytes:
    """Read a challan's stored PDF bytes (works for freshly-rendered or resumed)."""
    if challan.pdf_file_id is None:  # pragma: no cover - set before packaging
        raise ChallanError(f"challan {challan.number} has no PDF")
    sf = db.get(StoredFile, challan.pdf_file_id)
    if sf is None:  # pragma: no cover
        raise ChallanError("challan PDF file missing")
    with storage.open(sf.storage_ref) as fh:
        return fh.read()


_DEFAULT_EWAY_RUPEES = 50000  # statutory default if the setting is missing/invalid


def _eway_threshold_paise(db: Session) -> int:
    """E-way threshold in paise. Tolerates "50,000"/float; falls back to the
    statutory default for a missing, unparseable, OR non-positive setting — a
    threshold of 0/negative would otherwise disable e-way for every challan, the
    opposite of the safe (fail-open) direction. Never returns <= 0."""
    rupees = Decimal(_DEFAULT_EWAY_RUPEES)
    setting = db.get(Setting, "eway_threshold")
    if setting is not None and isinstance(setting.value, dict):
        raw = str(setting.value.get("amount", _DEFAULT_EWAY_RUPEES)).replace(",", "")
        try:
            parsed = Decimal(raw)
        except (InvalidOperation, ValueError):
            parsed = Decimal(_DEFAULT_EWAY_RUPEES)
        rupees = parsed if parsed > 0 else Decimal(_DEFAULT_EWAY_RUPEES)
    return int(rupees * 100)


def _store(
    db: Session,
    storage: Storage,
    data: bytes,
    filename: str,
    kind: str,
    uploaded_by: str | None,
    content_type: str,
) -> StoredFile:
    ref = storage.save(f"challan/{uuid4().hex}/{filename}", data)
    sf = StoredFile(
        kind=kind,
        filename=filename,
        content_type=content_type,
        size=len(data),
        storage_ref=ref,
        uploaded_by=uploaded_by or "system",
        module_key=MODULE_KEY,
    )
    db.add(sf)
    db.flush()
    return sf


def _csv_field(value: str) -> str:
    """Quote a CSV field and neutralize spreadsheet formula/DDE injection.

    A field derived from an untrusted cell that begins with = + - @ (or a control
    char) is prefixed with `'` so Excel/Sheets treats it as text, not a formula.
    An embedded double-quote is escaped by DOUBLING it (RFC 4180) rather than
    altering it, so the exported value stays faithful to the record.
    """
    value = value.replace('"', '""')
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return f'"{value}"'


def _issue_message(result: ValidationResult) -> str:
    """A short human summary of the validation outcome for `batch.message`."""
    parts = []
    if result.errors:
        parts.append(f"{len(result.errors)} validation error(s)")
    if result.warnings:
        parts.append(f"{len(result.warnings)} warning(s)")
    return "; ".join(parts) if parts else "no issues"


def _issue_report_bytes(issues: list[RowError]) -> bytes:
    """Render errors + warnings to the downloadable English report (CSV).

    A `Severity` column distinguishes blocking ERRORs from non-blocking WARNINGs
    so the operator sees deviations/possible-splits without them blocking generate.
    """
    lines = ["Row,Severity,Column,Problem"]
    for e in issues:
        lines.append(
            f"{e.row_number},{_csv_field(e.severity)},{_csv_field(e.column)},{_csv_field(e.message)}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_view(ch: Challan) -> ChallanView:
    show_amount = ch.total_paise is not None
    code = ch.consignee_gstin[:2] if len(ch.consignee_gstin) >= 2 else ""
    state_label = f"{ch.consignee_state} ({code})" if ch.consignee_state else code
    return ChallanView(
        number=ch.number,
        project_id=ch.project_code,
        invoice_number=ch.invoice_number,
        challan_date=_ordinal_date(ch.challan_date),
        consignor=ConsignorView(
            name=ch.consignor_name, warehouse_address=ch.consignor_address,
            gstin=ch.consignor_gstin, phone=ch.consignor_phone),
        consignee=ConsigneeView(
            name=ch.consignee_name, address=ch.consignee_address, gstin=ch.consignee_gstin,
            state_label=state_label, phone=ch.consignee_phone),
        ship_to=ShipToView(
            name=ch.ship_to_name, address=ch.ship_to_address,
            enterprise=ch.ship_to_enterprise, phone=ch.ship_to_number),
        lines=[
            LineView(
                line_no=line.line_no,
                description=line.description,
                hsn=line.hsn,
                quantity=_qty(line.quantity),
                rate=line.rate_text,
                amount="" if line.amount_paise is None else _rupees(line.amount_paise),
            )
            for line in ch.lines
        ],
        total_qty=_qty(sum((line.quantity for line in ch.lines), Decimal(0))),
        total_amount=_rupees(ch.total_paise) if show_amount else "",
        show_amount=show_amount,
    )


def _ordinal_date(d: date) -> str:
    """`date(2026,7,28)` -> "28th July 2026" (matches the L/433 challan style)."""
    n = d.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} {d.strftime('%B %Y')}"


def _qty(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _indian_grouping(digits: str) -> str:
    """Group an integer string in the Indian system: 163620 -> '1,63,620'."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _rupees(paise: int | None) -> str:
    """Indian-grouped rupees; drops a trailing .00 (matches L/433: '41,890')."""
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    value = (Decimal(abs(paise)) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = int(value)
    frac = value - whole
    grouped = _indian_grouping(str(whole))
    return sign + grouped if frac == 0 else f"{sign}{grouped}.{f'{frac:.2f}'[2:]}"


def _safe(number: str) -> str:
    return number.replace("/", "_")


def _audit(
    db: Session, action: str, actor_uid: str | None, batch_or_id: int, detail: dict[str, object]
) -> None:
    audit.log(
        db,
        action=action,
        actor_uid=actor_uid,
        entity="challan_batch" if action.startswith("challan.batch") else "challan",
        entity_id=str(batch_or_id),
        detail=detail,
    )


def new_batch(db: Session, *, source_file_id: int, actor_uid: str | None) -> ChallanBatch:
    """Create a PENDING batch bound to an uploaded source file."""
    batch = ChallanBatch(
        source_file_id=source_file_id,
        status=BatchStatus.PENDING.value,
        created_by=actor_uid,
    )
    db.add(batch)
    db.flush()
    return batch


_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _clear_decisions(db: Session, batch: ChallanBatch) -> None:
    """Drop any prior review decisions for the batch (a fresh validation supersedes)."""
    db.execute(
        delete(ChallanBatchDecision).where(ChallanBatchDecision.batch_id == batch.id)
    )
    db.flush()


def _persist_decisions(
    db: Session, batch: ChallanBatch, contradictions: list[Contradiction]
) -> None:
    """Write one PENDING decision row per (gstin, field) contradiction."""
    for c in contradictions:
        db.add(ChallanBatchDecision(
            batch_id=batch.id, gstin=c.gstin, consignee_name=c.consignee_name,
            field=c.field, stored_value=c.stored, uploaded_value=c.uploaded,
            choice=DecisionChoice.PENDING.value,
        ))
    db.flush()


def validate_batch(db: Session, batch: ChallanBatch, *, actor_uid: str | None) -> ValidationResult:
    """Parse + validate the batch's source file synchronously (no rendering).

    Sets the batch to FAILED_VALIDATION (errors), NEEDS_REVIEW (no errors but the
    upload contradicts the stored consignee golden record — one decision per
    contradiction is written, and generation is blocked until each is resolved), or
    VALIDATED (ready to generate). No numbers are reserved and no golden records are
    written here. A VALIDATED batch with non-blocking WARNINGS still attaches a CSV
    issue report; a NEEDS_REVIEW batch attaches the downloadable Excel review report.
    """
    storage = get_storage()
    rows, structural = parsing.parse_workbook(_source_bytes(db, batch, storage))
    result = (ValidationResult(errors=structural) if structural
              else validate(db, rows, exclude_batch_id=batch.id))
    _clear_decisions(db, batch)
    if not result.ok:
        report = _store(db, storage, _issue_report_bytes(result.issues),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        batch.status = BatchStatus.FAILED_VALIDATION.value
        batch.challan_count = 0
        batch.line_count = 0
        batch.message = _issue_message(result)
    elif result.contradictions:
        _persist_decisions(db, batch, result.contradictions)
        report = _store(
            db, storage, review_report.build_review_xlsx(rows, result.contradictions),
            f"batch-{batch.id}-review.xlsx", "challan-review-report", actor_uid, _XLSX_MEDIA)
        batch.error_report_file_id = report.id
        batch.status = BatchStatus.NEEDS_REVIEW.value
        batch.challan_count = len(result.challans)
        batch.line_count = sum(len(pc.lines) for pc in result.challans)
        batch.message = (f"{len(result.contradictions)} consignee contradiction(s) to "
                         "review before generating")
    else:
        if result.warnings:
            report = _store(db, storage, _issue_report_bytes(result.warnings),
                            f"batch-{batch.id}-warnings.csv", "challan-error-report",
                            actor_uid, "text/csv")
            batch.error_report_file_id = report.id
        else:
            batch.error_report_file_id = None
        batch.status = BatchStatus.VALIDATED.value
        batch.challan_count = len(result.challans)
        batch.line_count = sum(len(pc.lines) for pc in result.challans)
        batch.message = _issue_message(result) if result.warnings else None
    _audit(db, "challan.batch_validated", actor_uid, batch.id,
           {"status": batch.status, "errors": len(result.errors),
            "warnings": len(result.warnings), "contradictions": len(result.contradictions)})
    db.commit()
    return result


def list_decisions(db: Session, batch: ChallanBatch) -> list[ChallanBatchDecision]:
    """The batch's consignee contradictions, oldest-first (stable review order)."""
    return list(db.execute(
        select(ChallanBatchDecision)
        .where(ChallanBatchDecision.batch_id == batch.id)
        .order_by(ChallanBatchDecision.id.asc())
    ).scalars())


def submit_decisions(
    db: Session,
    batch: ChallanBatch,
    choices: dict[int, str],
    *,
    actor_uid: str | None,
    allow_update_master: bool = False,
) -> ChallanBatch:
    """Record the operator's choice for one or more contradictions. When NONE remain
    PENDING the batch flips NEEDS_REVIEW -> VALIDATED (ready to generate); otherwise
    it stays in review. Raises `ChallanError` (route -> 409/422) on a bad state,
    unknown decision id, or invalid choice.

    `allow_update_master` re-asserts the admin gate at the SERVICE boundary (not only
    the HTTP route): an `UPDATE_MASTER` choice — the sole one that mutates the SHARED
    golden record — is refused unless the caller is authorized, so any future caller
    of this function inherits the check instead of an ungated master write."""
    if batch.status != BatchStatus.NEEDS_REVIEW.value:
        raise ChallanError(f"batch is {batch.status}, not awaiting review")
    # Serialize concurrent submits on this batch so two PATCHes each resolving the
    # last-pending decision can't both read the other as PENDING and leave the batch
    # stuck NEEDS_REVIEW with zero pending. (A no-op on SQLite; real on Postgres.)
    db.execute(
        select(ChallanBatch.id).where(ChallanBatch.id == batch.id).with_for_update()
    ).scalar_one_or_none()
    valid = {c.value for c in DecisionChoice if c is not DecisionChoice.PENDING}
    rows = {d.id: d for d in list_decisions(db, batch)}
    for decision_id, choice in choices.items():
        decision = rows.get(decision_id)
        if decision is None:
            raise ChallanError(f"decision {decision_id} is not part of this batch")
        if choice not in valid:
            raise ChallanError(f"choice must be one of {sorted(valid)}")
        if choice == DecisionChoice.UPDATE_MASTER.value and not allow_update_master:
            raise ChallanError(
                "updating the saved consignee record requires admin; choose "
                "'This upload only' or 'Reject', or ask an admin to update the master")
        decision.choice = choice
    db.flush()
    pending = sum(1 for d in rows.values() if d.choice == DecisionChoice.PENDING.value)
    if pending == 0:
        batch.status = BatchStatus.VALIDATED.value
        batch.message = None
    else:
        batch.message = f"{pending} consignee contradiction(s) to review before generating"
    _audit(db, "challan.batch_reviewed", actor_uid, batch.id,
           {"submitted": len(choices), "pending": pending, "status": batch.status})
    db.commit()
    return batch
