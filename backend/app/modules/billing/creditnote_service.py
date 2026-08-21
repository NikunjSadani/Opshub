"""Client credit-note capture orchestration — the CLONE of ``invoice_service`` against the
``billing_credit_note`` tables.

A client credit note is CREATED in our accounting software (to REDUCE a client invoice) and
UPLOADED here. Each PDF is run through the REUSED pdfplumber ``Extractor`` (injectable — tests
pass a fake), mapped onto a ``CreditNote`` header snapshot + first-class lines, then each line
is auto-matched to a PO line on the CREDITED invoice's ``po_id``. It carries the invoice lane's
discipline verbatim:

* Persist under a SAVEPOINT so a UNIQUE collision that RACED our dedup pre-check converts THAT
  file to a DUPLICATE instead of a 500 that rolls back the batch.
* HARD dedup on the identity key (client/buyer GSTIN | cn number | cn date | grand total) AND on
  the raw source bytes — a collision is NOT persisted; the operator deletes + re-uploads.
* CONFIRM is the immutable freeze that drives §6 ``invoiced_qty`` (a confirmed CN REOPENS billed
  units): blocked until the CREDITED invoice is itself CONFIRMED, every line is matched/mapped,
  and every required header field is present.

Unlike ``SalesInvoice`` a ``CreditNote`` has NO per-field envelope table — the header scalars are
persisted directly and "review" edits them via a simple corrections PATCH + line matching. The
status is derived from the extraction (needs_ocr / review_needed) plus the match state.

Errors: ``BillingError`` subclasses map to HTTP at the route (NotFound→404, BadRequest→400,
Forbidden→403, Conflict→409) — the SAME hierarchy the invoice lane raises (re-exported here).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.billing import dedup, matcher
from app.modules.billing.invoice_service import (
    BillingBadRequest,
    BillingConflict,
    BillingError,
    BillingForbidden,
    BillingNotFound,
)
from app.modules.billing.models import (
    CreditNote,
    CreditNoteLine,
    CreditNoteStatus,
    LineMatchStatus,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceStatus,
)
from app.modules.expense.canonical import ExtractedInvoice
from app.modules.expense.canonical import Field as CField
from app.modules.expense.extractor import Extractor, get_extractor
from app.modules.files.models import StoredFile
from app.modules.sales_orders.models import POLineItem
from app.platform import audit
from app.platform.storage import Storage, get_storage

logger = logging.getLogger(__name__)

MODULE_KEY = "billing"

# Money ceiling (== invoice lane): a corrected paise amount outside +/- this is a clean 400,
# never an int8-overflow DB 500. Money is SIGNED (round_off can be negative).
_MAX_MONEY_PAISE = 10**15

_REJECTED_MESSAGE = (
    "the document could not be read (unreadable or corrupt file) — re-scan or "
    "upload a clearer copy"
)

# States from which corrections / matching / confirm are still permitted (not terminal).
_EDITABLE_STATUSES = frozenset({
    CreditNoteStatus.UPLOADED.value,
    CreditNoteStatus.EXTRACTED.value,
    CreditNoteStatus.NEEDS_REVIEW.value,
    CreditNoteStatus.NEEDS_OCR.value,
    CreditNoteStatus.NEEDS_MATCH.value,
    CreditNoteStatus.MATCHED.value,
})

# Re-export the shared error hierarchy so route + tests can catch off THIS module too.
__all__ = [
    "BillingBadRequest",
    "BillingConflict",
    "BillingError",
    "BillingForbidden",
    "BillingNotFound",
    "FileOutcome",
    "apply_manual_cn_match",
    "cancel_credit_note",
    "confirm_credit_note",
    "delete_credit_note",
    "get_credit_note",
    "list_credit_notes",
    "match_credit_note",
    "run_cn_match",
    "submit_cn_corrections",
    "upload_credit_notes",
]


# ------------------------------------------------------------- correctable fields

@dataclass(frozen=True)
class _Spec:
    attr: str
    kind: str      # "money" | "date" | "text"
    maxlen: int = 500


# The header scalars a human may correct during review (no field envelope — direct columns).
_CN_FIELD_SPECS: tuple[_Spec, ...] = (
    _Spec("cn_number", "text", 120),
    _Spec("cn_date", "date"),
    _Spec("total_taxable_paise", "money"),
    _Spec("total_cgst_paise", "money"),
    _Spec("total_sgst_paise", "money"),
    _Spec("total_igst_paise", "money"),
    _Spec("round_off_paise", "money"),
    _Spec("grand_total_paise", "money"),
    _Spec("reason", "text", 500),
)
_SPEC_BY_ATTR: dict[str, _Spec] = {s.attr: s for s in _CN_FIELD_SPECS}

# Required header scalars whose absence BLOCKS confirm (and parks the CN in NEEDS_REVIEW).
_REQUIRED_ATTRS: tuple[str, ...] = (
    "cn_number", "cn_date", "total_taxable_paise", "grand_total_paise",
)


def _coerce(spec: _Spec, raw: str) -> Any:
    """Parse a human-supplied correction string into its typed scalar, bounding it so a hostile
    / typo'd value is a clean 400 (never a DB 500)."""
    text = raw.strip()
    if spec.kind == "money":
        try:
            value = int(text)
        except ValueError as exc:
            raise BillingBadRequest(
                f"expected an integer paise amount for '{spec.attr}', got {raw!r}") from exc
        if not -_MAX_MONEY_PAISE <= value <= _MAX_MONEY_PAISE:
            raise BillingBadRequest(
                f"amount {value} is out of range (max +/-{_MAX_MONEY_PAISE} paise)")
        return value
    if spec.kind == "date":
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise BillingBadRequest(
                f"expected an ISO date (YYYY-MM-DD) for '{spec.attr}', got {raw!r}") from exc
    if len(raw) > spec.maxlen:
        raise BillingBadRequest(
            f"value for '{spec.attr}' is too long (max {spec.maxlen} characters, got {len(raw)})")
    return raw  # text — stored verbatim (empty string allowed; required-check catches blanks)


