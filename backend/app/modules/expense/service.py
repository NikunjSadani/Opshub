"""Expense/Invoice orchestration — extract, persist, review, confirm, delete.

The upload flow mirrors the challan module's discipline (store bytes, then run the
engine, then persist a SNAPSHOT), but the money here is READ from a vendor PDF, so
the seam is the ``Extractor`` (injectable — tests pass a fake) rather than a
renderer. For each uploaded PDF:

  read bytes -> extractor.extract -> map ExtractedInvoice onto an Invoice snapshot
  (scalars) + first-class line items + the per-field ENVELOPE rows, deriving the
  invoice STATUS from the extraction (EXTRACTED / NEEDS_REVIEW / NEEDS_OCR), or
  REJECTED when the engine can't read the document at all.

Dedup is HARD: a new invoice whose identity key (``dedup.dedup_key``) collides a
stored one is NOT persisted — the per-file outcome carries the existing invoice's
id + summary so the route can surface a 409, and the operator deletes + re-uploads
to replace. Every mutation is audited.

Errors: ``ExpenseError`` subclasses map to HTTP status at the route
(``ExpenseNotFound`` -> 404, ``ExpenseBadRequest`` -> 400, ``ExpenseConflict`` -> 409).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.expense import dedup
from app.modules.expense.canonical import (
    REQUIRED_FIELD_PATHS,
    ExtractedInvoice,
)
from app.modules.expense.canonical import (
    Field as CField,
)
from app.modules.expense.extractor import Extractor, get_extractor
from app.modules.expense.models import (
    ExpensePaymentMethod,
    FieldStatus,
    Invoice,
    InvoiceBatch,
    InvoiceBatchStatus,
    InvoiceCorrection,
    InvoiceField,
    InvoiceLineItem,
    InvoiceStatus,
)
from app.modules.files.models import StoredFile
from app.modules.projects.models import Project
from app.platform import audit
from app.platform.storage import Storage, get_storage

logger = logging.getLogger(__name__)

MODULE_KEY = "expense_invoice"
# Hard ceiling on a single CSV export so the response can never be unbounded.
MAX_CSV_ROWS = 50000

# Mirror of challan.parsing._MAX_MONEY_PAISE (= Rs 10,000,000,000,000). A corrected
# money amount outside +/- this ceiling is a 400, never an int8-overflow DB 500. Money
# is SIGNED (round_off can be negative), so the floor is the negated ceiling.
_MAX_MONEY_PAISE = 10**15

# Per-column string widths (from models.Invoice) a corrected TEXT field must fit, so an
# over-width value is a 400 at the seam rather than a String(n) truncation/DB 500.
_COLUMN_MAXLEN: dict[str, int] = {
    "supplier_name": 300, "supplier_gstin": 15, "supplier_address": 600,
    "buyer_name": 300, "buyer_gstin": 15, "buyer_address": 600,
    "invoice_number": 64, "place_of_supply": 64, "po_ref": 64,
    "amount_in_words": 600,
}

_REJECTED_MESSAGE = (
    "the document could not be read (unreadable or corrupt file) — re-scan or "
    "upload a clearer copy"
)

# inc 28 (vendor credit notes): a captured document is either an INVOICE (a cost) or a
# CREDIT_NOTE (a REDUCTION of cost). Every money aggregate must be SIGN-AWARE — a credit
# note subtracts. INVOICE is the default so all pre-inc-28 rows behave byte-identically.
DOC_TYPE_INVOICE = "INVOICE"
DOC_TYPE_CREDIT_NOTE = "CREDIT_NOTE"
DOC_TYPES = frozenset({DOC_TYPE_INVOICE, DOC_TYPE_CREDIT_NOTE})


# --------------------------------------------------------------------- errors

class ExpenseError(Exception):
    """Base for expense-flow errors."""


class ExpenseNotFound(ExpenseError):
    """A referenced invoice/entity does not exist -> 404."""


class ExpenseBadRequest(ExpenseError):
    """A malformed correction / unknown field -> 400."""


class ExpenseConflict(ExpenseError):
    """A state-machine or identity conflict -> 409."""


class ExpenseForbidden(ExpenseError):
    """The actor lacks the privilege for this specific transition -> 403."""


# ----------------------------------------------------------- field mapping

# The canonical HEADER + TOTALS scalars carried as per-field ENVELOPE rows (lines
# are first-class, not envelopes). The dataclass attr name == the Invoice scalar
# attr name == the trailing segment of the field_path, so one list drives the map.
_HEADER_FIELDS: tuple[str, ...] = (
    "supplier_name", "supplier_gstin", "supplier_address",
    "buyer_name", "buyer_gstin", "buyer_address",
    "invoice_number", "invoice_date", "place_of_supply", "po_ref",
)
_TOTALS_FIELDS: tuple[str, ...] = (
    "total_taxable_paise", "total_cgst_paise", "total_sgst_paise",
    "total_igst_paise", "round_off_paise", "grand_total_paise", "amount_in_words",
)


@dataclass(frozen=True)
class _Spec:
    field_path: str
    section: str   # "header" | "totals"
    attr: str
    kind: str      # "money" | "date" | "text"


def _kind_for(attr: str) -> str:
    if attr == "invoice_date":
        return "date"
    if attr.endswith("_paise"):
        return "money"
    return "text"


_FIELD_SPECS: tuple[_Spec, ...] = tuple(
    _Spec(f"header.{a}", "header", a, _kind_for(a)) for a in _HEADER_FIELDS
) + tuple(
    _Spec(f"totals.{a}", "totals", a, _kind_for(a)) for a in _TOTALS_FIELDS
)
_SPEC_BY_PATH: dict[str, _Spec] = {s.field_path: s for s in _FIELD_SPECS}


def _norm_str(kind: str, value: Any) -> str | None:
    """The stored string form of an envelope value (paise->int-str, date->ISO)."""
    if value is None:
        return None
    if kind == "date":
        return cast("date", value).isoformat()
    return str(value)


def _coerce(spec: _Spec, raw: str) -> Any:
    """Parse a human-supplied correction string into its typed scalar value.

    Bounds are enforced HERE so a hostile/typo'd value is a clean 400, never a DB
    500: money is clamped to +/- ``_MAX_MONEY_PAISE`` (the int8 column), and a text
    value must fit its target column width (``_COLUMN_MAXLEN``)."""
    text = raw.strip()
    if spec.kind == "money":
        try:
            value = int(text)
        except ValueError as exc:
            raise ExpenseBadRequest(
                f"expected an integer paise amount, got {raw!r}") from exc
        if not -_MAX_MONEY_PAISE <= value <= _MAX_MONEY_PAISE:
            raise ExpenseBadRequest(
                f"amount {value} is out of range (max +/-{_MAX_MONEY_PAISE} paise)")
        return value
    if spec.kind == "date":
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ExpenseBadRequest(
                f"expected an ISO date (YYYY-MM-DD), got {raw!r}") from exc
    maxlen = _COLUMN_MAXLEN.get(spec.attr)
    if maxlen is not None and len(raw) > maxlen:
        raise ExpenseBadRequest(
            f"value for '{spec.field_path}' is too long "
            f"(max {maxlen} characters, got {len(raw)})")
    return raw  # text — stored verbatim


# --------------------------------------------------------------- outcomes

@dataclass
class FileOutcome:
    """One uploaded file's result (surfaced per-file by the route)."""

    file_id: int
    status: str                       # EXTRACTED|NEEDS_REVIEW|NEEDS_OCR|REJECTED|DUPLICATE
    invoice_id: int | None = None
    duplicate_of: int | None = None   # the EXISTING invoice id on a DUPLICATE
    supplier_name: str | None = None
    invoice_number: str | None = None
    grand_total_paise: int | None = None
    review_reasons: list[str] = field(default_factory=list)  # extraction review flags
    message: str | None = None
    # Echoed by the route from the stored upload (the service works off file_id).
    filename: str | None = None


