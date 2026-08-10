"""Challan orchestration — validate, then reserve -> render -> issue -> package.

The transaction discipline here is what CLOSES the numbering engine's lock-window
constraint: numbers for the whole batch are reserved and **committed in one short
transaction BEFORE any PDF is rendered**, so the counter row-lock is never held
across document generation. Rendering + issuing then run with no numbering lock.

  validate(db, rows)                 -> ValidationResult (English errors or challans)
  generate(db, batch, renderer, ...) -> reserve(commit) -> render -> issue -> zip/merge

Consignor + consignee are re-resolved from master data at generation and
SNAPSHOTTED onto each challan, so later edits never rewrite an issued document.
Untrusted cell values are escaped by the renderer (bind-as-data).
"""
from __future__ import annotations

from datetime import datetime, time
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
    ChallanView,
    LineView,
    ParsedChallan,
    ParsedLine,
    PartyView,
    RawRow,
    RowError,
    ValidationResult,
)
from app.modules.files.models import StoredFile
from app.modules.masterdata.models import Consignee, Consignor, HsnCode
from app.modules.masterdata.normalize import collapse_ws
from app.modules.numbering import service as numbering
from app.modules.numbering.models import AllocationStatus, NumberingAllocation
from app.modules.numbering.service import IST
from app.platform import audit
from app.platform.models import Setting
from app.platform.storage import Storage, get_storage

MODULE_KEY = "document_automation"
# Per-batch caps: one upload can't burn an unbounded slice of the statutory
# sequence (numbers are never reused) or schedule unbounded rendering work.
MAX_BATCH_ROWS = 5000
MAX_BATCH_CHALLANS = 2000


class ChallanError(Exception):
    """Raised for unrecoverable generation problems (e.g. unconfigured series)."""


# ------------------------------------------------------------------ validation

def validate(db: Session, rows: list[RawRow]) -> ValidationResult:
    """Structural checks (no DB) + semantic checks (master data), then group.

    Returns every error found (for a complete English report), or — when clean —
    the parsed challans grouped by the `group` key.
    """
    if len(rows) > MAX_BATCH_ROWS:
        return ValidationResult(errors=[RowError(
            0, "", f"batch has {len(rows)} rows; the per-batch limit is {MAX_BATCH_ROWS}")])
    groups = {r.cells.get("group", "") for r in rows} - {""}
    if len(groups) > MAX_BATCH_CHALLANS:
        return ValidationResult(errors=[RowError(
            0, "", f"batch has {len(groups)} challans; the limit is {MAX_BATCH_CHALLANS}")])
    errors: list[RowError] = list(parsing.structural_row_errors(rows))
    bad = {e.row_number for e in errors}

    consignors: dict[str, Consignor | None] = {}
    consignees: dict[tuple[str, str], Consignee | None] = {}
    hsns: dict[str, HsnCode | None] = {}

    for row in rows:
        if row.row_number in bad:
            continue  # can't semantically check a structurally-broken row
        c = row.cells
        if _consignor(db, c["consignor"], consignors) is None:
            errors.append(RowError(row.row_number, "consignor",
                                   f"no active consignor named '{c['consignor']}'"))
        if _consignee(db, c["brand"], c["ship_to_state"], consignees) is None:
            errors.append(RowError(row.row_number, "brand",
                                   f"no active consignee for brand '{c['brand']}' "
                                   f"in state '{c['ship_to_state']}'"))
        hsn = _hsn(db, c["hsn"], hsns)
        if hsn is None:
            errors.append(RowError(row.row_number, "hsn",
                                   f"unknown or inactive HSN '{c['hsn']}'"))
        elif c.get("gst_rate"):
            rate = parsing.parse_qty(c["gst_rate"])
            if rate is not None and hsn.gst_rate is not None and rate != hsn.gst_rate:
                errors.append(RowError(row.row_number, "gst_rate",
                                       f"gst_rate {rate} does not match HSN {hsn.hsn} "
                                       f"rate {hsn.gst_rate}"))
        _amount_sanity(row, errors)

    if errors:
        errors.sort(key=lambda e: (e.row_number, e.column))
        return ValidationResult(errors=errors)
    return ValidationResult(challans=_build_challans(rows))


def _amount_sanity(row: RawRow, errors: list[RowError]) -> None:
    """If rate is numeric, amount should be ~ rate*qty (skipped for text rates)."""
    c = row.cells
    _, rate_paise = parsing.parse_rate(c.get("rate", ""))
    qty = parsing.parse_qty(c["quantity"])
    amount = parsing.parse_paise(c["amount"])
    if rate_paise is None or qty is None or amount is None or qty <= 0:
        return
    expected = int((Decimal(rate_paise) * qty).to_integral_value(rounding=ROUND_HALF_UP))
    tol = max(100, int(Decimal(expected) * Decimal("0.01")))
    if abs(amount - expected) > tol:
        errors.append(RowError(row.row_number, "amount",
                               f"amount {amount / 100:.2f} != rate x qty "
                               f"{expected / 100:.2f}"))