# --------------------------------------------------------------------- outcomes

@dataclass
class FileOutcome:
    """One uploaded credit-note file's result (surfaced per-file by the route)."""

    file_id: int
    status: str                       # EXTRACTED|NEEDS_*|MATCHED|REJECTED|DUPLICATE
    cn_id: int | None = None
    cn_number: str | None = None
    duplicate_of: int | None = None
    grand_total_paise: int | None = None
    message: str | None = None


# --------------------------------------------------------------- canonical mapping

def _field_value(extracted: ExtractedInvoice, section: str, attr: str) -> Any:
    src = extracted.header if section == "header" else extracted.totals
    return cast("CField[Any]", getattr(src, attr)).value_normalized


def _scalars_from(extracted: ExtractedInvoice) -> dict[str, Any]:
    """The typed canonical scalars a CreditNote persists (+ the buyer GSTIN used only to key
    the dedup fingerprint — it is NOT a stored column)."""
    return {
        "client_gstin": _field_value(extracted, "header", "buyer_gstin"),
        "cn_number": _field_value(extracted, "header", "invoice_number"),
        "cn_date": _field_value(extracted, "header", "invoice_date"),
        "total_taxable_paise": _field_value(extracted, "totals", "total_taxable_paise"),
        "total_cgst_paise": _field_value(extracted, "totals", "total_cgst_paise"),
        "total_sgst_paise": _field_value(extracted, "totals", "total_sgst_paise"),
        "total_igst_paise": _field_value(extracted, "totals", "total_igst_paise"),
        "round_off_paise": _field_value(extracted, "totals", "round_off_paise"),
        "grand_total_paise": _field_value(extracted, "totals", "grand_total_paise"),
    }


def _cn_number_for(scalars: dict[str, Any], chash: str) -> str:
    """The stored ``cn_number`` (non-null column). A missing number gets a per-document
    placeholder so the ``(client, cn_number)`` uniqueness never false-collides two unread scans."""
    num = scalars.get("cn_number")
    if isinstance(num, str) and num.strip():
        return num[:120]
    return f"UNKNOWN-{chash[:12]}"


def _enforceable(scalars: dict[str, Any]) -> bool:
    """The identity key binds only when all four fields are present."""
    return (
        bool(scalars.get("client_gstin"))
        and bool(scalars.get("cn_number"))
        and scalars.get("cn_date") is not None
        and scalars.get("grand_total_paise") is not None
    )


# --------------------------------------------------------------------- upload

