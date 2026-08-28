"""Tally 'Tax Invoice' extractor (`TallyInvoiceExtractor`, "tally/1.0").

Tally's tax-invoice layout defeats the generic `TextLayerExtractor`: its outer border box
makes `pdfplumber.extract_tables()` merge the whole masthead into one cell, and its header
meta (invoice number / date) is a POSITIONAL grid — the value sits on the row *below* its
label, not inline after it — so the generic ``Invoice No:`` anchor would read the neighbouring
``Dated`` column. This engine parses the Tally shape with **word geometry** and reuses the
zero-cost text-layer helpers wholesale (money → integer paise via ``_money_to_paise``, GSTIN
assignment + checksum, ``_normalize_pos``, ``_parse_invoice_date``, the arithmetic
cross-checks, the review gate, hashing) — it only overrides the item table, the header meta,
and the totals block.

House conventions (identical to the text-layer engine):
  * **Money is ALWAYS integer paise** via the challan Decimal discipline (``_money_to_paise``
    → ``challan.parsing.parse_paise``), never a float; the VERBATIM cell string is parsed.
  * **Uncertainty routes to a human, never a silent wrong number.** A recomputed grand total
    that disagrees with the printed one drops ``grand_total_paise`` to LOW_CONFIDENCE (a
    REQUIRED field → NEEDS_REVIEW → confirm blocked); a printed-vs-summed taxable mismatch,
    an inconsistent tax split, a per-line tax mismatch, or a dropped/ambiguous row each add a
    review reason and cap the affected money.
  * **Multi-page is read whole, never truncated at a per-page subtotal.** Line items are
    gathered across ALL pages and the DOCUMENT grand total is the LAST "Total" row (not the
    first per-page subtotal), so an intermediate subtotal can never masquerade as the grand
    total and halve the invoice. When a per-page subtotal is present the all-page line sum must
    reconcile to the printed document taxable; if it cannot, BOTH required totals drop to
    LOW_CONFIDENCE (→ review) rather than store a self-consistent-but-wrong figure.

The composite `TallyAwareExtractor` reads the PDF once, and either takes the Tally branch
(only when ``_is_tally_tax_invoice`` sees a genuinely Tally-SPECIFIC structural signal — the
split two-row ``Taxable``+``CGST``/``SGST``/``IGST`` column banner, or the positional
``Invoice No.``/``Dated`` header-meta grid — biasing to FALLBACK when uncertain) or delegates
UNCHANGED to `TextLayerExtractor` (keeping its empty-text-layer → needs_ocr behaviour). It is
wired into ``get_extractor()`` as the default, so no billing-service call site changes.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal

import pdfplumber

from app.modules.challan.parsing import _ILLEGAL_CTRL
from app.modules.expense.canonical import (
    CANONICAL_SCHEMA_VERSION,
    ArithmeticChecks,
    DateField,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceHeader,
    InvoiceTotals,
    MoneyField,
    TextField,
)
from app.modules.expense.text_layer import (
    _BILLTO_RE,
    _CONF_OK,
    _CONF_STRONG,
    _CONF_WEAK,
    _MIN_CHARS_PER_PAGE,
    _PO_RE,
    _POS_RE,
    _ROUND_OFF_RE,
    _TOTAL_CGST_RE,
    _TOTAL_IGST_RE,
    _TOTAL_SGST_RE,
    _WORDS_RE,
    InvoiceExtractionError,
    TextLayerExtractor,
    _assign_gstins,
    _build_lines,
    _buyer_block,
    _content_hash,
    _Corrob,
    _dedup_key,
    _find_first,
    _group_word_rows,
    _gstin_candidates,
    _gstin_field,
    _Line,
    _missing,
    _mk,
    _money_to_paise,
    _normalize_pos,
    _num_or_none,
    _parse_invoice_date,
    _RawLine,
    _rebuild_lines,
    _review_reasons,
    _run_arithmetic,
    _supplier_block,
    _Word,
    _words_of_page,
)

NAME = "tally/1.0"

# A per-line character cap applied BEFORE any regex runs on a visual line — a defensive bound
# against a pathological long line driving a regex into quadratic backtracking (the handoff's
# lazy multi-money grand-total regex is deliberately NOT used at all; totals come from geometry).
_MAX_LINE = 2000

# Tokens that must all appear in the Tally line-item column banner.
_BANNER_KEYS = ("hsn/sac", "quantity", "amount", "taxable", "total")

# Leftmost keyword of a Tally summary / sub-total row (never a real item — items carry an Sl
# integer). "total" is handled separately (the grand-total row), so it is NOT listed here.
_SUMMARY_LEADS: frozenset[str] = frozenset({
    "cgst", "sgst", "utgst", "igst", "round", "rounding", "output",
    "less", "add", "sub", "subtotal", "sub-total", "discount", "cess",
})


# --------------------------------------------------------------------------- detection

def _is_banner_line(text: str) -> bool:
    """True for the Tally item-table column banner (``Sl … HSN/SAC Quantity … Amount Taxable
    … Total``) — all of the strong banner keywords present in one line."""
    low = text.lower()
    return all(k in low for k in _BANNER_KEYS)


def _is_tally_banner_line(text: str) -> bool:
    """True only for the FULL Tally item-table banner: every strong banner keyword AND the
    split tax-column structure Tally lays out (a ``Taxable`` column beside ``CGST``+``SGST``/
    ``UTGST`` for intra-state, or beside ``IGST`` for inter-state).

    This split ``Taxable Value`` + per-tax-head layout on a single header line is a genuinely
    Tally-SPECIFIC structural signal — an ordinary GST invoice writes ``Qty``/``Rate``/``Amount``
    without folding HSN/SAC + Quantity + Amount + Taxable + Total + the tax heads into one
    banner row — so a match is decisive on its own.
    """
    low = text.lower()
    if not all(k in low for k in _BANNER_KEYS):
        return False
    has_intra = "cgst" in low and ("sgst" in low or "utgst" in low)
    has_inter = "igst" in low
    return has_intra or has_inter


def _has_tally_meta_grid(lines: list[str]) -> bool:
    """True when the Tally POSITIONAL header-meta grid is present: the ``Invoice No.`` and
    ``Dated`` column LABELS sit together on one banner row (their values print on the row
    below, in the label's own x-band). A generic invoice writes these inline
    (``Invoice No: X`` / ``Invoice Date: Y``) rather than as a shared two-column header, so the
    ``Invoice No`` + ``Dated`` header pair is a Tally-structural tell.
    """
    for ln in lines:
        low = ln.lower()
        if re.search(r"invoice\s*no\b", low) and re.search(r"\bdated\b", low):
            return True
    return False


def _is_tally_tax_invoice(text: str) -> bool:
    """Format gate: take the Tally branch ONLY on a HIGH-confidence, genuinely Tally-SPECIFIC
    STRUCTURAL signal; otherwise fall back to the generic engine (always safe).

    Deciding signals are the two Tally-exclusive layout structures:
      * the full two-row column banner with its split ``Taxable`` + ``CGST``/``SGST``/``IGST``
        tax columns (`_is_tally_banner_line`) — decisive on its own; or
      * the positional header-meta grid (`_has_tally_meta_grid`), accepted only when the
        document also titles itself "Tax Invoice" (the grid pair alone is a weaker tell).

    The old non-exclusive phrase anchors ("Amount Chargeable (in words)", "Buyer's Order No",
    "Computer Generated Invoice") are DROPPED as deciding signals — a plain non-Tally GST
    invoice carries them too, and routing it here mangles it. They are, at most, incidental.
    Uncertain → False, so the generic `TextLayerExtractor` handles it.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    if any(_is_tally_banner_line(ln) for ln in lines):
        return True
    return _has_tally_meta_grid(lines) and lines[0].lower() == "tax invoice"


# --------------------------------------------------------------------------- column model

def _center(w: _Word) -> float:
    return (w.x0 + w.x1) / 2.0


@dataclass
class _Col:
    """One leaf column of the Tally item table: its canonical name + x-centre."""
    name: str
    center: float


@dataclass
class _Grid:
    """The Tally item-table column model, built from the two-row banner via word geometry."""
    desc_tail_x: float          # words whose centre < this are description; ≥ this are the tail
    tail_cols: list[_Col]       # ordered leaf columns for the numeric tail (hsn … total)
    intra: bool                 # True = CGST+SGST columns; False = IGST column


def _row_text(row: list[_Word]) -> str:
    return " ".join(w.text for w in sorted(row, key=lambda w: w.x0))


def _page_rows(words: list[_Word]) -> list[list[_Word]]:
    """One page's words grouped into visual rows, ordered top→bottom (each row left→right)."""
    rows = _group_word_rows(words)
    rows.sort(key=lambda r: min(w.top for w in r))
    return rows


def _find1(row: list[_Word], pred: object) -> _Word | None:
    for w in row:
        if pred(w.text):  # type: ignore[operator]
            return w
    return None


def _build_grid(row1: list[_Word], row2: list[_Word]) -> _Grid | None:
    """Build the leaf-column model from banner row1 (main labels) + row2 (sub-labels).

    Row1 gives the left columns (Sl / Description / HSN / Qty / Rate / per / Amount / Taxable)
    and the tax-group headers (CGST / SGST or IGST); row2 gives the tax sub-columns (Rate /
    Amount pairs) and the Total amount. Returns None if the row is not a recognisable Tally
    banner (no HSN column), so the caller can fall back to the generic engine.
    """
    r1 = sorted(row1, key=lambda w: w.x0)
    r2 = sorted(row2, key=lambda w: w.x0)

    tok_sl = _find1(r1, lambda t: t.lower().rstrip(".") == "sl")
    tok_hsn = _find1(r1, lambda t: t.upper().startswith("HSN") or t.upper() == "SAC")
    if tok_hsn is None:
        return None
    tok_qty = _find1(r1, lambda t: t.lower().startswith(("quantity", "qty")))
    tok_rate = _find1(r1, lambda t: t.lower() == "rate")           # unit rate (first "Rate")
    tok_per = _find1(r1, lambda t: t.lower() == "per")
    tok_amount = _find1(r1, lambda t: t.lower() == "amount")       # unit amount (first "Amount")
    tok_taxable = _find1(r1, lambda t: t.lower().startswith("taxable"))
    tok_cgst = _find1(r1, lambda t: t.upper() == "CGST")
    tok_sgst = _find1(r1, lambda t: t.upper().startswith(("SGST", "UTGST")))
    tok_igst = _find1(r1, lambda t: t.upper() == "IGST")
    tok_total = next((w for w in reversed(r1) if w.text.lower() == "total"), None)

    # Description / tail boundary: the gutter between the description header span and HSN.
    desc_tokens = [w for w in r1
                   if (tok_sl is None or w.x0 > tok_sl.x0) and w.x0 < tok_hsn.x0]
    desc_right = max((w.x1 for w in desc_tokens), default=tok_hsn.x0 - 20.0)
    desc_tail_x = (desc_right + tok_hsn.x0) / 2.0

    cols: list[_Col] = []

    def add(name: str, w: _Word | None) -> None:
        if w is not None:
            cols.append(_Col(name, _center(w)))

    add("hsn", tok_hsn)
    add("qty", tok_qty)
    add("rate", tok_rate)
    add("per", tok_per)
    add("amount", tok_amount)
    add("taxable", tok_taxable)

    intra = tok_igst is None and (tok_cgst is not None or tok_sgst is not None)
    taxable_center = _center(tok_taxable) if tok_taxable else None
    total_center = _center(tok_total) if tok_total else None

    subs = [w for w in r2 if w.text.lower() in ("rate", "amount")]
    if taxable_center is not None:
        subs = [w for w in subs if _center(w) > taxable_center]
    if total_center is not None:
        subs = [w for w in subs if _center(w) < total_center - 1.0]
    subs.sort(key=_center)

    if intra and len(subs) >= 4:
        names4 = ("cgst_rate", "cgst_amt", "sgst_rate", "sgst_amt")
        for name, sub in zip(names4, subs[:4], strict=True):
            cols.append(_Col(name, _center(sub)))
    elif not intra and len(subs) >= 2:
        for name, sub in zip(("igst_rate", "igst_amt"), subs[:2], strict=True):
            cols.append(_Col(name, _center(sub)))
    elif intra:                                   # single-row banner fallback (no sub-labels)
        add("cgst_amt", tok_cgst)
        add("sgst_amt", tok_sgst)
    else:
        add("igst_amt", tok_igst)

    add("total", tok_total)
    cols.sort(key=lambda c: c.center)
    return _Grid(desc_tail_x=desc_tail_x, tail_cols=cols, intra=intra)


def _bucket(words: list[_Word], cols: list[_Col]) -> dict[str, list[str]]:
    """Assign each tail word to its NEAREST-centre leaf column → {column: [words]}.

    Nearest-centre (not a left-edge band) keeps a right-aligned money value filed in its own
    column, exactly as the text-layer word-geometry fallback does.
    """
    out: dict[str, list[str]] = {}
    for w in words:
        j = min(range(len(cols)), key=lambda k: abs(cols[k].center - _center(w)))
        out.setdefault(cols[j].name, []).append(w.text)
    return out


def _money_of(cells: dict[str, list[str]], name: str) -> int | None:
    """First value in a column that parses as verbatim money → integer paise (else None).

    Per-token (never a join): money is a single token, so a stray non-money glyph sharing the
    column — e.g. Tally's rupee-symbol ``(cid:299)`` before the bill total — is skipped, not
    concatenated into an unparseable ``"(cid:299) 1,10,701.00"``.
    """
    for tok in cells.get(name, []):
        paise = _money_to_paise(tok)
        if paise is not None:
            return paise
    return None


def _text_of(cells: dict[str, list[str]], name: str) -> str | None:
    joined = " ".join(cells.get(name, [])).strip()
    return joined or None


def _rate_of(cells: dict[str, list[str]], name: str) -> Decimal | None:
    for tok in cells.get(name, []):
        if "%" in tok:
            return _num_or_none(tok)
    # No explicit "%": accept a bare numeric rate token if present.
    for tok in cells.get(name, []):
        val = _num_or_none(tok)
        if val is not None:
            return val
    return None


def _split_qty_unit(text: str | None) -> tuple[Decimal | None, str | None]:
    """Split a Tally quantity cell ("2 PCS") into (Decimal qty, unit)."""
    if not text:
        return None, None
    qty: Decimal | None = None
    unit_parts: list[str] = []
    for part in text.split():
        val = _num_or_none(part)
        if val is not None and qty is None:
            qty = val
        else:
            unit_parts.append(part)
    return qty, (" ".join(unit_parts) or None)


def _combine_rate(cgst_rate: Decimal | None, sgst_rate: Decimal | None,
                  igst_rate: Decimal | None) -> Decimal | None:
    """Canonical (combined) GST rate: IGST rate, else CGST + SGST (so taxable × rate/100 = the
    line's total tax and the per-line arithmetic check corroborates)."""
    if igst_rate is not None:
        return igst_rate
    if cgst_rate is not None or sgst_rate is not None:
        return (cgst_rate or Decimal(0)) + (sgst_rate or Decimal(0))
    return None


# --------------------------------------------------------------------------- item rows

def _tail_money_count(toks: list[_Word], grid: _Grid) -> int:
    return sum(1 for w in toks
               if _center(w) >= grid.desc_tail_x and _money_to_paise(w.text) is not None)


def _is_item_start(toks: list[_Word], grid: _Grid) -> bool:
    """An item row: leftmost token is a bare Sl integer AND the tail carries real money."""
    if not toks:
        return False
    lead = toks[0]
    return (lead.text.isascii() and lead.text.isdigit()
            and lead.x0 < grid.desc_tail_x and _tail_money_count(toks, grid) >= 1)


def _is_summary_lead(text: str) -> bool:
    return text.strip().lower().rstrip(":") in _SUMMARY_LEADS


def _parse_item_row(toks: list[_Word], grid: _Grid) -> _RawLine:
    """Parse one Tally item row into a `_RawLine` (paise ints / Decimals + verbatim strings)."""
    rest = toks[1:]  # drop the Sl number
    desc_words = [w for w in rest if _center(w) < grid.desc_tail_x]
    tail_words = [w for w in rest if _center(w) >= grid.desc_tail_x]
    cells = _bucket(tail_words, grid.tail_cols)

    taxable = _money_of(cells, "taxable")
    amount = _money_of(cells, "amount")
    if taxable is None:                       # no explicit Taxable-Value column → gross Amount
        taxable = amount
    qty, unit = _split_qty_unit(_text_of(cells, "qty"))
    gst_rate = _combine_rate(_rate_of(cells, "cgst_rate"), _rate_of(cells, "sgst_rate"),
                             _rate_of(cells, "igst_rate"))
    description = " ".join(w.text for w in desc_words).strip() or None
    return _RawLine(
        raw={"row": _row_text(toks)},
        description=description,
        hsn=_text_of(cells, "hsn"),
        quantity=qty,
        unit=unit,
        unit_rate=_money_of(cells, "rate"),
        taxable=taxable,
        gst_rate=gst_rate,
        cgst=_money_of(cells, "cgst_amt"),
        sgst=_money_of(cells, "sgst_amt"),
        igst=_money_of(cells, "igst_amt"),
        line_total=_money_of(cells, "total"),
    )


@dataclass
class _TotalRow:
    grand: int | None
    taxable: int | None
    cgst: int | None
    sgst: int | None
    igst: int | None


def _parse_total_row(toks: list[_Word], grid: _Grid) -> _TotalRow:
    """Bucket the Tally grand-total row: the bill total sits in the AMOUNT column, the taxable
    / tax totals in their own columns."""
    tail = [w for w in toks if _center(w) >= grid.desc_tail_x]
    cells = _bucket(tail, grid.tail_cols)
    return _TotalRow(
        grand=_money_of(cells, "amount"),
        taxable=_money_of(cells, "taxable"),
        cgst=_money_of(cells, "cgst_amt"),
        sgst=_money_of(cells, "sgst_amt"),
        igst=_money_of(cells, "igst_amt"),
    )


@dataclass
class _Items:
    rows: list[_RawLine] = field(default_factory=list)
    # EVERY "Total" row across all pages, in document order. On a multi-page Tally invoice the
    # earlier entries are per-page SUBTOTALS (carried forward); the DOCUMENT grand total is the
    # LAST one (on the final page, followed by round-off / "Amount Chargeable (in words)").
    total_rows: list[_TotalRow] = field(default_factory=list)
    total_row: _TotalRow | None = None          # the DOCUMENT grand total == total_rows[-1]
    intermediate_subtotal: bool = False          # a per-page subtotal preceded the grand total
    dropped: int = 0


def _extract_items(by_page: dict[int, list[_Word]]) -> tuple[_Items, _Grid | None]:
    """Walk EVERY page's item region, stitching wrapped descriptions and collecting every
    "Total" row. The column grid is established from the first banner and reused for
    continuation pages (which repeat the banner).

    Critically, scanning NEVER stops at the first "Total": on a multi-page invoice that first
    "Total" is an intermediate per-page SUBTOTAL, and stopping there would drop the later pages'
    line items and mistake the page-1 subtotal for the grand total (a silent money-halving that
    still passes every self-consistent arithmetic check). Line items are gathered across all
    pages, and the DOCUMENT grand total is resolved afterwards as the LAST "Total" row.
    """
    result = _Items()
    grid: _Grid | None = None
    for page in sorted(by_page):
        rows = _page_rows(by_page[page])
        b_idx = next((i for i, r in enumerate(rows) if _is_banner_line(_row_text(r))), None)
        if b_idx is None:
            continue
        row2 = rows[b_idx + 1] if b_idx + 1 < len(rows) else []
        if grid is None:
            grid = _build_grid(rows[b_idx], row2)
            if grid is None:
                continue
        # Skip the banner's own sub-label row before scanning items.
        start = b_idx + 1
        if start < len(rows) and _is_subheader(rows[start], grid):
            start += 1
        _scan_page(rows[start:], grid, result)
    # The grand total is the FINAL "Total" row; any earlier one was a per-page subtotal.
    if result.total_rows:
        result.total_row = result.total_rows[-1]
        result.intermediate_subtotal = len(result.total_rows) > 1
    return result, grid


def _is_subheader(row: list[_Word], grid: _Grid) -> bool:
    """The banner's second physical row (sub-labels only: No./Value/Rate/Amount)."""
    toks = [w.text.lower().rstrip(".") for w in row]
    return bool(toks) and all(t in ("no", "value", "rate", "amount") for t in toks)


def _scan_page(rows: list[list[_Word]], grid: _Grid, result: _Items) -> None:
    current: _RawLine | None = None
    in_summary = False
    for row in rows:
        toks = sorted(row, key=lambda w: w.x0)
        if not toks:
            continue
        lead = toks[0].text.strip()
        low = _row_text(toks).lower()
        if lead.lower() == "total":                       # a "Total" row (subtotal OR grand)
            if current is not None:
                result.rows.append(current)
                current = None
            # Record it and stop scanning THIS page. Whether it is a per-page subtotal or the
            # document grand total is decided in `_extract_items` (the LAST one wins) — so a
            # page-1 subtotal here can never truncate the later pages' items.
            result.total_rows.append(_parse_total_row(toks, grid))
            return
        if low.startswith("amount chargeable"):
            break
        if _is_summary_lead(lead):                         # CGST / SGST / ROUND OFF / …
            if current is not None:
                result.rows.append(current)
                current = None
            in_summary = True
            continue
        if in_summary:
            continue
        if _is_item_start(toks, grid):
            if current is not None:
                result.rows.append(current)
            current = _parse_item_row(toks, grid)
        elif current is not None and _center(toks[0]) < grid.desc_tail_x:
            # A wrapped description continuation row (text only, no Sl) — stitch it on.
            extra = " ".join(w.text for w in toks if _center(w) < grid.desc_tail_x).strip()
            if extra:
                current.description = (
                    f"{current.description} {extra}".strip() if current.description else extra)
        elif _tail_money_count(toks, grid) >= 1:
            result.dropped += 1                            # a money-bearing row we couldn't map
    if current is not None:
        result.rows.append(current)


# --------------------------------------------------------------------------- header meta

def _header_meta(rows: list[list[_Word]]) -> tuple[str | None, bool, str | None]:
    """Read the Tally header GRID: the invoice number / date value sits on the row BELOW its
    label, in the label's own x-band. Returns (number, number_ambiguous, date_raw).

    The value is bounded on the right by the neighbouring ``Dated`` column so adjacent-column
    bleed is stripped; a value that resolves to more than one token flags ambiguity → the
    caller drops the field to LOW_CONFIDENCE (→ review) rather than store a merged guess.
    """
    for i, row in enumerate(rows):
        toks = sorted(row, key=lambda w: w.x0)
        inv_tok: _Word | None = None
        for k in range(len(toks) - 1):
            if (toks[k].text.lower().rstrip(".") == "invoice"
                    and toks[k + 1].text.lower().startswith("no")):
                inv_tok = toks[k]
                break
        if inv_tok is None:
            continue
        dated_tok = next((w for w in toks
                          if w.text.lower().rstrip(".") == "dated" and w.x0 > inv_tok.x0), None)
        val_row = sorted(rows[i + 1], key=lambda w: w.x0) if i + 1 < len(rows) else []
        right = (dated_tok.x0 - 15.0) if dated_tok else float("inf")
        num_toks = [w for w in val_row if inv_tok.x0 - 12.0 <= w.x0 < right]
        date_toks = ([w for w in val_row if dated_tok and w.x0 >= dated_tok.x0 - 15.0]
                     if dated_tok else [])
        number = " ".join(w.text for w in num_toks).strip() or None
        date_raw = " ".join(w.text for w in date_toks).strip() or None
        return number, len(num_toks) > 1, date_raw
    return None, False, None


# --------------------------------------------------------------------------- totals

def _last_money(lines: list[_Line], pattern: re.Pattern[str]) -> tuple[str, _Line] | None:
    """The LAST line matching `pattern` (totals sit at the bottom), reading its captured money.

    Each line is length-capped BEFORE the regex runs — a defensive bound so a pathological long
    line can never drive the (linear) label regexes into a slow path.
    """
    found: tuple[str, _Line] | None = None
    for ln in lines:
        text = ln.text if len(ln.text) <= _MAX_LINE else ln.text[:_MAX_LINE]
        m = pattern.search(text)
        if m and m.group(1).strip():
            found = (m.group(1).strip(), ln)
    return found


# --------------------------------------------------------------------------- the extractor

class TallyInvoiceExtractor:
    """Word-geometry extractor for Tally 'Tax Invoice' PDFs (see module docstring)."""

    name = NAME

    def extract(self, pdf_bytes: bytes, *, doc_type: str = "gst_invoice") -> ExtractedInvoice:
        if doc_type != "gst_invoice":
            raise ValueError(f"unsupported doc_type {doc_type!r} (only 'gst_invoice')")
        page_count = TextLayerExtractor._probe(pdf_bytes)
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = list(pdf.pages)
                if not pages:  # pragma: no cover - _probe already guards zero pages
                    raise InvoiceExtractionError("zero-page PDF")
                page_texts = [(p.extract_text() or "") for p in pages]
                words: list[_Word] = []
                for i, p in enumerate(pages, start=1):
                    words.extend(_words_of_page(p, i))
        except InvoiceExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - pdfplumber raises many types on bad input
            raise InvoiceExtractionError("could not read the PDF text layer") from exc
        raw_text = _ILLEGAL_CTRL.sub("", "\n".join(page_texts))
        return _build(page_count, raw_text, words)


def _build(page_count: int, raw_text: str, words: list[_Word]) -> ExtractedInvoice:
    lines = _rebuild_lines(words)
    by_page: dict[int, list[_Word]] = {}
    for w in words:
        by_page.setdefault(w.page, []).append(w)

    # ---- GSTINs + place of supply (reused) --------------------------------------------
    cands = _gstin_candidates(words)
    billto = next((ln for ln in lines if _BILLTO_RE.search(ln.text)), None)
    supplier_c, buyer_c, supplier_ambiguous, buyer_ambiguous = _assign_gstins(cands, billto)
    supplier_code = supplier_c.value[:2] if supplier_c else None
    buyer_code = buyer_c.value[:2] if buyer_c else None
    pos_hit = _find_first(lines, _POS_RE)
    pos_code, pos_norm = _normalize_pos(pos_hit[0]) if pos_hit else (None, None)

    # ---- header meta (positional grid) ------------------------------------------------
    first_page = min(by_page) if by_page else 1
    meta_rows = _page_rows(by_page.get(first_page, []))
    number, number_ambiguous, date_raw = _header_meta(meta_rows)

    # ---- line items (word geometry) ---------------------------------------------------
    items, _grid = _extract_items(by_page)
    raw_lines = items.rows

    # ---- totals -----------------------------------------------------------------------
    cg = _last_money(lines, _TOTAL_CGST_RE)
    sg = _last_money(lines, _TOTAL_SGST_RE)
    ig = _last_money(lines, _TOTAL_IGST_RE)
    ro = _last_money(lines, _ROUND_OFF_RE)
    total_cgst = _money_to_paise(cg[0]) if cg else None
    total_sgst = _money_to_paise(sg[0]) if sg else None
    total_igst = _money_to_paise(ig[0]) if ig else None
    round_off = _money_to_paise(ro[0]) if ro else None

    tr = items.total_row
    if tr is not None:
        total_cgst = total_cgst if total_cgst is not None else tr.cgst
        total_sgst = total_sgst if total_sgst is not None else tr.sgst
        total_igst = total_igst if total_igst is not None else tr.igst
    printed_taxable = tr.taxable if tr else None
    grand_total = tr.grand if tr else None

    item_sum = (sum(r.taxable for r in raw_lines if r.taxable is not None)
                if any(r.taxable is not None for r in raw_lines) else None)
    total_taxable = printed_taxable if printed_taxable is not None else item_sum
    taxable_mismatch = (printed_taxable is not None and item_sum is not None
                        and printed_taxable != item_sum)

    # SAFETY NET (multi-page silent money-halving): when a per-page SUBTOTAL preceded the grand
    # total, the line items captured across ALL pages MUST reconcile to the printed DOCUMENT
    # taxable. If they do not — the all-page sum disagrees with the printed document total, a
    # figure is missing, or a money-bearing row was dropped (ambiguous page-break geometry) —
    # the totals cannot be trusted, so the REQUIRED totals are forced LOW_CONFIDENCE
    # (→ NEEDS_REVIEW → confirm BLOCKED) instead of storing a self-consistent-but-wrong (e.g.
    # halved) grand total. A clean multi-page invoice whose all-page sum DOES reconcile extracts
    # fully and stays review-clean.
    reconcile_uncertain = items.intermediate_subtotal and (
        item_sum is None or printed_taxable is None
        or item_sum != printed_taxable or items.dropped > 0)

    # ---- arithmetic cross-checks (reused) ---------------------------------------------
    arithmetic, corrob = _run_arithmetic(
        raw_lines, total_taxable, total_cgst, total_sgst, total_igst, round_off,
        grand_total, pos_code, supplier_code, buyer_code)

    # ---- envelopes --------------------------------------------------------------------
    header = _build_header(lines, billto, supplier_c, buyer_c, supplier_ambiguous,
                           buyer_ambiguous, arithmetic, corrob, pos_hit, pos_code, pos_norm,
                           number, number_ambiguous, date_raw)
    invoice_lines = _build_lines(raw_lines, arithmetic, corrob)
    words_hit = _last_money_words(lines)
    totals = _build_totals(total_taxable, total_cgst, total_sgst, total_igst, round_off,
                           grand_total, arithmetic, corrob, taxable_mismatch, words_hit,
                           reconcile_uncertain)

    reasons = _review_reasons(header, totals, invoice_lines, supplier_c, buyer_c, arithmetic)
    if items.dropped:
        reasons.append(
            f"{items.dropped} line row(s) carried amounts but could not be mapped to columns")
    if taxable_mismatch and arithmetic.lines_sum_matches_taxable:
        reasons.append("printed total taxable does not match the sum of the line items")
    if reconcile_uncertain:
        reasons.append(
            "multi-page invoice could not be reconciled — captured line items do not sum to "
            "the printed document total (a page may have been dropped)")
    reasons = _dedupe(reasons)

    invoice = ExtractedInvoice(
        schema_version=CANONICAL_SCHEMA_VERSION, doc_type="gst_invoice", source_engine=NAME,
        page_count=page_count, needs_ocr=False, review_needed=bool(reasons),
        review_reasons=reasons, header=header, lines=invoice_lines, totals=totals,
        arithmetic=arithmetic, raw_text=raw_text, content_hash=_content_hash(raw_text),
        dedup_key=_dedup_key(
            supplier_c.value if supplier_c else None,
            header.invoice_number.value_normalized,
            header.invoice_date.value_normalized,
            grand_total,
        ),
    )
    _restamp_engine(invoice)
    return invoice


def _last_money_words(lines: list[_Line]) -> tuple[str, _Line] | None:
    found: tuple[str, _Line] | None = None
    for ln in lines:
        text = ln.text if len(ln.text) <= _MAX_LINE else ln.text[:_MAX_LINE]
        m = _WORDS_RE.search(text)
        if m and m.group(1).strip():
            found = (m.group(1).strip(), ln)
    return found


def _dedupe(reasons: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# --------------------------------------------------------------------------- header build

def _build_header(
    lines: list[_Line], billto: _Line | None, supplier_c: object, buyer_c: object,
    supplier_ambiguous: bool, buyer_ambiguous: bool, arith: ArithmeticChecks, corrob: _Corrob,
    pos_hit: tuple[str, _Line] | None, pos_code: str | None, pos_norm: str | None,
    number: str | None, number_ambiguous: bool, date_raw: str | None,
) -> InvoiceHeader:
    supply_ok = arith.supply_type_consistent and corrob.supply_ran
    supplier_gstin = _gstin_field(supplier_c, supply_ok, ambiguous=supplier_ambiguous)  # type: ignore[arg-type]
    buyer_gstin = _gstin_field(buyer_c, supply_ok, ambiguous=buyer_ambiguous)  # type: ignore[arg-type]

    if number:
        invoice_number: TextField = _mk(
            number, number, _CONF_WEAK if number_ambiguous else _CONF_OK,
            FieldStatus.LOW_CONFIDENCE if number_ambiguous else FieldStatus.OK, None)
    else:
        invoice_number = _missing()

    if date_raw:
        parsed = _parse_invoice_date(date_raw)
        invoice_date: DateField = _mk(
            parsed, date_raw, _CONF_OK if parsed else _CONF_WEAK,
            FieldStatus.OK if parsed else FieldStatus.LOW_CONFIDENCE, None)
    else:
        invoice_date = _missing()

    if pos_hit and pos_code:
        place_of_supply: TextField = _mk(
            pos_norm, pos_hit[0], _CONF_STRONG if supply_ok else _CONF_OK,
            FieldStatus.OK, pos_hit[1])
    elif pos_hit:
        place_of_supply = _mk(pos_norm, pos_hit[0], _CONF_WEAK,
                              FieldStatus.LOW_CONFIDENCE, pos_hit[1])
    else:
        place_of_supply = _missing()

    po_hit = _find_first(lines, _PO_RE)
    po_ref: TextField = (_mk(po_hit[0], po_hit[0], _CONF_OK, FieldStatus.OK, po_hit[1])
                         if po_hit else _missing())

    supplier_name, supplier_address = _supplier_block(lines, billto, supplier_c)  # type: ignore[arg-type]
    buyer_name, buyer_address = _buyer_block(lines, billto, buyer_c)  # type: ignore[arg-type]

    return InvoiceHeader(
        supplier_name=supplier_name, supplier_gstin=supplier_gstin,
        supplier_address=supplier_address, buyer_name=buyer_name, buyer_gstin=buyer_gstin,
        buyer_address=buyer_address, invoice_number=invoice_number, invoice_date=invoice_date,
        place_of_supply=place_of_supply, po_ref=po_ref,
    )


# --------------------------------------------------------------------------- totals build

def _money_field(value: int | None, low: bool, corroborated: bool) -> MoneyField:
    """A totals money field. ``low`` (a failed cross-check) forces LOW_CONFIDENCE — for a
    REQUIRED field that routes the whole invoice to human review and blocks confirm."""
    if value is None:
        return _missing()
    if low:
        return _mk(value, str(value), _CONF_WEAK, FieldStatus.LOW_CONFIDENCE, None)
    conf = _CONF_STRONG if corroborated else _CONF_OK
    return _mk(value, str(value), conf, FieldStatus.OK, None)


def _build_totals(
    total_taxable: int | None, total_cgst: int | None, total_sgst: int | None,
    total_igst: int | None, round_off: int | None, grand_total: int | None,
    arith: ArithmeticChecks, corrob: _Corrob, taxable_mismatch: bool,
    words_hit: tuple[str, _Line] | None, reconcile_uncertain: bool = False,
) -> InvoiceTotals:
    # An unreconciled multi-page invoice (`reconcile_uncertain`) forces BOTH required totals to
    # LOW_CONFIDENCE even when the printed figures are internally self-consistent — a halved
    # grand total that still "adds up" must never be stored as OK.
    grand_low = (reconcile_uncertain
                 or (grand_total is not None and corrob.grand_ran
                     and not arith.totals_add_to_grand))
    taxable_low = (reconcile_uncertain or taxable_mismatch
                   or (corrob.taxable_ran and not arith.lines_sum_matches_taxable))
    tax_low = corrob.per_line_ran and not arith.per_line_tax_consistent

    taxable_ok = corrob.taxable_ran and arith.lines_sum_matches_taxable
    grand_ok = corrob.grand_ran and arith.totals_add_to_grand
    tax_ok = corrob.per_line_ran and arith.per_line_tax_consistent

    return InvoiceTotals(
        total_taxable_paise=_money_field(total_taxable, taxable_low, taxable_ok),
        total_cgst_paise=_money_field(total_cgst, tax_low, tax_ok),
        total_sgst_paise=_money_field(total_sgst, tax_low, tax_ok),
        total_igst_paise=_money_field(total_igst, tax_low, tax_ok),
        round_off_paise=_money_field(round_off, False, grand_ok),
        grand_total_paise=_money_field(grand_total, grand_low, grand_ok),
        amount_in_words=(_mk(words_hit[0], words_hit[0], _CONF_OK, FieldStatus.OK, words_hit[1])
                         if words_hit else _missing()),
    )


# --------------------------------------------------------------------------- engine restamp

def _restamp_engine(inv: ExtractedInvoice) -> None:
    """Stamp every field's provenance as this engine (the reused text-layer builders hardcode
    their own NAME)."""
    fields: list[Field] = [  # type: ignore[type-arg]
        inv.header.supplier_name, inv.header.supplier_gstin, inv.header.supplier_address,
        inv.header.buyer_name, inv.header.buyer_gstin, inv.header.buyer_address,
        inv.header.invoice_number, inv.header.invoice_date, inv.header.place_of_supply,
        inv.header.po_ref, inv.totals.total_taxable_paise, inv.totals.total_cgst_paise,
        inv.totals.total_sgst_paise, inv.totals.total_igst_paise, inv.totals.round_off_paise,
        inv.totals.grand_total_paise, inv.totals.amount_in_words,
    ]
    for ln in inv.lines:
        fields.extend([
            ln.description, ln.hsn_sac, ln.quantity, ln.unit, ln.unit_rate_paise,
            ln.taxable_paise, ln.gst_rate, ln.cgst_paise, ln.sgst_paise, ln.igst_paise,
            ln.line_total_paise,
        ])
    for f in fields:
        f.source_engine = NAME


# --------------------------------------------------------------------------- composite

class TallyAwareExtractor:
    """Reads the PDF once, routes Tally 'Tax Invoice' PDFs to the Tally engine, and delegates
    everything else UNCHANGED to `TextLayerExtractor` (including its empty-text-layer →
    needs_ocr behaviour)."""

    name = "tally_aware/1.0"

    def __init__(self) -> None:
        self._fallback = TextLayerExtractor()
        self._tally = TallyInvoiceExtractor()

    def extract(self, pdf_bytes: bytes, *, doc_type: str = "gst_invoice") -> ExtractedInvoice:
        if doc_type != "gst_invoice":
            raise ValueError(f"unsupported doc_type {doc_type!r} (only 'gst_invoice')")
        page_count = TextLayerExtractor._probe(pdf_bytes)
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = list(pdf.pages)
                if not pages:  # pragma: no cover - _probe already guards zero pages
                    raise InvoiceExtractionError("zero-page PDF")
                page_texts = [(p.extract_text() or "") for p in pages]
                total_chars = sum(len(t) for t in page_texts)
                if total_chars < _MIN_CHARS_PER_PAGE * len(pages):
                    return self._fallback._needs_ocr(len(pages), page_texts)
                is_tally = _is_tally_tax_invoice("\n".join(page_texts))
                words: list[_Word] = []
                for i, p in enumerate(pages, start=1):
                    words.extend(_words_of_page(p, i))
                tables: list[list[list[str]]] = []
                if not is_tally:
                    for p in pages:
                        for tbl in (p.extract_tables() or []):
                            tables.append([[("" if c is None else str(c)) for c in row]
                                           for row in tbl])
        except InvoiceExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - pdfplumber raises many types on bad input
            raise InvoiceExtractionError("could not read the PDF text layer") from exc

        raw_text = _ILLEGAL_CTRL.sub("", "\n".join(page_texts))
        if is_tally:
            return _build(page_count, raw_text, words)
        return self._fallback._build(page_count, raw_text, words, tables)