def _consignor(db: Session, name: str, cache: dict[str, Consignor | None]) -> Consignor | None:
    # Match case-insensitively (mirrors how master data normalizes on write) so a
    # spreadsheet's "gifsy depot" resolves the "Gifsy Depot" entity.
    key = collapse_ws(name).lower()
    if key not in cache:
        cache[key] = db.execute(
            select(Consignor).where(
                func.lower(func.trim(Consignor.name)) == key,
                Consignor.active.is_(True),
            )
        ).scalar_one_or_none()
    return cache[key]


def _consignee(
    db: Session, brand: str, state: str, cache: dict[tuple[str, str], Consignee | None]
) -> Consignee | None:
    key = (collapse_ws(brand).lower(), collapse_ws(state).lower())
    if key not in cache:
        cache[key] = db.execute(
            select(Consignee).where(
                func.lower(func.trim(Consignee.brand)) == key[0],
                func.lower(func.trim(Consignee.state)) == key[1],
                Consignee.active.is_(True),
            )
        ).scalar_one_or_none()
    return cache[key]


def _hsn(db: Session, hsn: str, cache: dict[str, HsnCode | None]) -> HsnCode | None:
    if hsn not in cache:
        cache[hsn] = db.execute(
            select(HsnCode).where(HsnCode.hsn == hsn, HsnCode.active.is_(True))
        ).scalar_one_or_none()
    return cache[hsn]