def upload_credit_notes(
    db: Session,
    file_ids: list[int],
    *,
    invoice_id: int,
    client_id: int,
    actor_uid: str | None,
    extractor: Extractor | None = None,
) -> list[FileOutcome]:
    """Extract + persist + auto-match every uploaded credit-note PDF against ``invoice_id``.

    ``extractor`` is INJECTABLE (defaults to the configured engine) so tests pass a fake. Each
    file is processed independently: an unreadable file becomes a REJECTED CN (never aborts the
    batch); a document whose identity collides a stored CN yields a DUPLICATE outcome and is NOT
    persisted. The route has already validated ``invoice_id`` (exists) + resolved ``client_id``
    from it, and stored the blobs.
    """
    extractor = extractor or get_extractor()
    storage = get_storage()
    outcomes: list[FileOutcome] = []
    for file_id in file_ids:
        outcomes.append(_process_file(
            db, file_id, storage, extractor, actor_uid,
            invoice_id=invoice_id, client_id=client_id))
    db.commit()
    return outcomes


def _process_file(
    db: Session,
    file_id: int,
    storage: Storage,
    extractor: Extractor,
    actor_uid: str | None,
    *,
    invoice_id: int,
    client_id: int,
) -> FileOutcome:
    """Extract + persist + match a single file. Never raises for a bad document."""
    try:
        pdf_bytes = _read_source(db, storage, file_id)
    except FileNotFoundError:
        logger.error("credit-note source bytes missing for file %s", file_id)
        return _persist_rejected(db, file_id, actor_uid, invoice_id, client_id, None)

    try:
        extracted = extractor.extract(pdf_bytes, doc_type="gst_invoice")
    except Exception as err:  # noqa: BLE001 - any engine failure -> a clean REJECTED
        logger.warning("credit-note extraction failed for file %s", file_id, exc_info=err)
        chash = dedup.content_hash(pdf_bytes)
        return _persist_rejected(db, file_id, actor_uid, invoice_id, client_id, chash)

    scalars = _scalars_from(extracted)
    chash = dedup.content_hash(pdf_bytes)
    key = (dedup.dedup_key(
        scalars["client_gstin"], scalars["cn_number"],
        scalars["cn_date"], scalars["grand_total_paise"],
    ) if _enforceable(scalars) else None)
    number = _cn_number_for(scalars, chash)

    existing = _find_duplicate(db, client_id, key, chash, number)
    if existing is not None:
        return _duplicate_outcome(db, file_id, existing, actor_uid)

    try:
        with db.begin_nested():
            cn = _persist_cn(
                db, file_id, extracted, scalars, key, chash, number, actor_uid,
                invoice_id=invoice_id, client_id=client_id)
    except IntegrityError:
        logger.info("credit-note dedup race for file %s; converting to DUPLICATE", file_id)
        existing = _find_duplicate(db, client_id, key, chash, number)
        if existing is None:  # a DIFFERENT integrity fault — never silently swallow it
            raise
        return _duplicate_outcome(db, file_id, existing, actor_uid)
    return FileOutcome(
        file_id=file_id, status=cn.status, cn_id=cn.id,
        cn_number=cn.cn_number, grand_total_paise=cn.grand_total_paise)


def _find_duplicate(
    db: Session, client_id: int, key: str | None, chash: str, number: str,
) -> CreditNote | None:
    """The stored CN this upload duplicates — by identity key, source-byte hash, OR the
    ``(client_id, cn_number)`` uniqueness. Oldest-first so a re-upload resolves to the original."""
    conds = [CreditNote.content_hash == chash]
    if key is not None:
        conds.append(CreditNote.dedup_key == key)
    if number and not number.startswith(("UNKNOWN-", "REJECTED-")):
        conds.append(and_(
            CreditNote.client_id == client_id,
            CreditNote.cn_number == number,
        ))
    return db.execute(
        select(CreditNote).where(or_(*conds)).order_by(CreditNote.id).limit(1)
    ).scalar_one_or_none()


def _duplicate_outcome(
    db: Session, file_id: int, existing: CreditNote, actor_uid: str | None,
) -> FileOutcome:
    _audit(db, "billing.credit_note_duplicate", actor_uid, existing.id, {"file_id": file_id})
    return FileOutcome(
        file_id=file_id, status="DUPLICATE", duplicate_of=existing.id,
        cn_number=existing.cn_number, grand_total_paise=existing.grand_total_paise,
        message="a matching credit note already exists; delete it to re-upload")


