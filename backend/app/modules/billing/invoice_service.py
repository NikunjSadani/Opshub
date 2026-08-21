"""Client-invoice capture orchestration — upload, extract, match, review, confirm.

The SALES mirror of ``app.modules.expense.service``: a client invoice is CREATED in our
accounting software and UPLOADED here (never generated). Each PDF is run through the REUSED
pdfplumber ``Extractor`` (injectable — tests pass a fake), mapped onto a ``SalesInvoice``
snapshot + first-class lines + per-field ENVELOPE rows, then its lines are auto-matched to
the PO's OPEN line items. The whole thing carries the expense module's discipline:

* Persist under a SAVEPOINT so a UNIQUE collision that RACED our dedup pre-check (a
  concurrent upload of the same invoice, OR a same ``(client, invoice_number)``) converts
  THAT file to a DUPLICATE instead of a 500 that rolls back the whole batch (F1).
* HARD dedup on the identity key (client/buyer GSTIN | number | date | grand total) AND on
  the raw source bytes — a collision is NOT persisted; the operator deletes + re-uploads.
* A **self-GSTIN check**: the supplier on a sales invoice is US, so a ``supplier_gstin`` that
  isn't our configured ``company_gstin`` is flagged for review (this may not be our invoice).
* CONFIRM is the immutable freeze that drives §6 ``invoiced_qty`` — blocked until every line
  is matched/mapped AND every required field is present.

Errors: ``BillingError`` subclasses map to HTTP at the route (NotFound→404, BadRequest→400,
Forbidden→403, Conflict→409).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.billing import dedup, matcher
from app.modules.billing.models import (
    BillingBatch,
    BillingBatchStatus,
    LineMatchStatus,
    SalesInvoice,
    SalesInvoiceCorrection,
    SalesInvoiceField,
    SalesInvoiceLine,
    SalesInvoiceStatus,
)
from app.modules.expense.canonical import ExtractedInvoice, FieldStatus
from app.modules.expense.canonical import Field as CField
from app.modules.expense.extractor import Extractor, get_extractor
from app.modules.files.models import StoredFile
from app.modules.sales_orders.models import POLineItem
from app.platform import audit
from app.platform.models import Setting
from app.platform.storage import Storage, get_storage

logger = logging.getLogger(__name__)

MODULE_KEY = "billing"
COMPANY_GSTIN_SETTING = "company_gstin"

# Money ceiling (== challan/expense): a corrected paise amount outside +/- this is a clean
# 400, never an int8-overflow DB 500. Money is SIGNED (round_off can be negative).
_MAX_MONEY_PAISE = 10**15
_TEXT_MAXLEN = 500  # the value_raw/value_norm envelope columns are String(500)

# The identity fields whose absence/weakness a human must resolve before CONFIRM. On a
# sales invoice the CLIENT (buyer) GSTIN is the identity discriminator — the supplier GSTIN
# is our own constant (verified by the self-check, not required here).
REQUIRED_FIELD_PATHS: tuple[str, ...] = (
    "header.buyer_gstin",
    "header.invoice_number",
    "header.invoice_date",
    "totals.total_taxable_paise",
    "totals.grand_total_paise",
)

_REJECTED_MESSAGE = (
    "the document could not be read (unreadable or corrupt file) — re-scan or "
    "upload a clearer copy"
)

# States from which corrections / matching / confirm are still permitted (i.e. not the
# terminal CONFIRMED / CANCELLED / REJECTED).
_EDITABLE_STATUSES = frozenset({
    SalesInvoiceStatus.UPLOADED.value,
    SalesInvoiceStatus.EXTRACTED.value,
    SalesInvoiceStatus.NEEDS_REVIEW.value,
    SalesInvoiceStatus.NEEDS_OCR.value,
    SalesInvoiceStatus.NEEDS_MATCH.value,
    SalesInvoiceStatus.MATCHED.value,
})


# --------------------------------------------------------------------- errors

class BillingError(Exception):
    """Base for billing-flow errors."""


class BillingNotFound(BillingError):
    """A referenced invoice / PO line does not exist -> 404."""


class BillingBadRequest(BillingError):
    """A malformed correction / unknown field / invalid match target -> 400."""


class BillingConflict(BillingError):
    """A state-machine or identity conflict -> 409."""


class BillingForbidden(BillingError):
    """The actor lacks the privilege for this specific transition -> 403."""


# ----------------------------------------------------------- field mapping

_HEADER_FIELDS: tuple[str, ...] = (
    "supplier_name", "supplier_gstin", "supplier_address",
    "buyer_name", "buyer_gstin", "buyer_address",
    "invoice_number", "invoice_date", "place_of_supply", "po_ref",
)
_TOTALS_FIELDS: tuple[str, ...] = (
    "total_taxable_paise", "total_cgst_paise", "total_sgst_paise",
    "total_igst_paise", "round_off_paise", "grand_total_paise", "amount_in_words",
)

# The SUBSET of canonical attrs that map to a real SalesInvoice column (the rest live only
# as review envelope rows — the sales table stores GSTINs + number/date + the money totals).
_COLUMN_ATTRS: frozenset[str] = frozenset({
    "supplier_gstin", "buyer_gstin", "invoice_number", "invoice_date",
    "total_taxable_paise", "total_cgst_paise", "total_sgst_paise",
    "total_igst_paise", "round_off_paise", "grand_total_paise",
})

# Per-column string widths a corrected TEXT value must fit (so an over-width value is a 400
# at the seam, not a String(n) truncation / DB 500).
_COLUMN_MAXLEN: dict[str, int] = {
    "supplier_gstin": 15, "buyer_gstin": 15, "invoice_number": 120,
}


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
    """Parse a human-supplied correction string into its typed scalar value, bounding it so a
    hostile / typo'd value is a clean 400 (never a DB 500)."""
    text = raw.strip()
    if spec.kind == "money":
        try:
            value = int(text)
        except ValueError as exc:
            raise BillingBadRequest(
                f"expected an integer paise amount, got {raw!r}") from exc
        if not -_MAX_MONEY_PAISE <= value <= _MAX_MONEY_PAISE:
            raise BillingBadRequest(
                f"amount {value} is out of range (max +/-{_MAX_MONEY_PAISE} paise)")
        return value
    if spec.kind == "date":
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise BillingBadRequest(
                f"expected an ISO date (YYYY-MM-DD), got {raw!r}") from exc
    maxlen = _COLUMN_MAXLEN.get(spec.attr, _TEXT_MAXLEN)
    if len(raw) > maxlen:
        raise BillingBadRequest(
            f"value for '{spec.field_path}' is too long "
            f"(max {maxlen} characters, got {len(raw)})")
    return raw  # text — stored verbatim