@dataclass
class BatchResult:
    """The persisted batch plus the per-file outcomes (duplicates are NOT persisted,
    so they live only here — the route reads them straight off this result)."""

    batch: InvoiceBatch
    outcomes: list[FileOutcome] = field(default_factory=list)


# --------------------------------------------------------------- create_batch

def create_batch(
    db: Session,
    file_ids: list[int],
    *,
    project_id: int,
    payment_method_id: int,
    actor_uid: str | None,
    doc_type: str = DOC_TYPE_INVOICE,
    against_invoice_id: int | None = None,
    extractor: Extractor | None = None,
) -> BatchResult:
    """Extract + persist every uploaded PDF into one batch; return per-file outcomes.

    ``extractor`` is INJECTABLE (defaults to the configured engine) so tests pass a
    fake. Each file is processed independently: an unreadable file becomes a
    REJECTED invoice (never aborts the batch); a document whose identity key
    collides a stored invoice yields a DUPLICATE outcome and is NOT persisted.

    ``project_id`` + ``payment_method_id`` are the cost allocation (inc 27), stamped
    on EVERY invoice the batch creates (rejected rows included) — the route validates
    they exist + are active before we get here.

    ``doc_type`` (INVOICE | CREDIT_NOTE) + the optional ``against_invoice_id`` are the
    inc-28 credit-note fields, likewise stamped on EVERY invoice in the batch (rejected
    included). The route validates ``doc_type`` and that any ``against_invoice_id`` exists
    + shares the batch's project before we get here.
    """
    extractor = extractor or get_extractor()
    storage = get_storage()
    batch = InvoiceBatch(status=InvoiceBatchStatus.EXTRACTING.value, created_by=actor_uid)
    db.add(batch)
    db.flush()
    _audit(db, "expense.batch_created", actor_uid, batch.id,
           {"files": len(file_ids), "project_id": project_id,
            "payment_method_id": payment_method_id,
            "doc_type": doc_type, "against_invoice_id": against_invoice_id})

    outcomes: list[FileOutcome] = []
    persisted = 0
    for file_id in file_ids:
        outcome = _process_file(
            db, batch, file_id, storage, extractor, actor_uid,
            project_id=project_id, payment_method_id=payment_method_id,
            doc_type=doc_type, against_invoice_id=against_invoice_id)
        outcomes.append(outcome)
        if outcome.invoice_id is not None and outcome.status != "DUPLICATE":
            persisted += 1

    batch.invoice_count = persisted
    batch.status = InvoiceBatchStatus.COMPLETED.value
    _audit(db, "expense.batch_completed", actor_uid, batch.id,
           {"persisted": persisted, "files": len(file_ids)})
    db.commit()
    db.refresh(batch)
    return BatchResult(batch=batch, outcomes=outcomes)


