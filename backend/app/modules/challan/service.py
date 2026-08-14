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

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.challan import parsing, render
from app.modules.challan.models import (
    BatchStatus,
    Challan,
    ChallanBatch,
    ChallanLineItem,
    ChallanStatus,
)
from app.modules.challan.schema import (
    COLLISION_FIELDS,
    ChallanView,
    ConsigneeView,
    ConsignorView,
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
from app.modules.masterdata.normalize import match_key, valid_gstin
from app.modules.numbering import service as numbering
from app.modules.numbering.models import AllocationStatus, NumberingAllocation
from app.modules.numbering.service import IST
from app.modules.projects import service as projects
from app.modules.projects.models import Project
from app.platform import audit
from app.platform.models import Setting
from app.platform.storage import Storage, get_storage

MODULE_KEY = "document_automation"
# Per-batch caps: one upload can't burn an unbounded slice of the statutory
# sequence (numbers are never reused) or schedule unbounded rendering work.
MAX_BATCH_ROWS = 5000
MAX_BATCH_CHALLANS = 2000
_AMOUNT_TOL_CAP_PAISE = 50000  # Rs 500 — absolute ceiling on the amount-sanity band


class ChallanError(Exception):
    """Raised for unrecoverable generation problems (e.g. unconfigured series)."""


# ------------------------------------------------------------------ validation

def validate(db: Session, rows: list[RawRow]) -> ValidationResult:
    """Structural checks (no DB) + semantic checks (master data / projects /
    consignee golden record), then group.

    Returns every blocking ERROR (for a complete English report) plus non-blocking
    WARNINGS (consignee deviations, possible splits), or — when there are no errors
    — the parsed challans grouped by `challan_group`. No golden records are written
    here (consignee resolution runs `dry_run`); the real write is at generation.
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
        _consignee_preview(db, row, errors, warnings)
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
    warnings.extend(_intrabatch_new_gstin_warnings(db, rows))

    warnings.sort(key=lambda e: (e.row_number, e.column))
    if errors:
        errors.sort(key=lambda e: (e.row_number, e.column))
        return ValidationResult(errors=errors, warnings=warnings)
    return ValidationResult(warnings=warnings, challans=_build_challans(rows, hsns))


def _consignee_preview(
    db: Session, row: RawRow, errors: list[RowError], warnings: list[RowError]
) -> None:
    """Read-only consignee resolution (`dry_run`): an invalid GSTIN / state mismatch
    is a blocking error; a known GSTIN whose typed details differ from the stored
    golden record yields one WARNING per field (the stored value is what we use)."""
    c = row.cells
    try:
        result = consignee_master.resolve_or_create(
            db,
            gstin=c["consignee_gstin"],
            name=c["consignee_name"],
            address_line1=c.get("consignee_address_line1", ""),
            address_line2=c.get("consignee_address_line2", ""),
            pincode=c.get("consignee_pincode", ""),
            state=c.get("consignee_state", ""),
            phone=c.get("consignee_phone", ""),
            source="UPLOAD",
            dry_run=True,
        )
    except ConsigneeMasterError as err:
        column = "consignee_state" if "does not match the GSTIN" in str(err) else "consignee_gstin"
        errors.append(RowError(row.row_number, column, str(err)))
        return
    for dev in result.deviations:
        warnings.append(RowError(
            row.row_number, f"consignee_{dev.field}",
            f"consignee {dev.field}: using the stored value '{dev.stored}' "
            f"(you entered '{dev.incoming}')",
            severity="WARNING"))


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


# Consignee fields to compare for the intra-batch new-GSTIN check: (short name used
# in the warning column, upload cell key). Text fields compare via match_key; the
# numeric pair via digits-only, mirroring the golden-record deviation semantics.
_CONSIGNEE_DIFF_TEXT: tuple[tuple[str, str], ...] = (
    ("name", "consignee_name"),
    ("address_line1", "consignee_address_line1"),
    ("address_line2", "consignee_address_line2"),
    ("state", "consignee_state"),
)
_CONSIGNEE_DIFF_NUM: tuple[tuple[str, str], ...] = (
    ("pincode", "consignee_pincode"),
    ("phone", "consignee_phone"),
)


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _consignee_field_diffs(
    first: dict[str, str], later: dict[str, str]
) -> list[tuple[str, str, str]]:
    """(short_field, first_value, later_value) for each field the later row typed
    differently from the first row's. An empty later value is never a difference."""
    diffs: list[tuple[str, str, str]] = []
    for short, key in _CONSIGNEE_DIFF_TEXT:
        a, b = first.get(key, ""), later.get(key, "")
        if b and match_key(a) != match_key(b):
            diffs.append((short, a, b))
    for short, key in _CONSIGNEE_DIFF_NUM:
        a, b = first.get(key, ""), later.get(key, "")
        if b and _digits(a) != _digits(b):
            diffs.append((short, a, b))
    return diffs


def _intrabatch_new_gstin_warnings(db: Session, rows: list[RawRow]) -> list[RowError]:
    """Warn when a GSTIN that is NEW to the golden-record master appears in two
    groups of the SAME upload with conflicting consignee details.

    The master auto-creates that party from the FIRST group's typed values at
    generation, then every later group snapshots those same stored values — so a
    later group's differing typed name/address is silently dropped. During
    validation the party doesn't exist yet, so `resolve_or_create(dry_run)` can't
    surface this; we detect it here. (A GSTIN already IN the master is handled by
    the per-row deviation check against the stored record.)"""
    first_row, order = _first_row_per_group(rows)
    all_keys = [key for _short, key in (*_CONSIGNEE_DIFF_TEXT, *_CONSIGNEE_DIFF_NUM)]
    in_master: dict[str, bool] = {}
    seen: dict[str, tuple[str, dict[str, str]]] = {}  # gstin -> (group, typed fields)
    warnings: list[RowError] = []

    for group in order:
        cells = first_row[group].cells
        gstin = consignee_master.normalize_gstin(cells.get("consignee_gstin", ""))
        if not valid_gstin(gstin):
            continue  # an invalid GSTIN is already a blocking error elsewhere
        if gstin not in in_master:
            in_master[gstin] = db.execute(
                select(ConsigneeParty.id).where(ConsigneeParty.gstin == gstin)
            ).scalar_one_or_none() is not None
        if in_master[gstin]:
            continue  # existing record -> per-row deviations already cover it
        typed = {key: cells.get(key, "") for key in all_keys}
        if gstin in seen:
            first_group, first_typed = seen[gstin]
            for short, stored, incoming in _consignee_field_diffs(first_typed, typed):
                warnings.append(RowError(
                    first_row[group].row_number, f"consignee_{short}",
                    f"consignee {short}: challan '{group}' entered '{incoming}', but the "
                    f"same GSTIN in challan '{first_group}' will create the golden record "
                    f"as '{stored}' — that stored value is what gets printed",
                    severity="WARNING"))
        else:
            seen[gstin] = (group, typed)
    return warnings


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
        key = c["challan_group"]
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
    result = ValidationResult(errors=structural) if structural else validate(db, rows)
    if not result.ok:
        report = _store(db, storage, _issue_report_bytes(result.issues),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        # If this is a RETRY and some challans were already issued in a prior run,
        # a now-failing re-validation (e.g. the project was set ON_HOLD, or an HSN
        # deactivated, between runs) must NOT bury those valid statutory documents
        # by zeroing the counts as FAILED_VALIDATION. Mark FAILED (retryable) with an
        # honest message and preserve the issued count instead.
        already_issued = db.execute(
            select(func.count()).select_from(Challan).where(Challan.batch_id == batch.id)
        ).scalar() or 0
        if already_issued:
            batch.status = BatchStatus.FAILED.value
            batch.message = (
                f"re-validation failed after {already_issued} challan(s) were already "
                "issued; master data may have changed — resolve it and retry")
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
        batch.status = BatchStatus.FAILED.value
        batch.message = f"numbering: {err}"
        _audit(db, "challan.batch_failed", actor_uid, batch.id, {"error": str(err)})
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
                challan = _persist_challan(db, batch, pc, alloc_id, threshold_paise, actor_uid)
                pdf = renderer.render_pdf(render.build_challan_html(_build_view(challan)))
                stored = _store(db, storage, pdf, f"{_safe(challan.number)}.pdf",
                                "challan-pdf", actor_uid, "application/pdf")
                challan.pdf_file_id = stored.id
                numbering.issue(db, _allocation(db, alloc_id), entity="challan",
                                entity_id=str(challan.id), actor_uid=actor_uid)
                db.commit()
            pdfs.append((f"{_safe(challan.number)}.pdf", _stored_bytes(db, storage, challan)))

        zip_file = _store(db, storage, render.zip_files(pdfs), f"batch-{batch.id}.zip",
                          "challan-zip", actor_uid, "application/zip")
        merged = _store(db, storage, render.merge_pdfs([p for _, p in pdfs]),
                        f"batch-{batch.id}-merged.pdf", "challan-merged", actor_uid,
                        "application/pdf")
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
    batch.message = f"generation failed: {err}"[:2000]
    _audit(db, "challan.batch_failed", actor_uid, batch.id, {"error": str(err)[:500]})
    db.commit()
    return batch


def _persist_challan(
    db: Session,
    batch: ChallanBatch,
    pc: ParsedChallan,
    alloc_id: int,
    threshold_paise: int,
    actor_uid: str | None,
) -> Challan:
    """Build the Challan + line items with fresh snapshots.

    The consignee is resolved against the GSTIN golden record HERE (the real
    auto-create), and the STORED record — not the typed values — is snapshotted, so
    an issued challan always carries the canonical party. The project is re-checked
    Active. The ship-to address is joined for the printed block.
    """
    consignor = _active_consignor(db)
    project = projects.resolve_active_project(db, pc.project_id)
    if consignor is None or project is None:  # pragma: no cover - pre-validated
        raise ChallanError("consignor/project changed after validation")
    res = consignee_master.resolve_or_create(
        db,
        gstin=pc.consignee_gstin,
        name=pc.consignee_name,
        address_line1=pc.consignee_address_line1,
        address_line2=pc.consignee_address_line2,
        pincode=pc.consignee_pincode,
        state=pc.consignee_state,
        phone=pc.consignee_phone,
        source="UPLOAD",
        actor_uid=actor_uid,
    )
    party = res.party
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
        consignor_name=consignor.name,
        consignor_gstin=consignor.gstin,
        consignor_state=consignor.state,
        consignor_address=consignor.address,
        consignor_phone=consignor.phone,
        consignee_brand="",
        consignee_name=party.name,
        consignee_gstin=party.gstin,
        consignee_state=party.state,
        consignee_address=_join_consignee(party),
        consignee_phone=party.phone,
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


def _join_consignee(party: ConsigneeParty) -> str:
    """Join the golden record's address lines + pincode into one printed block."""
    parts = [party.address_line1, party.address_line2, party.pincode]
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


def validate_batch(db: Session, batch: ChallanBatch, *, actor_uid: str | None) -> ValidationResult:
    """Parse + validate the batch's source file synchronously (no rendering).

    Sets the batch to VALIDATED or FAILED_VALIDATION (writing an issue report), and
    returns the result. No numbers are reserved and no golden records are written.
    A VALIDATED batch that carries WARNINGS still gets an issue report attached (the
    warnings) so the operator can review deviations/possible-splits before generate.
    """
    storage = get_storage()
    rows, structural = parsing.parse_workbook(_source_bytes(db, batch, storage))
    result = ValidationResult(errors=structural) if structural else validate(db, rows)
    if not result.ok:
        report = _store(db, storage, _issue_report_bytes(result.issues),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        batch.status = BatchStatus.FAILED_VALIDATION.value
        batch.message = _issue_message(result)
    else:
        if result.warnings:
            report = _store(db, storage, _issue_report_bytes(result.warnings),
                            f"batch-{batch.id}-warnings.csv", "challan-error-report",
                            actor_uid, "text/csv")
            batch.error_report_file_id = report.id
        batch.status = BatchStatus.VALIDATED.value
        batch.challan_count = len(result.challans)
        batch.line_count = sum(len(pc.lines) for pc in result.challans)
        batch.message = _issue_message(result) if result.warnings else None
    _audit(db, "challan.batch_validated", actor_uid, batch.id,
           {"status": batch.status, "errors": len(result.errors),
            "warnings": len(result.warnings)})
    db.commit()
    return result