# --------------------------------------------------------------- outcomes

@dataclass
class FileOutcome:
    """One uploaded file's result (surfaced per-file by the route)."""

    file_id: int
    # EXTRACTED|NEEDS_REVIEW|NEEDS_OCR|NEEDS_MATCH|MATCHED|REJECTED|DUPLICATE
    status: str
    invoice_id: int | None = None
    duplicate_of: int | None = None
    buyer_gstin: str | None = None
    invoice_number: str | None = None
    grand_total_paise: int | None = None
    review_reasons: list[str] = field(default_factory=list)
    message: str | None = None
    filename: str | None = None       # echoed by the route (the service works off file_id)


@dataclass
class BatchResult:
    batch: BillingBatch
    outcomes: list[FileOutcome] = field(default_factory=list)


# --------------------------------------------------------------- company GSTIN

def _company_gstin(db: Session) -> str | None:
    """Our configured company GSTIN (the self-check reference), or None when unset.

    Read directly off the ``Setting`` store (JSON value). A bare string or a
    ``{"gstin": ...}`` shape is tolerated; anything else / unset -> None (the self-check
    is simply skipped, never an error)."""
    row = db.get(Setting, COMPANY_GSTIN_SETTING)
    if row is None:
        return None
    value: Any = row.value
    if isinstance(value, dict):
        value = value.get("gstin")
    if not isinstance(value, str):
        return None
    norm = "".join(value.split()).upper()
    return norm or None


def _norm_gstin(gstin: str | None) -> str | None:
    if not gstin:
        return None
    return "".join(gstin.split()).upper() or None


# --------------------------------------------------------------- create_batch