def _process_file(
    db: Session,
    batch: InvoiceBatch,
    file_id: int,
    storage: Storage,
    extractor: Extractor,
    actor_uid: str | None,
    *,
    project_id: int,
    payment_method_id: int,
    doc_type: str = DOC_TYPE_INVOICE,
    against_invoice_id: int | None = None,
) -> FileOutcome:
    """Extract + persist a single file, returning its outcome. Never raises for a
    bad document: an extraction failure becomes a REJECTED invoice."""
    try:
        pdf_bytes = _read_source(db, storage, file_id)
    except FileNotFoundError:
        # The row was created microseconds ago in the same request; a missing blob
        # is an internal fault, not a bad document. Log server-side, reject the file.
        logger.error("expense source bytes missing for file %s", file_id)
        return _persist_rejected(
            db, batch, file_id, actor_uid,
            project_id=project_id, payment_method_id=payment_method_id,
            doc_type=doc_type, against_invoice_id=against_invoice_id)

    try:
        extracted = extractor.extract(pdf_bytes, doc_type="gst_invoice")
    except Exception as err:  # noqa: BLE001 - any engine failure -> a clean REJECTED
        # Detail (which may embed file internals) is logged, NEVER surfaced.
        logger.warning("expense extraction failed for file %s", file_id, exc_info=err)
        return _persist_rejected(
            db, batch, file_id, actor_uid,
            project_id=project_id, payment_method_id=payment_method_id,
            doc_type=doc_type, against_invoice_id=against_invoice_id)

    scalars = _scalars_from(extracted)
    # Vendor credit note: the supplier GSTIN can only be checked AFTER extraction (the
    # upload endpoint validates same-project, but the vendor is unknown pre-extraction).
    # A CREDIT_NOTE whose supplier GSTIN differs from the invoice it credits is a SOFT
    # review flag (never a hard reject — the doc is already captured).
    if doc_type == DOC_TYPE_CREDIT_NOTE and against_invoice_id is not None:
        against = db.get(Invoice, against_invoice_id)
        this_supplier = (scalars.get("supplier_gstin") or "").strip().upper()
        ref_supplier = (against.supplier_gstin or "").strip().upper() if against else ""
        if against is not None and this_supplier and ref_supplier and this_supplier != ref_supplier:
            extracted.review_reasons.append(
                "credit-note supplier GSTIN differs from the referenced invoice")
            extracted.review_needed = True
    key = (dedup.dedup_key(
        scalars["supplier_gstin"], scalars["invoice_number"],
        scalars["invoice_date"], scalars["grand_total_paise"],
    ) if _enforceable(scalars) else None)
    chash = dedup.content_hash(pdf_bytes)

    # Fast path: a stored invoice with the same identity key (F2) OR the same source
    # bytes (F4 — a byte-identical re-upload of an un-OCR'd scan whose key is NULL) is
    # a DUPLICATE; do NOT persist.
    existing = _find_duplicate(db, key, chash)
    if existing is not None:
        return _duplicate_outcome(db, batch, file_id, existing, actor_uid)

    # Persist under a SAVEPOINT so a UNIQUE(dedup_key) collision that RACED our check (a
    # concurrent upload of the same invoice) converts THIS file to a DUPLICATE outcome
    # instead of surfacing an uncaught IntegrityError -> 500 that rolls back the whole
    # batch and loses the earlier good files (F1).
    try:
        with db.begin_nested():
            invoice = _persist_invoice(
                db, batch, file_id, extracted, scalars, key, chash, actor_uid,
                project_id=project_id, payment_method_id=payment_method_id,
                doc_type=doc_type, against_invoice_id=against_invoice_id)
    except IntegrityError:
        logger.info("expense dedup race for file %s; converting to DUPLICATE", file_id)
        existing = _find_duplicate(db, key, chash)
        if existing is None:  # a DIFFERENT integrity fault — never silently swallow it
            raise
        return _duplicate_outcome(db, batch, file_id, existing, actor_uid)
    return FileOutcome(
        file_id=file_id, status=invoice.status, invoice_id=invoice.id,
        supplier_name=invoice.supplier_name, invoice_number=invoice.invoice_number,
        grand_total_paise=invoice.grand_total_paise,
        review_reasons=list(extracted.review_reasons),
    )