def _persist_cn(
    db: Session,
    file_id: int,
    extracted: ExtractedInvoice,
    scalars: dict[str, Any],
    key: str | None,
    content_hash: str,
    number: str,
    actor_uid: str | None,
    *,
    invoice_id: int,
    client_id: int,
) -> CreditNote:
    """Persist the CreditNote header snapshot + line items, run the auto-matcher, set status."""
    cn = CreditNote(
        invoice_id=invoice_id,
        client_id=client_id,
        source_file_id=file_id,
        cn_number=number,
        cn_date=scalars.get("cn_date"),
        total_taxable_paise=scalars.get("total_taxable_paise"),
        total_cgst_paise=scalars.get("total_cgst_paise"),
        total_sgst_paise=scalars.get("total_sgst_paise"),
        total_igst_paise=scalars.get("total_igst_paise"),
        round_off_paise=scalars.get("round_off_paise"),
        grand_total_paise=scalars.get("grand_total_paise"),
        source_engine=extracted.source_engine,
        dedup_key=key,
        content_hash=content_hash,
        status=CreditNoteStatus.UPLOADED.value,  # refined below after match
        created_by=actor_uid,
    )
    for i, line in enumerate(extracted.lines, start=1):
        cn.lines.append(CreditNoteLine(
            line_no=i,
            description=line.description.value_normalized,
            quantity=line.quantity.value_normalized,
            taxable_paise=line.taxable_paise.value_normalized,
            line_total_paise=line.line_total_paise.value_normalized,
        ))
    db.add(cn)
    db.flush()

    match_credit_note(db, cn)
    cn.status = _upload_status(cn, extracted)
    db.flush()

    _audit(db, "billing.credit_note_created", actor_uid, cn.id,
           {"status": cn.status, "file_id": file_id, "invoice_id": invoice_id})
    return cn


def _persist_rejected(
    db: Session, file_id: int, actor_uid: str | None,
    invoice_id: int, client_id: int, chash: str | None,
) -> FileOutcome:
    """A quality-gate failure: a terminal REJECTED CN with a generic message (no identity key,
    no lines). ``cn_number`` is non-null, so a per-document placeholder keeps the
    ``(client, cn_number)`` uniqueness from false-colliding two rejects."""
    number = f"REJECTED-{chash[:12]}" if chash else f"REJECTED-F{file_id}"
    cn = CreditNote(
        invoice_id=invoice_id,
        client_id=client_id,
        source_file_id=file_id,
        cn_number=number,
        content_hash=chash,
        status=CreditNoteStatus.REJECTED.value,
        reason=_REJECTED_MESSAGE,
        created_by=actor_uid,
    )
    db.add(cn)
    db.flush()
    _audit(db, "billing.credit_note_rejected", actor_uid, cn.id, {"file_id": file_id})
    return FileOutcome(
        file_id=file_id, status="REJECTED", cn_id=cn.id, cn_number=number,
        message=_REJECTED_MESSAGE)


# --------------------------------------------------------------- status machine

def _required_missing(cn: CreditNote) -> list[str]:
    """Required header scalars still absent (MISSING or corrected-to-blank) — these BLOCK confirm
    and, at upload/derive time, park the CN in NEEDS_REVIEW."""
    missing: list[str] = []
    number = cn.cn_number or ""
    if not number.strip() or number.startswith(("UNKNOWN-", "REJECTED-")):
        missing.append("cn_number")
    if cn.cn_date is None:
        missing.append("cn_date")
    if cn.total_taxable_paise is None:
        missing.append("total_taxable_paise")
    if cn.grand_total_paise is None:
        missing.append("grand_total_paise")
    return missing


def _all_lines_matched(cn: CreditNote) -> bool:
    return bool(cn.lines) and all(
        ln.match_status in (LineMatchStatus.MATCHED.value, LineMatchStatus.MANUAL.value)
        for ln in cn.lines
    )


def _derive_status(cn: CreditNote) -> str:
    """The in-review status label from the CN's own state (required scalars + lines + match)."""
    if _required_missing(cn):
        return CreditNoteStatus.NEEDS_REVIEW.value
    if not cn.lines:
        return CreditNoteStatus.EXTRACTED.value
    if _all_lines_matched(cn):
        return CreditNoteStatus.MATCHED.value
    return CreditNoteStatus.NEEDS_MATCH.value