def upload_invoices(
    db: Session,
    file_ids: list[int],
    *,
    client_id: int,
    po_id: int | None,
    actor_uid: str | None,
    extractor: Extractor | None = None,
) -> BatchResult:
    """Extract + persist + auto-match every uploaded client-invoice PDF into one batch.

    ``extractor`` is INJECTABLE (defaults to the configured engine) so tests pass a fake.
    Each file is processed independently: an unreadable file becomes a REJECTED invoice
    (never aborts the batch); a document whose identity collides a stored invoice yields a
    DUPLICATE outcome and is NOT persisted. The route has already validated ``client_id`` +
    ``po_id`` (exist + PO belongs to the client) and stored the blobs.
    """
    extractor = extractor or get_extractor()
    storage = get_storage()
    company_gstin = _company_gstin(db)
    batch = BillingBatch(status=BillingBatchStatus.EXTRACTING.value, created_by=actor_uid)
    db.add(batch)
    db.flush()
    _audit(db, "billing.batch_created", actor_uid, batch.id,
           {"files": len(file_ids), "client_id": client_id, "po_id": po_id})

    outcomes: list[FileOutcome] = []
    persisted = 0
    for file_id in file_ids:
        outcome = _process_file(
            db, batch, file_id, storage, extractor, actor_uid,
            client_id=client_id, po_id=po_id, company_gstin=company_gstin)
        outcomes.append(outcome)
        if outcome.invoice_id is not None and outcome.status != "DUPLICATE":
            persisted += 1

    batch.file_count = persisted
    batch.status = BillingBatchStatus.COMPLETED.value
    _audit(db, "billing.batch_completed", actor_uid, batch.id,
           {"persisted": persisted, "files": len(file_ids)})
    db.commit()
    db.refresh(batch)
    return BatchResult(batch=batch, outcomes=outcomes)


def _process_file(
    db: Session,
    batch: BillingBatch,
    file_id: int,
    storage: Storage,
    extractor: Extractor,
    actor_uid: str | None,
    *,
    client_id: int,
    po_id: int | None,
    company_gstin: str | None,
) -> FileOutcome:
    """Extract + persist + match a single file. Never raises for a bad document."""
    try:
        pdf_bytes = _read_source(db, storage, file_id)
    except FileNotFoundError:
        logger.error("billing source bytes missing for file %s", file_id)
        return _persist_rejected(db, batch, file_id, actor_uid, client_id, None)

    try:
        extracted = extractor.extract(pdf_bytes, doc_type="gst_invoice")
    except Exception as err:  # noqa: BLE001 - any engine failure -> a clean REJECTED
        logger.warning("billing extraction failed for file %s", file_id, exc_info=err)
        chash = dedup.content_hash(pdf_bytes)
        return _persist_rejected(db, batch, file_id, actor_uid, client_id, chash)

    scalars = _scalars_from(extracted)
    chash = dedup.content_hash(pdf_bytes)
    key = (dedup.dedup_key(
        scalars["buyer_gstin"], scalars["invoice_number"],
        scalars["invoice_date"], scalars["grand_total_paise"],
    ) if _enforceable(scalars) else None)
    number = _invoice_number_for(scalars, chash)

    existing = _find_duplicate(db, client_id, key, chash, number)
    if existing is not None:
        return _duplicate_outcome(db, batch, file_id, existing, actor_uid)

    try:
        with db.begin_nested():
            invoice = _persist_invoice(
                db, batch, file_id, extracted, scalars, key, chash, number, actor_uid,
                client_id=client_id, po_id=po_id, company_gstin=company_gstin)
    except IntegrityError:
        logger.info("billing dedup race for file %s; converting to DUPLICATE", file_id)
        existing = _find_duplicate(db, client_id, key, chash, number)
        if existing is None:  # a DIFFERENT integrity fault — never silently swallow it
            raise
        return _duplicate_outcome(db, batch, file_id, existing, actor_uid)
    return FileOutcome(
        file_id=file_id, status=invoice.status, invoice_id=invoice.id,
        buyer_gstin=invoice.buyer_gstin, invoice_number=invoice.invoice_number,
        grand_total_paise=invoice.grand_total_paise,
        review_reasons=list(invoice.review_reasons),
    )


