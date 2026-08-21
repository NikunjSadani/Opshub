"""Dedup fingerprints for captured CLIENT (sales) invoices — the identity key + a
text hash. Mirrors ``app.modules.expense.dedup``, but with the SALES semantic inversion.

On a client invoice, **supplier = US** (a constant, our own GSTIN — never a useful
discriminator) and **buyer = the CLIENT**. So the identity key is keyed on the BUYER
(client) GSTIN, not the supplier GSTIN the expense side uses.

Two independent fingerprints:

* ``dedup_key`` — the HARD identity key. ``sha256`` over the normalized client (buyer)
  GSTIN, the invoice number (case/whitespace-folded), the invoice date (ISO) and the
  grand total (paise). Two documents agreeing on all four are the SAME client invoice —
  the unique constraint on ``billing_invoice.dedup_key`` refuses the second (the operator
  deletes + re-uploads to replace). A partially-extracted / OCR-parked document carries a
  NULL key (callers only enforce it when all four identity fields are present) so it never
  falsely collides.
* ``content_hash`` — a belt-and-suspenders fingerprint of the raw SOURCE bytes, catching
  a byte-identical re-upload even before the identity fields are read (and even when there
  is NO text layer, where ``dedup_key`` is NULL — hashing an empty text layer would
  false-match every scan, so we hash the file bytes instead).

Both live here so the service and the tests compute them from ONE formula.
"""
from __future__ import annotations

import hashlib
from datetime import date

from app.modules.masterdata.normalize import match_key


def _norm_gstin(gstin: str | None) -> str:
    """Whitespace-stripped, upper-cased GSTIN (the identity form)."""
    return "".join((gstin or "").split()).upper()


def dedup_key(
    client_gstin: str | None,
    invoice_number: str | None,
    invoice_date: date | None,
    grand_total_paise: int | None,
) -> str:
    """The identity key: ``sha256(norm_client_gstin | match_key(inv_no) | iso_date | paise)``.

    Keyed on the BUYER (client) GSTIN — the supplier GSTIN on a sales invoice is our own
    constant and would collide every invoice we ever raise. A missing component serializes
    to "" so the key stays total; callers only ENFORCE the key when all four identity
    fields are present (see ``invoice_service._enforceable``).
    """
    parts = (
        _norm_gstin(client_gstin),
        match_key(invoice_number or ""),
        invoice_date.isoformat() if invoice_date is not None else "",
        str(grand_total_paise) if grand_total_paise is not None else "",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def content_hash(source: bytes) -> str:
    """``sha256`` of the raw source bytes — a byte-identical re-upload hashes the same
    even when the identity key is NULL (an un-OCR'd scan)."""
    return hashlib.sha256(source).hexdigest()