def _upload_status(cn: CreditNote, extracted: ExtractedInvoice) -> str:
    """Status at upload time. The extractor's ``needs_ocr`` / ``review_needed`` are one-shot
    triggers surfaced only here; thereafter ``_derive_status`` runs off the CN's own state."""
    if extracted.needs_ocr:
        return CreditNoteStatus.NEEDS_OCR.value
    if extracted.review_needed:
        return CreditNoteStatus.NEEDS_REVIEW.value
    return _derive_status(cn)


# --------------------------------------------------------------- matching

def match_credit_note(db: Session, cn: CreditNote) -> None:
    """Auto-match every UNMATCHED line of ``cn`` to a PO line on the CREDITED invoice's ``po_id``.

    Reuses the invoice matcher's deterministic scoring (word-boundary identity, 0.60 threshold,
    greedy 1:1). A CreditNoteLine has only description + quantity, so identity/price fall back to
    description/quantity agreement (a transient ``SalesInvoiceLine`` probe carries the CN line's
    fields into ``matcher._score`` — the same shape the invoice lane scores). Idempotent: lines
    already MATCHED/MANUAL are left untouched. With no credited invoice / no PO, nothing matches.
    """
    invoice = db.get(SalesInvoice, cn.invoice_id)
    if invoice is None or invoice.po_id is None:
        return
    pending = [ln for ln in cn.lines if ln.match_status == LineMatchStatus.UNMATCHED.value]
    if not pending:
        return

    candidates = matcher._load_candidates(db, invoice.po_id)
    taken: set[int] = {
        ln.po_line_item_id for ln in cn.lines if ln.po_line_item_id is not None
    }

    scored: list[tuple[float, int, int, CreditNoteLine]] = []
    for ln in pending:
        probe = SalesInvoiceLine(
            description=ln.description, hsn_sac=None,
            quantity=ln.quantity, unit_rate_paise=None)
        for cand in candidates:
            s = matcher._score(probe, cand)
            if s >= matcher.MATCH_THRESHOLD:
                scored.append((s, ln.line_no, cand.po_line_id, ln))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    used_lines: set[int] = set()
    for _s, _no, po_line_id, ln in scored:
        if ln.id in used_lines or po_line_id in taken:
            continue
        ln.po_line_item_id = po_line_id
        ln.match_status = LineMatchStatus.MATCHED.value
        used_lines.add(ln.id)
        taken.add(po_line_id)


def run_cn_match(db: Session, cn: CreditNote, *, actor_uid: str | None) -> CreditNote:
    """Re-run the auto-matcher over an editable CN's unmatched lines, then re-derive status."""
    if cn.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"credit note is {cn.status}; matching applies only to in-review credit notes")
    match_credit_note(db, cn)
    cn.status = _derive_status(cn)
    _audit(db, "billing.credit_note_matched", actor_uid, cn.id,
           {"matched": sum(1 for ln in cn.lines
                           if ln.match_status != LineMatchStatus.UNMATCHED.value),
            "lines": len(cn.lines)})
    db.commit()
    db.refresh(cn)
    return cn


def apply_manual_cn_match(
    db: Session, cn: CreditNote, line_id: int, po_line_item_id: int,
    *, actor_uid: str | None,
) -> CreditNote:
    """Map one CN line to a specific PO line (status → MANUAL). The PO line must exist and belong
    to the CREDITED invoice's PO (else 400); an unknown CN line is 404.

    Unlike the invoice lane a CN may target ANY line on that PO — even a CLOSED / SHORT_CLOSED one
    — because a credit note REVERSES billing (it can return units on a line that was retired)."""
    if cn.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"credit note is {cn.status}; matching applies only to in-review credit notes")
    line = next((ln for ln in cn.lines if ln.id == line_id), None)
    if line is None:
        raise BillingNotFound("credit-note line not found")
    invoice = db.get(SalesInvoice, cn.invoice_id)
    if invoice is None or invoice.po_id is None:
        raise BillingBadRequest("the credited invoice has no PO to match against")
    po_line = db.get(POLineItem, po_line_item_id)
    if po_line is None or po_line.po_id != invoice.po_id:
        raise BillingBadRequest(
            "the PO line does not exist or does not belong to the credited invoice's PO")

    line.po_line_item_id = po_line_item_id
    line.match_status = LineMatchStatus.MANUAL.value
    cn.status = _derive_status(cn)
    _audit(db, "billing.credit_note_manual_match", actor_uid, cn.id,
           {"line_id": line_id, "po_line_item_id": po_line_item_id})
    db.commit()
    db.refresh(cn)
    return cn