def _find_duplicate(
    db: Session, client_id: int, key: str | None, chash: str, number: str,
) -> SalesInvoice | None:
    """The stored invoice this upload duplicates — by identity key, source-byte hash, OR the
    ``(client_id, invoice_number)`` uniqueness. Oldest-first so a re-upload resolves to the
    original winner."""
    conds = [SalesInvoice.content_hash == chash]
    if key is not None:
        conds.append(SalesInvoice.dedup_key == key)
    if number and not number.startswith(("UNKNOWN-", "REJECTED-")):
        conds.append(and_(
            SalesInvoice.client_id == client_id,
            SalesInvoice.invoice_number == number,
        ))
    return db.execute(
        select(SalesInvoice).where(or_(*conds)).order_by(SalesInvoice.id).limit(1)
    ).scalar_one_or_none()


def _duplicate_outcome(
    db: Session, batch: BillingBatch, file_id: int, existing: SalesInvoice,
    actor_uid: str | None,
) -> FileOutcome:
    _audit(db, "billing.invoice_duplicate", actor_uid, existing.id,
           {"file_id": file_id, "batch_id": batch.id})
    return FileOutcome(
        file_id=file_id, status="DUPLICATE", duplicate_of=existing.id,
        buyer_gstin=existing.buyer_gstin,
        invoice_number=existing.invoice_number,
        grand_total_paise=existing.grand_total_paise,
        message="a matching invoice already exists; delete it to re-upload",
    )