def _find_duplicate(db: Session, key: str | None, chash: str) -> Invoice | None:
    """The stored invoice this upload duplicates (by identity key OR source-byte hash),
    or None. Oldest-first so a re-upload always resolves to the original winner."""
    conds = [Invoice.content_hash == chash]
    if key is not None:
        conds.append(Invoice.dedup_key == key)
    return db.execute(
        select(Invoice).where(or_(*conds)).order_by(Invoice.id).limit(1)
    ).scalar_one_or_none()


def _duplicate_outcome(
    db: Session, batch: InvoiceBatch, file_id: int, existing: Invoice,
    actor_uid: str | None,
) -> FileOutcome:
    """A DUPLICATE per-file outcome carrying the STORED winner's id + summary (audited)."""
    _audit(db, "expense.invoice_duplicate", actor_uid, existing.id,
           {"file_id": file_id, "batch_id": batch.id})
    return FileOutcome(
        file_id=file_id, status="DUPLICATE", duplicate_of=existing.id,
        supplier_name=existing.supplier_name,
        invoice_number=existing.invoice_number,
        grand_total_paise=existing.grand_total_paise,
        message="a matching invoice already exists; delete it to re-upload",
    )


def _persist_invoice(
    db: Session,
    batch: InvoiceBatch,
    file_id: int,
    extracted: ExtractedInvoice,
    scalars: dict[str, Any],
    key: str | None,
    content_hash: str,
    actor_uid: str | None,
    *,
    project_id: int,
    payment_method_id: int,
    doc_type: str = DOC_TYPE_INVOICE,
    against_invoice_id: int | None = None,
) -> Invoice:
    """Persist the Invoice snapshot + line items + field envelope rows."""
    invoice = Invoice(
        batch_id=batch.id,
        source_file_id=file_id,
        schema_version=extracted.schema_version,
        source_engine=extracted.source_engine,
        status=_status_for(extracted),
        needs_ocr=extracted.needs_ocr,
        review_reasons=list(extracted.review_reasons),
        dedup_key=key,
        content_hash=content_hash,
        project_id=project_id,
        payment_method_id=payment_method_id,
        doc_type=doc_type,
        against_invoice_id=against_invoice_id,
        created_by=actor_uid,
        **scalars,
    )
    for spec in _FIELD_SPECS:
        cfield = _canonical_field(extracted, spec)
        invoice.fields.append(InvoiceField(
            field_path=spec.field_path,
            value_normalized=_norm_str(spec.kind, cfield.value_normalized),
            value_raw=cfield.value_raw,
            confidence=_confidence(cfield.confidence),
            source_engine=cfield.source_engine,
            page=cfield.page,
            bbox=list(cfield.bbox) if cfield.bbox is not None else None,
            status=cfield.status.value,
        ))
    for i, line in enumerate(extracted.lines, start=1):
        invoice.lines.append(InvoiceLineItem(
            line_no=i,
            description=line.description.value_normalized,
            hsn_sac=line.hsn_sac.value_normalized,
            quantity=line.quantity.value_normalized,
            unit=line.unit.value_normalized,
            unit_rate_paise=line.unit_rate_paise.value_normalized,
            taxable_paise=line.taxable_paise.value_normalized,
            gst_rate=line.gst_rate.value_normalized,
            cgst_paise=line.cgst_paise.value_normalized,
            sgst_paise=line.sgst_paise.value_normalized,
            igst_paise=line.igst_paise.value_normalized,
            line_total_paise=line.line_total_paise.value_normalized,
        ))
    db.add(invoice)
    db.flush()
    _audit(db, "expense.invoice_created", actor_uid, invoice.id,
           {"batch_id": batch.id, "status": invoice.status, "file_id": file_id})
    return invoice


def _persist_rejected(
    db: Session, batch: InvoiceBatch, file_id: int, actor_uid: str | None,
    *, project_id: int, payment_method_id: int,
    doc_type: str = DOC_TYPE_INVOICE, against_invoice_id: int | None = None,
) -> FileOutcome:
    """A quality-gate failure: a terminal REJECTED invoice with a generic message
    (no identity key, no fields — there was nothing readable to snapshot). The batch's
    cost allocation + credit-note stamping are still applied so the whole upload carries
    them (rejected included), mirroring the inc-27 project/payment stamping."""
    invoice = Invoice(
        batch_id=batch.id,
        source_file_id=file_id,
        status=InvoiceStatus.REJECTED.value,
        review_reasons=[_REJECTED_MESSAGE],
        project_id=project_id,
        payment_method_id=payment_method_id,
        doc_type=doc_type,
        against_invoice_id=against_invoice_id,
        created_by=actor_uid,
    )
    db.add(invoice)
    db.flush()
    _audit(db, "expense.invoice_rejected", actor_uid, invoice.id,
           {"batch_id": batch.id, "file_id": file_id})
    return FileOutcome(
        file_id=file_id, status="REJECTED", invoice_id=invoice.id,
        message=_REJECTED_MESSAGE,
    )


def _canonical_field(extracted: ExtractedInvoice, spec: _Spec) -> CField[Any]:
    section = extracted.header if spec.section == "header" else extracted.totals
    return cast("CField[Any]", getattr(section, spec.attr))