# --------------------------------------------------------------- register / read

def _register_query(
    *,
    status: str | None = None,
    client_id: int | None = None,
    invoice_id: int | None = None,
) -> Select[tuple[CreditNote]]:
    stmt = select(CreditNote)
    if status:
        stmt = stmt.where(CreditNote.status == status)
    if client_id is not None:
        stmt = stmt.where(CreditNote.client_id == client_id)
    if invoice_id is not None:
        stmt = stmt.where(CreditNote.invoice_id == invoice_id)
    return stmt.order_by(CreditNote.id.desc())


def list_credit_notes(
    db: Session,
    *,
    status: str | None = None,
    client_id: int | None = None,
    invoice_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CreditNote]:
    """The credit-note register (status / client / credited-invoice filters)."""
    stmt = _register_query(
        status=status, client_id=client_id, invoice_id=invoice_id,
    ).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def get_credit_note(db: Session, cn_id: int) -> CreditNote:
    """One credit note with its line items. Raises ``BillingNotFound`` on a miss."""
    cn = db.get(CreditNote, cn_id)
    if cn is None:
        raise BillingNotFound("credit note not found")
    return cn


# --------------------------------------------------------------- corrections

def submit_cn_corrections(
    db: Session,
    cn: CreditNote,
    corrections: list[tuple[str, str]],
    *,
    actor_uid: str | None,
) -> CreditNote:
    """Apply per-field human corrections (header totals / cn number / date / reason) to an
    editable CN, then re-derive its status.

    A CONFIRMED / CANCELLED / REJECTED CN is immutable (409); an unknown field is 400; a
    ``cn_number`` change that collides another CN of the same client is 409 (not a DB 500)."""
    if cn.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"credit note is {cn.status}; only in-review credit notes can be corrected")
    if not corrections:
        raise BillingBadRequest("no corrections supplied")

    changed: list[str] = []
    for raw_attr, raw_value in corrections:
        attr = raw_attr.strip()
        spec = _SPEC_BY_ATTR.get(attr)
        if spec is None:
            raise BillingBadRequest(f"unknown field '{raw_attr}'")
        setattr(cn, spec.attr, _coerce(spec, raw_value))
        changed.append(attr)

    _resync_number(db, cn)
    cn.status = _derive_status(cn)
    _audit(db, "billing.credit_note_corrected", actor_uid, cn.id, {"fields": changed})
    db.commit()
    db.refresh(cn)
    return cn