def _persist_invoice(
    db: Session,
    batch: BillingBatch,
    file_id: int,
    extracted: ExtractedInvoice,
    scalars: dict[str, Any],
    key: str | None,
    content_hash: str,
    number: str,
    actor_uid: str | None,
    *,
    client_id: int,
    po_id: int | None,
    company_gstin: str | None,
) -> SalesInvoice:
    """Persist the SalesInvoice snapshot + line items + field envelope rows, run the self-GSTIN
    check + the auto-matcher, and set the resulting status."""
    self_mismatch, mismatch_reason = _self_gstin_mismatch(scalars, company_gstin)
    review_reasons = list(extracted.review_reasons)
    if mismatch_reason is not None:
        review_reasons.append(mismatch_reason)

    column_values = {a: scalars[a] for a in _COLUMN_ATTRS if a in scalars}
    column_values["invoice_number"] = number  # non-null column (placeholder if unextracted)

    invoice = SalesInvoice(
        batch_id=batch.id,
        client_id=client_id,
        po_id=po_id,
        source_file_id=file_id,
        source_engine=extracted.source_engine,
        needs_ocr=extracted.needs_ocr,
        review_reasons=review_reasons,
        dedup_key=key,
        content_hash=content_hash,
        status=SalesInvoiceStatus.UPLOADED.value,  # refined below after match
        created_by=actor_uid,
        **column_values,
    )
    for spec in _FIELD_SPECS:
        cfield = _canonical_field(extracted, spec)
        invoice.fields.append(SalesInvoiceField(
            field_path=spec.field_path,
            value_norm=_norm_str(spec.kind, cfield.value_normalized),
            value_raw=cfield.value_raw,
            confidence=float(cfield.confidence),
            source_engine=cfield.source_engine,
            status=cfield.status.value,
        ))
    for i, line in enumerate(extracted.lines, start=1):
        invoice.lines.append(SalesInvoiceLine(
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

    # Propose PO-line matches, then derive the status. A self-GSTIN mismatch or a weak
    # required field parks it in NEEDS_REVIEW; otherwise the match state decides.
    matcher.match_invoice(db, invoice)
    invoice.status = _upload_status(invoice, extracted, self_mismatch)
    db.flush()

    _audit(db, "billing.invoice_created", actor_uid, invoice.id,
           {"batch_id": batch.id, "status": invoice.status, "file_id": file_id,
            "po_id": po_id})
    return invoice


def _persist_rejected(
    db: Session, batch: BillingBatch, file_id: int, actor_uid: str | None,
    client_id: int, chash: str | None,
) -> FileOutcome:
    """A quality-gate failure: a terminal REJECTED invoice with a generic message (no
    identity key, no fields). ``invoice_number`` is non-null, so a per-document placeholder
    keeps the ``(client, number)`` uniqueness from false-colliding two rejects."""
    number = f"REJECTED-{chash[:12]}" if chash else f"REJECTED-F{file_id}"
    invoice = SalesInvoice(
        batch_id=batch.id,
        client_id=client_id,
        source_file_id=file_id,
        invoice_number=number,
        content_hash=chash,
        status=SalesInvoiceStatus.REJECTED.value,
        review_reasons=[_REJECTED_MESSAGE],
        created_by=actor_uid,
    )
    db.add(invoice)
    db.flush()
    _audit(db, "billing.invoice_rejected", actor_uid, invoice.id,
           {"batch_id": batch.id, "file_id": file_id})
    return FileOutcome(
        file_id=file_id, status="REJECTED", invoice_id=invoice.id,
        message=_REJECTED_MESSAGE,
    )


def _self_gstin_mismatch(
    scalars: dict[str, Any], company_gstin: str | None,
) -> tuple[bool, str | None]:
    """The self-check: the supplier on a sales invoice must be US. Returns (mismatch, reason).

    Skipped (no mismatch) when our company GSTIN is unset or the supplier GSTIN wasn't
    extracted — the check can only fire on a definite disagreement."""
    if company_gstin is None:
        return False, None
    supplier = _norm_gstin(scalars.get("supplier_gstin"))
    if supplier is None or supplier == company_gstin:
        return False, None
    return True, (
        f"supplier GSTIN {supplier} is not our company GSTIN {company_gstin} — "
        "verify this is our invoice"
    )


def _canonical_field(extracted: ExtractedInvoice, spec: _Spec) -> CField[Any]:
    section = extracted.header if spec.section == "header" else extracted.totals
    return cast("CField[Any]", getattr(section, spec.attr))


def _scalars_from(extracted: ExtractedInvoice) -> dict[str, Any]:
    """The typed canonical scalar values keyed by canonical attr name."""
    out: dict[str, Any] = {}
    for spec in _FIELD_SPECS:
        out[spec.attr] = _canonical_field(extracted, spec).value_normalized
    return out


def _invoice_number_for(scalars: dict[str, Any], chash: str) -> str:
    """The stored ``invoice_number`` (non-null column). A missing number gets a per-document
    placeholder so the ``(client, number)`` uniqueness never false-collides two unread scans."""
    num = scalars.get("invoice_number")
    if isinstance(num, str) and num.strip():
        return num[:120]
    return f"UNKNOWN-{chash[:12]}"


def _enforceable(scalars: dict[str, Any]) -> bool:
    """The identity key only binds when all four fields are present (client GSTIN | number |
    date | grand total). A partial / OCR-parked extraction carries a NULL key."""
    return (
        bool(scalars.get("buyer_gstin"))
        and bool(scalars.get("invoice_number"))
        and scalars.get("invoice_date") is not None
        and scalars.get("grand_total_paise") is not None
    )


# --------------------------------------------------------------- status machine

def _required_weak(invoice: SalesInvoice) -> bool:
    return any(
        f.field_path in REQUIRED_FIELD_PATHS
        and f.status in (FieldStatus.MISSING.value, FieldStatus.LOW_CONFIDENCE.value)
        for f in invoice.fields
    )


def _all_lines_matched(invoice: SalesInvoice) -> bool:
    return bool(invoice.lines) and all(
        ln.match_status in (LineMatchStatus.MATCHED.value, LineMatchStatus.MANUAL.value)
        for ln in invoice.lines
    )


def _derive_status(invoice: SalesInvoice) -> str:
    """The in-review status label from the invoice's own state (fields + lines + match).

    NEEDS_OCR (a scan still missing its required fields) > NEEDS_REVIEW (a weak required
    field) > NEEDS_MATCH (a line still unmatched) > MATCHED (all lines mapped) > EXTRACTED
    (a lineless clean extraction — confirm still blocks on 'needs a line')."""
    if invoice.needs_ocr and _required_weak(invoice):
        return SalesInvoiceStatus.NEEDS_OCR.value
    if _required_weak(invoice):
        return SalesInvoiceStatus.NEEDS_REVIEW.value
    if not invoice.lines:
        return SalesInvoiceStatus.EXTRACTED.value
    if _all_lines_matched(invoice):
        return SalesInvoiceStatus.MATCHED.value
    return SalesInvoiceStatus.NEEDS_MATCH.value


def _upload_status(
    invoice: SalesInvoice, extracted: ExtractedInvoice, self_mismatch: bool,
) -> str:
    """Status at upload time. The extractor's ``review_needed`` and the self-GSTIN mismatch
    are one-shot review triggers surfaced only here (advisory, never a hard confirm block);
    thereafter ``_derive_status`` runs off the invoice's own state."""
    if extracted.needs_ocr:
        return SalesInvoiceStatus.NEEDS_OCR.value
    if extracted.review_needed or self_mismatch:
        return SalesInvoiceStatus.NEEDS_REVIEW.value
    return _derive_status(invoice)


# --------------------------------------------------------------- register / read

def _register_query(
    *,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    po_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Select[tuple[SalesInvoice]]:
    stmt = select(SalesInvoice)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            SalesInvoice.invoice_number.ilike(like),
            SalesInvoice.buyer_gstin.ilike(like),
            SalesInvoice.supplier_gstin.ilike(like),
        ))
    if status:
        stmt = stmt.where(SalesInvoice.status == status)
    if client_id is not None:
        stmt = stmt.where(SalesInvoice.client_id == client_id)
    if po_id is not None:
        stmt = stmt.where(SalesInvoice.po_id == po_id)
    if date_from is not None:
        stmt = stmt.where(SalesInvoice.invoice_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(SalesInvoice.invoice_date <= date_to)
    return stmt.order_by(SalesInvoice.id.desc())


def list_invoices(
    db: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    client_id: int | None = None,
    po_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SalesInvoice]:
    """The searchable client-invoice register (q on number/GSTINs + status/client/PO/date)."""
    stmt = _register_query(
        q=q, status=status, client_id=client_id, po_id=po_id,
        date_from=date_from, date_to=date_to,
    ).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def get_invoice(db: Session, invoice_id: int) -> SalesInvoice:
    """One invoice with its field envelope + line items. Raises ``BillingNotFound`` on a miss."""
    invoice = db.get(SalesInvoice, invoice_id)
    if invoice is None:
        raise BillingNotFound("invoice not found")
    return invoice


# --------------------------------------------------------------- corrections

def submit_corrections(
    db: Session,
    invoice: SalesInvoice,
    corrections: list[tuple[str, str]],
    *,
    actor_uid: str | None,
) -> SalesInvoice:
    """Apply per-field human corrections to an editable invoice.

    Each correction flips its ``SalesInvoiceField`` to CORRECTED (source "human"), writes a
    ``SalesInvoiceCorrection`` audit row, and (for a mapped field) overwrites the snapshot
    scalar. A CONFIRMED / CANCELLED / REJECTED invoice is immutable (409); an unknown field
    path is 400. The status is re-derived (a review cleared may reach MATCHED/NEEDS_MATCH)."""
    if invoice.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"invoice is {invoice.status}; only in-review invoices can be corrected")
    if not corrections:
        raise BillingBadRequest("no corrections supplied")

    fields_by_path = {f.field_path: f for f in invoice.fields}
    changed: list[str] = []
    for raw_path, raw_value in corrections:
        path = raw_path.strip()
        spec = _SPEC_BY_PATH.get(path)
        if spec is None:
            raise BillingBadRequest(f"unknown field '{raw_path}'")
        typed = _coerce(spec, raw_value)               # may raise BillingBadRequest
        new_norm = _norm_str(spec.kind, typed)

        fld = fields_by_path.get(path)
        old_norm = fld.value_norm if fld is not None else None
        if fld is None:
            fld = SalesInvoiceField(field_path=path)
            invoice.fields.append(fld)
        fld.value_norm = new_norm
        fld.value_raw = raw_value
        fld.status = FieldStatus.CORRECTED.value
        fld.source_engine = "human"

        if spec.attr in _COLUMN_ATTRS:
            setattr(invoice, spec.attr, typed)         # overwrite the snapshot scalar
        db.add(SalesInvoiceCorrection(
            invoice_id=invoice.id, field_path=path,
            old_value=old_norm, new_value=new_norm, actor_uid=actor_uid,
        ))
        changed.append(path)

    # Corrections can move the identity fields; keep the key + (client, number) uniqueness
    # consistent (a clash with a DIFFERENT invoice is a 409, not a surprise DB error).
    _resync_identity(db, invoice)
    invoice.status = _derive_status(invoice)

    _audit(db, "billing.invoice_corrected", actor_uid, invoice.id, {"fields": changed})
    db.commit()
    db.refresh(invoice)
    return invoice


def _resync_identity(db: Session, invoice: SalesInvoice) -> None:
    """Recompute the dedup key + re-check both uniqueness axes against OTHER invoices."""
    enforceable = (
        bool(invoice.buyer_gstin)
        and bool(invoice.invoice_number)
        and invoice.invoice_date is not None
        and invoice.grand_total_paise is not None
    )
    key = dedup.dedup_key(
        invoice.buyer_gstin, invoice.invoice_number,
        invoice.invoice_date, invoice.grand_total_paise,
    ) if enforceable else None
    if key is not None and key != invoice.dedup_key:
        clash = db.execute(
            select(SalesInvoice.id).where(
                SalesInvoice.dedup_key == key, SalesInvoice.id != invoice.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise BillingConflict(
                f"these values match an existing invoice (#{clash}); delete that one to "
                "merge, or correct the identity fields")
    num_clash = db.execute(
        select(SalesInvoice.id).where(
            SalesInvoice.client_id == invoice.client_id,
            SalesInvoice.invoice_number == invoice.invoice_number,
            SalesInvoice.id != invoice.id,
        )
    ).scalar_one_or_none()
    if num_clash is not None:
        raise BillingConflict(
            f"invoice number {invoice.invoice_number} already exists for this client "
            f"(#{num_clash})")
    invoice.dedup_key = key


# --------------------------------------------------------------- matching

def run_match(db: Session, invoice: SalesInvoice, *, actor_uid: str | None) -> SalesInvoice:
    """Re-run the auto-matcher over an editable invoice's unmatched lines, then re-derive
    the status (a fully-matched invoice reaches MATCHED)."""
    if invoice.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"invoice is {invoice.status}; matching applies only to in-review invoices")
    matcher.match_invoice(db, invoice)
    invoice.status = _derive_status(invoice)
    _audit(db, "billing.invoice_matched", actor_uid, invoice.id,
           {"matched": sum(1 for ln in invoice.lines
                           if ln.match_status != LineMatchStatus.UNMATCHED.value),
            "lines": len(invoice.lines)})
    db.commit()
    db.refresh(invoice)
    return invoice


def apply_manual_match(
    db: Session, invoice: SalesInvoice, line_id: int, po_line_item_id: int,
    *, actor_uid: str | None,
) -> SalesInvoice:
    """Map one invoice line to a specific PO line (status → MANUAL). The PO line must exist
    and belong to THIS invoice's PO (else 400); an unknown line is 404."""
    if invoice.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"invoice is {invoice.status}; matching applies only to in-review invoices")
    line = next((ln for ln in invoice.lines if ln.id == line_id), None)
    if line is None:
        raise BillingNotFound("invoice line not found")
    if invoice.po_id is None:
        raise BillingBadRequest("this invoice has no PO to match against")
    # The target PO line must belong to this invoice's PO (never cross-PO/cross-tenant).
    po_line = db.get(POLineItem, po_line_item_id)
    if po_line is None or po_line.po_id != invoice.po_id:
        raise BillingBadRequest(
            "the PO line does not exist or does not belong to this invoice's PO")

    matcher.apply_manual_match(db, line, po_line_item_id)
    invoice.status = _derive_status(invoice)
    _audit(db, "billing.invoice_manual_match", actor_uid, invoice.id,
           {"line_id": line_id, "po_line_item_id": po_line_item_id})
    db.commit()
    db.refresh(invoice)
    return invoice


# --------------------------------------------------------------- confirm

def confirm_invoice(db: Session, invoice: SalesInvoice, *, actor_uid: str | None) -> SalesInvoice:
    """Freeze an in-review invoice into the immutable CONFIRMED record (drives §6 invoiced_qty).

    Blocked (409) unless: at least one line; every REQUIRED field present (not MISSING /
    LOW_CONFIDENCE); and every line MATCHED or MANUAL (mapped to a PO line)."""
    if invoice.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"invoice is {invoice.status}; only in-review invoices can be confirmed")
    if not invoice.lines:
        raise BillingConflict("a confirmable invoice needs at least one line item")
    blocking = sorted(
        f.field_path for f in invoice.fields
        if f.field_path in REQUIRED_FIELD_PATHS
        and f.status in (FieldStatus.MISSING.value, FieldStatus.LOW_CONFIDENCE.value)
    )
    if blocking:
        raise BillingConflict(
            "these required fields still need review before confirming: "
            + ", ".join(blocking))
    unmatched = sorted(
        ln.line_no for ln in invoice.lines
        if ln.match_status not in (
            LineMatchStatus.MATCHED.value, LineMatchStatus.MANUAL.value)
    )
    if unmatched:
        raise BillingConflict(
            "match every line to a PO line before confirming; still unmatched: line(s) "
            + ", ".join(str(n) for n in unmatched))
    invoice.status = SalesInvoiceStatus.CONFIRMED.value
    invoice.confirmed_by = actor_uid
    invoice.confirmed_at = datetime.now(UTC)
    _audit(db, "billing.invoice_confirmed", actor_uid, invoice.id, {})
    db.commit()
    db.refresh(invoice)
    return invoice