def _scalars_from(extracted: ExtractedInvoice) -> dict[str, Any]:
    """The typed canonical scalar values keyed by their Invoice column name."""
    out: dict[str, Any] = {}
    for spec in _FIELD_SPECS:
        out[spec.attr] = _canonical_field(extracted, spec).value_normalized
    return out


def _enforceable(scalars: dict[str, Any]) -> bool:
    """A dedup key only binds when all four identity fields are present. A partial /
    OCR-parked extraction carries a NULL key so it never falsely collides another."""
    return (
        bool(scalars.get("supplier_gstin"))
        and bool(scalars.get("invoice_number"))
        and scalars.get("invoice_date") is not None
        and scalars.get("grand_total_paise") is not None
    )


def _status_for(extracted: ExtractedInvoice) -> str:
    """Map the extraction onto the invoice state machine (REJECTED is handled by the
    quality-gate path, never reached here)."""
    if extracted.needs_ocr:
        return InvoiceStatus.NEEDS_OCR.value
    if extracted.review_needed:
        return InvoiceStatus.NEEDS_REVIEW.value
    return InvoiceStatus.EXTRACTED.value


def _confidence(value: float) -> Decimal:
    """Clamp + quantize a 0..1 confidence into the Numeric(4,3) column."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        dec = Decimal("0")
    dec = max(Decimal("0"), min(Decimal("1"), dec))
    return dec.quantize(Decimal("0.001"))


# --------------------------------------------------------------- register / read

def _register_query(
    *,
    q: str | None = None,
    supplier: str | None = None,
    gstin: str | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: int | None = None,
    payment_method_id: int | None = None,
    doc_type: str | None = None,
) -> Select[tuple[Invoice]]:
    """The filtered, newest-first register select shared by list + CSV."""
    stmt = select(Invoice)
    if q and q.strip():
        # Free-text search the FE sends as `q`: ORs supplier / GSTIN / invoice no. so a
        # single box matches any of them (without it the search returned the full list).
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Invoice.supplier_name.ilike(like),
            Invoice.supplier_gstin.ilike(like),
            Invoice.invoice_number.ilike(like),
        ))
    if supplier:
        stmt = stmt.where(Invoice.supplier_name.ilike(f"%{supplier.strip()}%"))
    if gstin:
        stmt = stmt.where(Invoice.supplier_gstin == "".join(gstin.split()).upper())
    if status:
        stmt = stmt.where(Invoice.status == status)
    if date_from is not None:
        stmt = stmt.where(Invoice.invoice_date >= date_from)
    if date_to is not None:  # inclusive
        stmt = stmt.where(Invoice.invoice_date <= date_to)
    if project_id is not None:
        stmt = stmt.where(Invoice.project_id == project_id)
    if payment_method_id is not None:
        stmt = stmt.where(Invoice.payment_method_id == payment_method_id)
    if doc_type is not None:
        stmt = stmt.where(Invoice.doc_type == doc_type)
    return stmt.order_by(Invoice.id.desc())


def list_invoices(
    db: Session,
    *,
    q: str | None = None,
    supplier: str | None = None,
    gstin: str | None = None,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: int | None = None,
    payment_method_id: int | None = None,
    doc_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Invoice]:
    """The searchable expense register (free-text q / supplier / GSTIN / date-range /
    status / project / payment method / doc_type)."""
    stmt = _register_query(
        q=q, supplier=supplier, gstin=gstin, status=status,
        date_from=date_from, date_to=date_to,
        project_id=project_id, payment_method_id=payment_method_id,
        doc_type=doc_type,
    ).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def allocation_maps(
    db: Session, invoices: list[Invoice]
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    """(project_id -> code, project_id -> name, payment_method_id -> name) for a page.

    The project code/name is resolved by an EXPLICIT lookup — the expense module keeps NO
    ORM relationship to Project (module decoupling) — and each map is one query (no per-row
    N+1). Ids with no row (e.g. a deleted method) simply drop out."""
    project_ids = {inv.project_id for inv in invoices if inv.project_id is not None}
    pm_ids = {
        inv.payment_method_id for inv in invoices if inv.payment_method_id is not None
    }
    project_codes: dict[int, str] = {}
    project_names: dict[int, str] = {}
    if project_ids:
        for row in db.execute(
            select(Project.id, Project.code, Project.name).where(Project.id.in_(project_ids))
        ):
            project_codes[row.id] = row.code
            project_names[row.id] = row.name
    methods: dict[int, str] = (
        {row.id: row.name for row in db.execute(
            select(ExpensePaymentMethod.id, ExpensePaymentMethod.name)
            .where(ExpensePaymentMethod.id.in_(pm_ids))
        )} if pm_ids else {}
    )
    return project_codes, project_names, methods


def register_csv(
    rows: list[Invoice],
    project_codes: dict[int, str] | None = None,
    payment_method_names: dict[int, str] | None = None,
) -> bytes:
    """Render the register to CSV; text-derived fields are CSV-injection-guarded.

    ``project_codes`` / ``payment_method_names`` resolve the allocation columns (from
    ``allocation_maps``); an unmapped id renders blank."""
    project_codes = project_codes or {}
    payment_method_names = payment_method_names or {}
    header = (
        "Invoice No,Date,Supplier,Supplier GSTIN,Doc Type,Project,Payment Method,"
        "Taxable (INR),Grand Total (INR),Status"
    )
    lines = [header]
    for inv in rows:
        project = project_codes.get(inv.project_id) if inv.project_id is not None else ""
        method = (
            payment_method_names.get(inv.payment_method_id)
            if inv.payment_method_id is not None else ""
        )
        # Sign-aware money: a CREDIT_NOTE is a REDUCTION, so its amounts render NEGATIVE —
        # a spreadsheet that sums the Taxable / Grand Total columns then yields the NET
        # (invoices minus credit notes), matching the /expense/summary dashboard.
        sign = -1 if inv.doc_type == DOC_TYPE_CREDIT_NOTE else 1
        lines.append(",".join((
            _csv_field(inv.invoice_number or ""),
            f'"{inv.invoice_date.isoformat() if inv.invoice_date else ""}"',
            _csv_field(inv.supplier_name or ""),
            _csv_field(inv.supplier_gstin or ""),
            f'"{inv.doc_type}"',
            _csv_field(project or ""),
            _csv_field(method or ""),
            f'"{_rupees_signed(inv.total_taxable_paise, sign)}"',
            f'"{_rupees_signed(inv.grand_total_paise, sign)}"',
            f'"{inv.status}"',
        )))
    return ("\n".join(lines) + "\n").encode("utf-8")


def get_invoice(db: Session, invoice_id: int) -> Invoice:
    """One invoice with its field envelope + line items (relationships eager-load
    on access). Raises ``ExpenseNotFound`` for a miss."""
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise ExpenseNotFound("invoice not found")
    return invoice


# --------------------------------------------------------------- summary

@dataclass
class GroupTotal:
    """One aggregation bucket: the grouping key (project or payment method), its
    human label, the summed grand total in paise, and the invoice count. `sub_label`
    is an optional secondary label (the project NAME alongside its code); None for
    payment methods (which have only a name)."""

    key_id: int | None
    label: str | None
    total_paise: int
    count: int
    sub_label: str | None = None


@dataclass
class ExpenseSummary:
    total_confirmed_paise: int
    invoice_count: int
    by_project: list[GroupTotal]
    by_payment_method: list[GroupTotal]


def _signed_paise(column: Any) -> Any:
    """A SIGN-AWARE money expression: a CREDIT_NOTE (a reduction) contributes the
    NEGATED amount, an INVOICE its plain amount. Summing this yields the NET spend.

    When no credit notes exist every row is an INVOICE, so the CASE returns the plain
    column and every sum is byte-identical to the pre-inc-28 behaviour."""
    return case((Invoice.doc_type == DOC_TYPE_CREDIT_NOTE, -column), else_=column)


def summary(db: Session) -> ExpenseSummary:
    """Cost-allocation rollup over CONFIRMED invoices only.

    Only CONFIRMED (the immutable financial record) invoices count; every other state
    (UPLOADED/EXTRACTED/NEEDS_REVIEW/NEEDS_OCR/REJECTED) is excluded — an unconfirmed
    row is not yet a committed expense. Money is summed in BigInt paise. A confirmed
    row missing a project/method (a pre-inc-27 edge that predates the confirm guard)
    falls into a NULL bucket so the group totals still reconcile to the grand total.

    inc 28: the money sum is SIGN-AWARE — a CREDIT_NOTE SUBTRACTS (net =
    Σ INVOICE − Σ CREDIT_NOTE). ``count`` is the raw number of confirmed documents
    (invoices + credit notes) in the bucket, so a credit note reduces the spend but is
    still a visible document. The by-project + by-payment groups each net independently,
    so they still reconcile to the net grand total."""
    confirmed = Invoice.status == InvoiceStatus.CONFIRMED.value
    net_grand = func.coalesce(func.sum(_signed_paise(Invoice.grand_total_paise)), 0)
    total_paise, count = db.execute(
        select(net_grand, func.count()).where(confirmed)
    ).one()

    by_project = [
        GroupTotal(key_id=pid, label=code, sub_label=name, total_paise=int(paise), count=cnt)
        for pid, code, name, paise, cnt in db.execute(
            select(
                Invoice.project_id,
                Project.code,
                Project.name,
                net_grand,
                func.count(),
            )
            .outerjoin(Project, Project.id == Invoice.project_id)
            .where(confirmed)
            .group_by(Invoice.project_id, Project.code, Project.name)
            .order_by(net_grand.desc())
        )
    ]
    by_payment_method = [
        GroupTotal(key_id=mid, label=name, total_paise=int(paise), count=cnt)
        for mid, name, paise, cnt in db.execute(
            select(
                Invoice.payment_method_id,
                ExpensePaymentMethod.name,
                net_grand,
                func.count(),
            )
            .outerjoin(
                ExpensePaymentMethod,
                ExpensePaymentMethod.id == Invoice.payment_method_id,
            )
            .where(confirmed)
            .group_by(Invoice.payment_method_id, ExpensePaymentMethod.name)
            .order_by(net_grand.desc())
        )
    ]
    return ExpenseSummary(
        total_confirmed_paise=int(total_paise),
        invoice_count=int(count),
        by_project=by_project,
        by_payment_method=by_payment_method,
    )


# --------------------------------------------------------------- corrections

def submit_corrections(
    db: Session,
    invoice: Invoice,
    corrections: list[tuple[str, str]],
    *,
    actor_uid: str | None,
) -> Invoice:
    """Apply per-field human corrections to an EXTRACTED / NEEDS_REVIEW invoice.

    Each correction flips its ``InvoiceField`` to CORRECTED (source_engine="human"),
    writes an ``InvoiceCorrection`` audit row, and overwrites the canonical scalar.
    A CONFIRMED / REJECTED invoice is immutable (409); an unknown field path is 400.
    """
    if invoice.status not in (
        InvoiceStatus.EXTRACTED.value, InvoiceStatus.NEEDS_REVIEW.value
    ):
        raise ExpenseConflict(
            f"invoice is {invoice.status}; only EXTRACTED or NEEDS_REVIEW invoices "
            "can be corrected")
    if not corrections:
        raise ExpenseBadRequest("no corrections supplied")

    fields_by_path = {f.field_path: f for f in invoice.fields}
    changed: list[str] = []
    for raw_path, raw_value in corrections:
        path = raw_path.strip()
        spec = _SPEC_BY_PATH.get(path)
        if spec is None:
            raise ExpenseBadRequest(f"unknown field '{raw_path}'")
        typed = _coerce(spec, raw_value)               # may raise ExpenseBadRequest
        new_norm = _norm_str(spec.kind, typed)

        fld = fields_by_path.get(path)
        old_norm = fld.value_normalized if fld is not None else None
        if fld is None:  # envelope row absent (a REJECTED-style skeleton) — create it
            fld = InvoiceField(field_path=path)
            invoice.fields.append(fld)
        fld.value_normalized = new_norm
        fld.status = FieldStatus.CORRECTED.value
        fld.source_engine = "human"

        setattr(invoice, spec.attr, typed)             # overwrite the snapshot scalar
        db.add(InvoiceCorrection(
            invoice_id=invoice.id, field_path=path,
            old_value=old_norm, new_value=new_norm, corrected_by=actor_uid,
        ))
        changed.append(path)

    # Corrections can move the identity fields; keep the enforceable key in sync so a
    # later confirm/register stays consistent. A collision with a DIFFERENT invoice is
    # a 409 (delete the other or fix the value) rather than a surprise DB error.
    _resync_dedup_key(db, invoice)

    _audit(db, "expense.invoice_corrected", actor_uid, invoice.id,
           {"fields": changed})
    db.commit()
    db.refresh(invoice)
    return invoice


def _resync_dedup_key(db: Session, invoice: Invoice) -> None:
    enforceable = (
        bool(invoice.supplier_gstin)
        and bool(invoice.invoice_number)
        and invoice.invoice_date is not None
        and invoice.grand_total_paise is not None
    )
    key = dedup.dedup_key(
        invoice.supplier_gstin, invoice.invoice_number,
        invoice.invoice_date, invoice.grand_total_paise,
    ) if enforceable else None
    if key == invoice.dedup_key:
        return
    if key is not None:
        clash = db.execute(
            select(Invoice.id).where(Invoice.dedup_key == key, Invoice.id != invoice.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise ExpenseConflict(
                f"these values match an existing invoice (#{clash}); delete that one "
                "to merge, or correct the identity fields")
    invoice.dedup_key = key


# --------------------------------------------------------------- confirm

def confirm_invoice(db: Session, invoice: Invoice, *, actor_uid: str | None) -> Invoice:
    """Freeze an EXTRACTED / NEEDS_REVIEW invoice into the immutable CONFIRMED record.

    Blocked (409) while any REQUIRED field is still MISSING / LOW_CONFIDENCE — those
    must be corrected (which flips them to CORRECTED) first.
    """
    if invoice.status not in (
        InvoiceStatus.EXTRACTED.value, InvoiceStatus.NEEDS_REVIEW.value
    ):
        raise ExpenseConflict(
            f"invoice is {invoice.status}; only EXTRACTED or NEEDS_REVIEW invoices "
            "can be confirmed")
    # Cost-allocation guard (inc 27): a CONFIRMED invoice is the immutable financial
    # record, so it must carry a project + payment method. New uploads always have them;
    # this blocks confirming a pre-inc-27 / edge row that predates the allocation.
    if invoice.project_id is None or invoice.payment_method_id is None:
        raise ExpenseConflict(
            "allocate a project and payment method before confirming")
    blocking = sorted(
        f.field_path for f in invoice.fields
        if f.field_path in REQUIRED_FIELD_PATHS
        and f.status in (FieldStatus.MISSING.value, FieldStatus.LOW_CONFIDENCE.value)
    )
    if blocking:
        raise ExpenseConflict(
            "these required fields still need review before confirming: "
            + ", ".join(blocking))
    # A real GST invoice has at least one line; the FE already requires one to enable
    # Confirm, so require it here too (L1) — otherwise a lineless invoice is API-
    # confirmable yet UI-blocked, an inconsistency.
    if not invoice.lines:
        raise ExpenseConflict(
            "a confirmable invoice needs at least one line item")
    invoice.status = InvoiceStatus.CONFIRMED.value
    invoice.confirmed_by = actor_uid
    invoice.confirmed_at = datetime.now(UTC)
    _audit(db, "expense.invoice_confirmed", actor_uid, invoice.id, {})
    db.commit()
    db.refresh(invoice)
    return invoice


# --------------------------------------------------------------- delete

def delete_invoice(
    db: Session, invoice: Invoice, *, actor_uid: str | None,
    can_delete_confirmed: bool,
) -> None:
    """Delete an invoice (cascades its lines / fields / corrections) + its source blob.

    Supports delete-and-re-upload to replace a hard-deduped record. Deleting a CONFIRMED
    record requires ``can_delete_confirmed`` (the route derives it from the admin
    ``expense.delete`` action), re-checked HERE under a row lock so a confirm that RACES
    the route's gate can't let a non-admin delete a now-CONFIRMED record (F5)."""
    invoice_id = invoice.id
    # F5: re-read the status under a row lock; a concurrent confirm can no longer slip
    # between the route's pre-check and this delete.
    locked = db.execute(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ExpenseNotFound("invoice not found")
    was_confirmed = locked.status == InvoiceStatus.CONFIRMED.value
    if was_confirmed and not can_delete_confirmed:
        raise ExpenseForbidden(
            "deleting a confirmed invoice requires admin (expense.delete)")

    # F7: a correction promoted into the eval gold set is training provenance — refuse to
    # cascade it away silently (an admin can re-point the gold set first if truly needed).
    promoted = db.execute(
        select(func.count()).select_from(InvoiceCorrection).where(
            InvoiceCorrection.invoice_id == invoice_id,
            InvoiceCorrection.promoted_to_gold.is_(True),
        )
    ).scalar_one()
    if promoted:
        raise ExpenseConflict(
            "this invoice has corrections promoted to the gold set and cannot be "
            "deleted (its gold provenance would be lost)")

    # F6: the source blob + its StoredFile row are NOT FK-cascaded — remove them too so a
    # delete doesn't orphan the PDF forever. ORDER MATTERS: the Invoice FK-references the
    # StoredFile, and there is no ORM relationship for the unit-of-work to order by, so we
    # delete the invoice + FLUSH first (dropping the FK) BEFORE deleting the StoredFile —
    # else Postgres (immediate FK checks) aborts the whole delete. Blob removal is
    # best-effort and happens AFTER the commit (a failed unlink must never lose the delete).
    source_file_id = locked.source_file_id
    db.delete(locked)
    db.flush()  # invoice gone -> its FK no longer pins the StoredFile

    source_ref: str | None = None
    if source_file_id is not None:
        sf = db.get(StoredFile, source_file_id)
        if sf is not None:
            source_ref = sf.storage_ref
            db.delete(sf)

    _audit(db, "expense.invoice_deleted", actor_uid, invoice_id,
           {"was_confirmed": was_confirmed, "source_file_id": source_file_id})
    db.commit()

    if source_ref is not None:
        try:
            get_storage().delete(source_ref)
        except Exception:  # noqa: BLE001 - best-effort; the DB row is already gone
            logger.warning(
                "expense source blob %s left on disk after delete of invoice %s",
                source_ref, invoice_id)


# --------------------------------------------------------------- helpers

def _read_source(db: Session, storage: Storage, file_id: int) -> bytes:
    sf = db.get(StoredFile, file_id)
    if sf is None:
        raise FileNotFoundError(f"stored file {file_id} missing")
    with storage.open(sf.storage_ref) as fh:
        return fh.read()


def _rupees(paise: int | None) -> str:
    """Paise -> a plain 2dp rupee decimal a spreadsheet can sum; "" when absent."""
    if paise is None:
        return ""
    return f"{Decimal(paise) / 100:.2f}"


def _rupees_signed(paise: int | None, sign: int) -> str:
    """Paise -> a signed 2dp rupee decimal ("" when absent). ``sign`` is -1 for a
    CREDIT_NOTE (a reduction) so the CSV money column sums to the NET."""
    if paise is None:
        return ""
    return f"{Decimal(sign * paise) / 100:.2f}"


def _csv_field(value: str) -> str:
    """Quote a CSV field and neutralize spreadsheet formula/DDE injection (RFC 4180
    quote-doubling; a leading = + - @ / control char is prefixed with `'`)."""
    value = value.replace('"', '""')
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return f'"{value}"'


def _audit(
    db: Session, action: str, actor_uid: str | None, entity_id: int,
    detail: dict[str, Any],
) -> None:
    audit.log(
        db, action=action, actor_uid=actor_uid,
        entity="expense_invoice", entity_id=str(entity_id), detail=detail,
    )