def _resync_number(db: Session, cn: CreditNote) -> None:
    """Guard the ``(client_id, cn_number)`` uniqueness against OTHER credit notes so a correction
    that clashes is a clean 409 rather than a surprise IntegrityError on commit."""
    clash = db.execute(
        select(CreditNote.id).where(
            CreditNote.client_id == cn.client_id,
            CreditNote.cn_number == cn.cn_number,
            CreditNote.id != cn.id,
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise BillingConflict(
            f"credit-note number {cn.cn_number} already exists for this client (#{clash})")


# --------------------------------------------------------------- confirm

def confirm_credit_note(
    db: Session, cn: CreditNote, *, actor_uid: str | None,
) -> CreditNote:
    """Freeze an in-review CN into the immutable CONFIRMED record (drives the §6 invoiced-qty
    write-back: a confirmed CN SUBTRACTS its matched line quantities, reopening billed units).

    Blocked (409) unless: the CREDITED invoice is itself CONFIRMED (a CN can't credit an
    unconfirmed / cancelled invoice); at least one line; every required header field present; and
    every line MATCHED or MANUAL (mapped to a PO line)."""
    # Re-read under a row lock + re-validate status (a concurrent confirm/cancel/delete must not
    # race off a stale snapshot; mirrors the invoice lane).
    locked = db.execute(
        select(CreditNote).where(CreditNote.id == cn.id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise BillingNotFound("credit note not found")
    cn = locked
    if cn.status not in _EDITABLE_STATUSES:
        raise BillingConflict(
            f"credit note is {cn.status}; only in-review credit notes can be confirmed")

    invoice = db.get(SalesInvoice, cn.invoice_id)
    if invoice is None:
        raise BillingConflict("the credited invoice no longer exists")
    if invoice.status != SalesInvoiceStatus.CONFIRMED.value:
        raise BillingConflict(
            f"the credited invoice is {invoice.status}; a credit note can only be confirmed "
            "against a CONFIRMED invoice")

    if not cn.lines:
        raise BillingConflict("a confirmable credit note needs at least one line item")
    missing = _required_missing(cn)
    if missing:
        raise BillingConflict(
            "these required fields still need review before confirming: " + ", ".join(missing))
    unmatched = sorted(
        ln.line_no for ln in cn.lines
        if ln.match_status not in (LineMatchStatus.MATCHED.value, LineMatchStatus.MANUAL.value)
    )
    if unmatched:
        raise BillingConflict(
            "match every line to a PO line before confirming; still unmatched: line(s) "
            + ", ".join(str(n) for n in unmatched))

    cn.status = CreditNoteStatus.CONFIRMED.value
    cn.confirmed_by = actor_uid
    cn.confirmed_at = datetime.now(UTC)
    _audit(db, "billing.credit_note_confirmed", actor_uid, cn.id, {"invoice_id": cn.invoice_id})
    db.commit()
    db.refresh(cn)
    return cn


# --------------------------------------------------------------- cancel / delete

def cancel_credit_note(
    db: Session, cn: CreditNote, *, actor_uid: str | None,
) -> CreditNote:
    """Soft-cancel an in-review CN (status → CANCELLED). A CONFIRMED (immutable) CN cannot be
    cancelled — it already drives the invoiced-qty write-back; use delete if truly needed."""
    locked = db.execute(
        select(CreditNote).where(CreditNote.id == cn.id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise BillingNotFound("credit note not found")
    cn = locked
    if cn.status == CreditNoteStatus.CONFIRMED.value:
        raise BillingConflict("a confirmed credit note is immutable and cannot be cancelled")
    if cn.status == CreditNoteStatus.CANCELLED.value:
        raise BillingConflict("credit note is already cancelled")
    cn.status = CreditNoteStatus.CANCELLED.value
    _audit(db, "billing.credit_note_cancelled", actor_uid, cn.id, {})
    db.commit()
    db.refresh(cn)
    return cn


def delete_credit_note(db: Session, cn: CreditNote, *, actor_uid: str | None) -> None:
    """Delete a credit note (cascades its lines) + its source blob. Supports delete-and-re-upload
    to replace a hard-deduped record. Nothing references a CreditNote, so there are no child
    records to block on (unlike an invoice's AR children)."""
    cn_id = cn.id
    locked = db.execute(
        select(CreditNote).where(CreditNote.id == cn_id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise BillingNotFound("credit note not found")

    was_confirmed = locked.status == CreditNoteStatus.CONFIRMED.value
    source_file_id = locked.source_file_id
    # ORDER MATTERS (immediate FK checks on Postgres): delete + flush the CN FIRST so its FK no
    # longer pins the StoredFile, THEN delete the StoredFile. Blob removal is best-effort AFTER
    # commit (a failed unlink must never lose the delete).
    db.delete(locked)
    db.flush()

    source_ref: str | None = None
    if source_file_id is not None:
        sf = db.get(StoredFile, source_file_id)
        if sf is not None:
            source_ref = sf.storage_ref
            db.delete(sf)

    _audit(db, "billing.credit_note_deleted", actor_uid, cn_id,
           {"was_confirmed": was_confirmed, "source_file_id": source_file_id})
    db.commit()

    if source_ref is not None:
        try:
            get_storage().delete(source_ref)
        except Exception:  # noqa: BLE001 - best-effort; the DB row is already gone
            logger.warning(
                "credit-note source blob %s left on disk after delete of CN %s",
                source_ref, cn_id)


# --------------------------------------------------------------- helpers

def _read_source(db: Session, storage: Storage, file_id: int) -> bytes:
    sf = db.get(StoredFile, file_id)
    if sf is None:
        raise FileNotFoundError(f"stored file {file_id} missing")
    with storage.open(sf.storage_ref) as fh:
        return fh.read()


def _audit(
    db: Session, action: str, actor_uid: str | None, entity_id: int, detail: dict[str, Any],
) -> None:
    audit.log(
        db, action=action, actor_uid=actor_uid,
        entity="billing_credit_note", entity_id=str(entity_id), detail=detail,
    )