# --------------------------------------------------------------- cancel / delete

def cancel_invoice(db: Session, invoice: SalesInvoice, *, actor_uid: str | None) -> SalesInvoice:
    """Soft-cancel an in-review invoice (status → CANCELLED). A CONFIRMED (immutable) record
    cannot be cancelled — it already drives the invoiced-qty rollup; use delete if truly
    needed. An already-cancelled invoice is a no-op conflict."""
    if invoice.status == SalesInvoiceStatus.CONFIRMED.value:
        raise BillingConflict(
            "a confirmed invoice is immutable and cannot be cancelled")
    if invoice.status == SalesInvoiceStatus.CANCELLED.value:
        raise BillingConflict("invoice is already cancelled")
    invoice.status = SalesInvoiceStatus.CANCELLED.value
    _audit(db, "billing.invoice_cancelled", actor_uid, invoice.id, {})
    db.commit()
    db.refresh(invoice)
    return invoice


def delete_invoice(db: Session, invoice: SalesInvoice, *, actor_uid: str | None) -> None:
    """Delete an invoice (cascades its lines / fields / corrections) + its source blob.

    Supports delete-and-re-upload to replace a hard-deduped record. A correction promoted
    into the eval gold set is training provenance — refuse to cascade it away silently."""
    invoice_id = invoice.id
    locked = db.execute(
        select(SalesInvoice).where(SalesInvoice.id == invoice_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise BillingNotFound("invoice not found")

    promoted = db.execute(
        select(SalesInvoiceCorrection.id).where(
            SalesInvoiceCorrection.invoice_id == invoice_id,
            SalesInvoiceCorrection.promoted_to_gold.is_(True),
        ).limit(1)
    ).scalar_one_or_none()
    if promoted is not None:
        raise BillingConflict(
            "this invoice has corrections promoted to the gold set and cannot be deleted "
            "(its gold provenance would be lost)")

    was_confirmed = locked.status == SalesInvoiceStatus.CONFIRMED.value
    source_file_id = locked.source_file_id
    # ORDER MATTERS (immediate FK checks on Postgres): delete + flush the invoice FIRST so
    # its FK no longer pins the StoredFile, THEN delete the StoredFile. Blob removal is
    # best-effort and happens AFTER commit (a failed unlink must never lose the delete).
    db.delete(locked)
    db.flush()

    source_ref: str | None = None
    if source_file_id is not None:
        sf = db.get(StoredFile, source_file_id)
        if sf is not None:
            source_ref = sf.storage_ref
            db.delete(sf)

    _audit(db, "billing.invoice_deleted", actor_uid, invoice_id,
           {"was_confirmed": was_confirmed, "source_file_id": source_file_id})
    db.commit()

    if source_ref is not None:
        try:
            get_storage().delete(source_ref)
        except Exception:  # noqa: BLE001 - best-effort; the DB row is already gone
            logger.warning(
                "billing source blob %s left on disk after delete of invoice %s",
                source_ref, invoice_id)


# --------------------------------------------------------------- helpers

def _read_source(db: Session, storage: Storage, file_id: int) -> bytes:
    sf = db.get(StoredFile, file_id)
    if sf is None:
        raise FileNotFoundError(f"stored file {file_id} missing")
    with storage.open(sf.storage_ref) as fh:
        return fh.read()


def _audit(
    db: Session, action: str, actor_uid: str | None, entity_id: int,
    detail: dict[str, Any],
) -> None:
    audit.log(
        db, action=action, actor_uid=actor_uid,
        entity="billing_invoice", entity_id=str(entity_id), detail=detail,
    )
