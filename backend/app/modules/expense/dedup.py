"""Dedup fingerprints for captured invoices (the identity key + a text hash).

Two independent fingerprints, mirroring the canonical schema:

* ``dedup_key`` — the HARD identity key (F2). ``sha256`` over the normalized
  supplier GSTIN, the invoice number (case/whitespace-folded), the invoice date
  (ISO) and the grand total (paise). Two documents that agree on all four are the
  SAME vendor invoice — the unique constraint on ``expense_invoice.dedup_key``
  refuses the second (the operator deletes + re-uploads to replace).
* ``content_hash`` — a belt-and-suspenders fingerprint of the normalized text
  layer, catching a byte-identical re-upload even before the identity fields are read.

Both live here (not only inside the extractor) so the service, the extractor and
the tests all compute them from ONE formula.
"""
from __future__ import annotations

import hashlib
from datetime import date

from app.modules.masterdata.normalize import collapse_ws, match_key


def _norm_gstin(gstin: str | None) -> str:
    """Whitespace-stripped, upper-cased GSTIN (the identity form)."""
    return "".join((gstin or "").split()).upper()


def dedup_key(
    supplier_gstin: str | None,
    invoice_number: str | None,
    invoice_date: date | None,
    grand_total_paise: int | None,
) -> str:
    """The identity key: ``sha256(norm_gstin | match_key(inv_no) | iso_date | paise)``.

    A missing component serializes to the empty string / "" so the key stays
    stable and total — but callers only ENFORCE the key when all four identity
    fields are present (see ``service._enforceable``); a partially-extracted or
    OCR-parked document carries a NULL key and never falsely collides.
    """
    parts = (
        _norm_gstin(supplier_gstin),
        match_key(invoice_number or ""),
        invoice_date.isoformat() if invoice_date is not None else "",
        str(grand_total_paise) if grand_total_paise is not None else "",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def content_hash(raw_text: str) -> str:
    """``sha256`` of the normalized (whitespace-collapsed) text layer."""
    return hashlib.sha256(collapse_ws(raw_text or "").encode("utf-8")).hexdigest()