def _build_challans(rows: list[RawRow]) -> list[ParsedChallan]:
    """Group clean rows by `group` (first-seen order) into ParsedChallans."""
    groups: dict[str, ParsedChallan] = {}
    order: list[str] = []
    for row in rows:
        c = row.cells
        key = c["group"]
        if key not in groups:
            challan_date = parsing.parse_date(c["challan_date"])
            if challan_date is None:  # pragma: no cover - structurally pre-validated
                raise ChallanError(f"unparseable challan_date on row {row.row_number}")
            groups[key] = ParsedChallan(
                group_key=key,
                consignor_name=c["consignor"],
                brand=c["brand"],
                ship_to_name=c["ship_to_name"],
                ship_to_address=c["ship_to_address"],
                ship_to_state=c["ship_to_state"],
                challan_date=challan_date,
                po_number=c.get("po_number", ""),
                invoice_number=c.get("invoice_number", ""),
            )
            order.append(key)
        pc = groups[key]
        rate_text, rate_paise = parsing.parse_rate(c.get("rate", ""))
        gst_rate = parsing.parse_qty(c["gst_rate"]) if c.get("gst_rate") else None
        pc.lines.append(ParsedLine(
            description=c["description"],
            hsn=c["hsn"],
            quantity=parsing.parse_qty(c["quantity"]) or Decimal(0),
            uom=c.get("uom") or "NOS",
            rate_text=rate_text,
            rate_paise=rate_paise,
            amount_paise=parsing.parse_paise(c["amount"]) or 0,
            gst_rate=gst_rate,
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

    On validation failure: writes an English error report and marks the batch
    FAILED_VALIDATION — no numbers are reserved. On success: every challan is
    ISSUED with a bound number and a stored PDF, plus a ZIP and a merged PDF.
    """
    storage = get_storage()
    rows, structural = parsing.parse_workbook(_source_bytes(db, batch, storage))
    result = ValidationResult(errors=structural) if structural else validate(db, rows)
    if not result.ok:
        report = _store(db, storage, _error_report_bytes(result.errors),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        batch.status = BatchStatus.FAILED_VALIDATION.value
        batch.challan_count = 0  # a re-validation failure invalidates the earlier counts
        batch.line_count = 0
        batch.message = f"{len(result.errors)} validation error(s)"
        _audit(db, "challan.batch_failed_validation", actor_uid, batch.id,
               {"errors": len(result.errors)})
        db.commit()
        return batch

    batch.status = BatchStatus.GENERATING.value
    db.flush()

    # --- RESERVE phase: one short transaction, COMMITTED before any render. ---
    reservations: list[tuple[ParsedChallan, int]] = []
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
    """Build the Challan + line items with fresh master-data snapshots."""
    consignor = _consignor(db, pc.consignor_name, {})
    consignee = _consignee(db, pc.brand, pc.ship_to_state, {})
    if consignor is None or consignee is None:  # pragma: no cover - pre-validated
        raise ChallanError("master data changed after validation")
    alloc = _allocation(db, alloc_id)
    challan = Challan(
        batch_id=batch.id,
        allocation_id=alloc.id,
        number=alloc.formatted,
        series=alloc.series,
        fy=alloc.fy,
        number_int=alloc.number,
        challan_date=pc.challan_date,
        consignor_name=consignor.name,
        consignor_gstin=consignor.gstin,
        consignor_state=consignor.state,
        consignor_address=consignor.address,
        consignee_brand=consignee.brand,
        consignee_name=consignee.name,
        consignee_gstin=consignee.gstin,
        consignee_state=consignee.state,
        consignee_address=consignee.address,
        ship_to_name=pc.ship_to_name,
        ship_to_address=pc.ship_to_address,
        ship_to_state=pc.ship_to_state,
        po_number=pc.po_number,
        invoice_number=pc.invoice_number,
        eway_required=pc.total_paise >= threshold_paise > 0,
        total_paise=pc.total_paise,
        status=ChallanStatus.ISSUED.value,
        created_by=actor_uid,
    )
    for i, line in enumerate(pc.lines, start=1):
        challan.lines.append(ChallanLineItem(
            line_no=i,
            description=line.description,
            hsn=line.hsn,
            quantity=line.quantity,
            uom=line.uom,
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
    """E-way threshold in paise. Tolerates "50,000"/float; defaults if unusable,
    so a bad/missing setting never crashes generation nor silently disables e-way."""
    rupees = Decimal(_DEFAULT_EWAY_RUPEES)
    setting = db.get(Setting, "eway_threshold")
    if setting is not None and isinstance(setting.value, dict):
        raw = str(setting.value.get("amount", _DEFAULT_EWAY_RUPEES)).replace(",", "")
        try:
            rupees = Decimal(raw)
        except (InvalidOperation, ValueError):
            rupees = Decimal(_DEFAULT_EWAY_RUPEES)
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
    """
    value = value.replace('"', "'")
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return f'"{value}"'


def _error_report_bytes(errors: list[RowError]) -> bytes:
    lines = ["Row,Column,Problem"]
    for e in errors:
        lines.append(f"{e.row_number},{_csv_field(e.column)},{_csv_field(e.message)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_view(ch: Challan) -> ChallanView:
    return ChallanView(
        number=ch.number,
        challan_date=ch.challan_date.strftime("%d-%m-%Y"),
        consignor=PartyView(ch.consignor_name, ch.consignor_gstin,
                            ch.consignor_state, ch.consignor_address),
        consignee=PartyView(ch.consignee_name, ch.consignee_gstin,
                            ch.consignee_state, ch.consignee_address),
        ship_to_name=ch.ship_to_name,
        ship_to_address=ch.ship_to_address,
        ship_to_state=ch.ship_to_state,
        po_number=ch.po_number,
        invoice_number=ch.invoice_number,
        eway_required=ch.eway_required,
        lines=[
            LineView(
                line_no=line.line_no,
                description=line.description,
                hsn=line.hsn,
                quantity=_qty(line.quantity),
                uom=line.uom,
                rate=line.rate_text or _rupees(line.rate_paise),
                amount=_rupees(line.amount_paise),
                gst_rate=("" if line.gst_rate is None else f"{line.gst_rate}"),
            )
            for line in ch.lines
        ],
        total_amount=_rupees(ch.total_paise),
    )


def _qty(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _rupees(paise: int | None) -> str:
    if paise is None:
        return ""
    return f"{Decimal(paise) / 100:,.2f}"  # Decimal, not float — statutory money display


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

    Sets the batch to VALIDATED or FAILED_VALIDATION (writing an error report),
    and returns the result. No numbers are reserved.
    """
    storage = get_storage()
    rows, structural = parsing.parse_workbook(_source_bytes(db, batch, storage))
    result = ValidationResult(errors=structural) if structural else validate(db, rows)
    if not result.ok:
        report = _store(db, storage, _error_report_bytes(result.errors),
                        f"batch-{batch.id}-errors.csv", "challan-error-report",
                        actor_uid, "text/csv")
        batch.error_report_file_id = report.id
        batch.status = BatchStatus.FAILED_VALIDATION.value
        batch.message = f"{len(result.errors)} validation error(s)"
    else:
        batch.status = BatchStatus.VALIDATED.value
        batch.challan_count = len(result.challans)
        batch.line_count = sum(len(pc.lines) for pc in result.challans)
        batch.message = None
    _audit(db, "challan.batch_validated", actor_uid, batch.id,
           {"status": batch.status, "errors": len(result.errors)})
    db.commit()
    return result
