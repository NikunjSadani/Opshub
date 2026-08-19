"""Zero-cost text-layer GST-invoice extractor (`TextLayerExtractor`, "text_layer/1.0").

The one engine behind the `Extractor` seam today: pure-python, deterministic, egress-locked
(no OCR, no network, no paid call). It reads the PDF's *existing* text layer with
`pdfplumber` (word boxes + ruled tables) and probes structure with `pypdf`, then maps what it
finds onto the engine-neutral `canonical` schema, attaching a CALIBRATED confidence + review
status to every field.

House conventions carried over from the challan module and honoured here:
  * **Money is ALWAYS integer paise** via `Decimal` × 100 `ROUND_HALF_UP`, never float. PDF
    free-text needs its own currency-tolerant tokenizer (`_money_to_paise`), but the
    arithmetic discipline is exactly `challan.parsing.parse_paise` (reused underneath).
  * **Every helper returns `None`/a clean status on junk** — a malformed cell yields a
    MISSING/LOW_CONFIDENCE field, never a 500.
  * **Reuse, do not re-implement:** `masterdata.normalize` for GSTIN checksum + state codes +
    text keys; `challan.parsing` for the grouped-number → paise discipline.

Column mapping is resolved **by header, never by index** (vendor column order varies); the
arithmetic cross-checks and the confidence ladder both follow the frozen design doc.

Unreadable / encrypted / zero-page / non-PDF input raises `InvoiceExtractionError`, a clean
domain error the service maps to a REJECTED quality-gate (never a bare 500 / stack trace).
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pdfplumber
from pypdf import PdfReader

from app.modules.challan.parsing import _ILLEGAL_CTRL, _MAX_MONEY_PAISE
from app.modules.challan.parsing import parse_paise as _grouped_paise
from app.modules.challan.parsing import parse_qty as _grouped_qty
from app.modules.expense.canonical import (
    CANONICAL_SCHEMA_VERSION,
    LINE_TAX_TOL_PAISE,
    REQUIRED_FIELD_PATHS,
    TOTALS_TOL_PAISE,
    ArithmeticChecks,
    DateField,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceHeader,
    InvoiceLine,
    InvoiceTotals,
    MoneyField,
    NumField,
    TextField,
)
from app.modules.masterdata.normalize import (
    STATE_NAME_TO_CODE,
    collapse_ws,
    match_key,
    valid_gstin,
)

NAME = "text_layer/1.0"

# Text-layer gate: a real text layer carries far more than a title-page's worth of glyphs;
# a scanned image yields ~0. Threshold per the design (§Extraction step 1).
_MIN_CHARS_PER_PAGE = 20

# Calibrated confidence ladder (design table). Not raw heuristic scores.
_CONF_STRONG = 0.95  # anchored + type-valid + arithmetic-corroborated
_CONF_OK = 0.80      # anchored + type-valid, no arithmetic corroboration
_CONF_WEAK = 0.50    # positional / weak-sourced / arithmetic-fails / checksum-fails

# GSTIN structure: 2 state digits, 5 PAN letters, 4 PAN digits, PAN letter, entity char,
# a literal 'Z', a check char. Checksum validity is a separate `valid_gstin` call.
_GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]\b")

# A clean, thousands-grouped rupee CORE (no sign, no leftover chars) — the WHOLE cell must
# match after sign/paren/currency stripping, so a malformed money cell (trailing sign, a
# European decimal comma, a fullwidth comma, leftover glyphs) is rejected rather than
# silently truncated to its first numeric token.
_ASCII_MONEY_RE = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")
_NUM_RE = re.compile(r"[-+]?[0-9][0-9,]*(?:\.[0-9]+)?")

# Currency ornaments that precede a money token in a PDF cell/label.
_CURRENCY_RE = re.compile(r"[₹]|(?i:rs\.?|inr|rupees)")


class InvoiceExtractionError(Exception):
    """Unreadable / encrypted / zero-page / non-PDF input.

    The service maps this to a REJECTED quality-gate with a clean operator message; the
    server-side detail is logged. It is NEVER allowed to surface as a bare 500 / stack.
    """


# --------------------------------------------------------------------------- primitives

@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int


@dataclass
class _Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    page: int

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


@dataclass
class _Gstin:
    value: str
    page: int
    x0: float
    top: float
    x1: float
    bottom: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)


def _money_to_paise(text: str | None) -> int | None:
    """Currency-tolerant free-text → SIGNED integer paise, validating the WHOLE cell.

    Strips ₹/Rs./INR ornaments, then peels an explicit sign — accounting parentheses
    ``(1,180.00)`` and a trailing ``1,180.00-`` both mean NEGATIVE — before requiring the
    remaining core to be a clean thousands-grouped number with NOTHING left over. Unlike a
    first-token grab, this makes a malformed cell (a European decimal comma ``1.234,50``, a
    fullwidth comma, a stray glyph) fail closed to ``None`` instead of a silently-wrong value.
    The core then defers to `challan.parsing.parse_paise` (Decimal × 100 ROUND_HALF_UP +
    grouping validation), and the result is CLAMPED to the challan money ceiling
    (`_MAX_MONEY_PAISE`) so an absurd/overflowing figure becomes None (never an int8 persist
    overflow). Junk → None, never a raise.
    """
    if text is None:
        return None
    s = _CURRENCY_RE.sub(" ", text).strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):     # accounting negative
        negative = not negative
        s = s[1:-1].strip()
    if s[:1] in "+-":                              # leading sign
        negative ^= (s[0] == "-")
        s = s[1:].strip()
    if s[-1:] in "+-":                             # trailing sign (e.g. "1,180.00-")
        negative ^= (s[-1] == "-")
        s = s[:-1].strip()
    if " " in s or _ASCII_MONEY_RE.fullmatch(s) is None:
        return None                               # leftover junk / EU comma / fullwidth → None
    paise = _grouped_paise(s)                      # grouping validation (Indian/Western)
    if paise is None:
        return None
    paise = -paise if negative else paise
    if abs(paise) > _MAX_MONEY_PAISE:              # clamp: out-of-range → None/MISSING
        return None
    return paise


def _num_or_none(text: str | None) -> Decimal | None:
    """Free-text → finite Decimal (qty / gst%), reusing the challan numeric discipline."""
    if text is None:
        return None
    m = _NUM_RE.search(text.replace("%", " "))
    if m is None:
        return None
    return _grouped_qty(m.group(0))


_DATE_FORMATS = (
    "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
    "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
    "%d-%m-%y", "%d/%m/%y",
)
# DMY substrings inside a longer value ("Dated: 15-05-2026 ...").
_DMY_NUMERIC = re.compile(r"\b([0-9]{1,2})[-/.]([0-9]{1,2})[-/.]([0-9]{2,4})\b")
_DMY_MONTH = re.compile(r"\b([0-9]{1,2})[-\s]([A-Za-z]{3,9})[-\s]([0-9]{2,4})\b")
_ISO = re.compile(r"\b([0-9]{4})-([0-9]{2})-([0-9]{2})\b")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _y4(year: int) -> int:
    """Expand a 2-digit year (DMY tokenizer) to 20xx; pass a 4-digit year through."""
    return 2000 + year if year < 100 else year


def _parse_invoice_date(text: str) -> date | None:
    """DMY-first tokenizer → `date` (DD-MM-YYYY / DD/MM/YYYY / DD-Mon-YYYY / ISO). None on junk."""
    s = collapse_ws(text)
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = _DMY_NUMERIC.search(s)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), _y4(int(m.group(3)))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    m = _DMY_MONTH.search(s)
    if m:
        mon = _MONTHS.get(m.group(2).lower()[:4]) or _MONTHS.get(m.group(2).lower()[:3])
        if mon is not None:
            try:
                return date(_y4(int(m.group(3))), mon, int(m.group(1)))
            except ValueError:
                return None
    m = _ISO.search(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- line rebuild

def _words_of_page(page: object, page_no: int) -> list[_Word]:
    """Extract word boxes for one page, coercing pdfplumber's dicts into typed `_Word`s."""
    out: list[_Word] = []
    for w in page.extract_words(use_text_flow=False):  # type: ignore[attr-defined]
        try:
            out.append(_Word(
                text=_ILLEGAL_CTRL.sub("", str(w["text"])),
                x0=float(w["x0"]), x1=float(w["x1"]),
                top=float(w["top"]), bottom=float(w["bottom"]),
                page=page_no,
            ))
        except (KeyError, TypeError, ValueError):  # pragma: no cover - defensive
            continue
    return out


def _rebuild_lines(words: list[_Word], y_tol: float = 3.0) -> list[_Line]:
    """Group words into visual lines by (page, top) and order each line left→right."""
    lines: list[_Line] = []
    by_page: dict[int, list[_Word]] = {}
    for w in words:
        by_page.setdefault(w.page, []).append(w)
    for page_no in sorted(by_page):
        rows: list[list[_Word]] = []
        for w in sorted(by_page[page_no], key=lambda w: (w.top, w.x0)):
            placed = False
            for row in rows:
                if abs(row[0].top - w.top) <= y_tol:
                    row.append(w)
                    placed = True
                    break
            if not placed:
                rows.append([w])
        for row in rows:
            row.sort(key=lambda w: w.x0)
            lines.append(_Line(
                text=" ".join(w.text for w in row),
                x0=min(w.x0 for w in row), x1=max(w.x1 for w in row),
                top=min(w.top for w in row), bottom=max(w.bottom for w in row),
                page=page_no,
            ))
    lines.sort(key=lambda ln: (ln.page, ln.top, ln.x0))
    return lines


def _find_first(lines: list[_Line], pattern: re.Pattern[str]) -> tuple[str, _Line] | None:
    """First line whose `pattern` captures a non-empty group 1 → (value, line)."""
    for ln in lines:
        m = pattern.search(ln.text)
        if m and m.group(1).strip():
            return m.group(1).strip(), ln
    return None


def _find_last(lines: list[_Line], pattern: re.Pattern[str]) -> tuple[str, _Line] | None:
    """Last matching line (totals sit at the bottom, below the item table)."""
    found: tuple[str, _Line] | None = None
    for ln in lines:
        m = pattern.search(ln.text)
        if m and m.group(1).strip():
            found = (m.group(1).strip(), ln)
    return found


# --------------------------------------------------------------------------- label anchors

_INV_NO_RE = re.compile(
    r"Invoice\s*(?:No|Number|Num|#)\b\.?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]*)",
    re.IGNORECASE)
_INV_DATE_RE = re.compile(
    r"(?:Invoice\s*Date|Inv\.?\s*Date|Dated|Date)\b\.?\s*[:\-]?\s*"
    r"([0-9]{1,2}[-/.\s][A-Za-z0-9]{1,9}[-/.\s][0-9]{2,4}|[0-9]{4}-[0-9]{2}-[0-9]{2})",
    re.IGNORECASE,
)
_POS_RE = re.compile(r"Place\s*of\s*Supply\b\.?\s*[:\-]?\s*(.+)", re.IGNORECASE)
_PO_RE = re.compile(r"(?:P\.?\s*O\.?\s*(?:No|Ref|Number)?|Purchase\s*Order)\b\.?\s*[:\-]?\s*"
                    r"([A-Za-z0-9][A-Za-z0-9\-/]*)", re.IGNORECASE)
_BILLTO_RE = re.compile(r"Bill\s*To\b\.?\s*[:\-]?\s*(.*)", re.IGNORECASE)

_TOTAL_TAXABLE_RE = re.compile(
    r"(?:Total\s*Taxable(?:\s*Value)?|Taxable\s*Value|Taxable\s*Amount)\s*[:\-]?\s*"
    r"([₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
_TOTAL_CGST_RE = re.compile(r"\bCGST\b\s*[:\-]?\s*([₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)",
                            re.IGNORECASE)
_TOTAL_SGST_RE = re.compile(r"\bSGST\b\s*[:\-]?\s*([₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)",
                            re.IGNORECASE)
_TOTAL_IGST_RE = re.compile(r"\bIGST\b\s*[:\-]?\s*([₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)",
                            re.IGNORECASE)
# Round-off is the one signed total; capture an optional accounting-parenthesised value
# ("(0.30)" == −₹0.30) WITH its parens so `_money_to_paise` can read the sign (M3).
_ROUND_OFF_RE = re.compile(r"Round(?:ing)?\s*[- ]?\s*Off\s*[:\-]?\s*"
                           r"(\(?\s*[₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?\s*\)?)", re.IGNORECASE)
_GRAND_TOTAL_RE = re.compile(
    r"(?:Grand\s*Total|Invoice\s*Total|Total\s*(?:Invoice\s*)?Amount|Amount\s*Payable|"
    r"Net\s*Payable)\s*[:\-]?\s*([₹]?\s*[-+]?[0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
_WORDS_RE = re.compile(r"(?:Amount\s*(?:Chargeable\s*)?\(?in\s*words\)?|In\s*Words|Rupees)\b"
                       r"\s*[:\-]?\s*(.+)", re.IGNORECASE)


# --------------------------------------------------------------------------- table mapping

# Canonical column -> ordered synonyms (match against a whitespace-collapsed, casefolded
# header cell). Resolution is by header text, NEVER by position. More-specific synonyms are
# listed so an ambiguous "amount" loses to a "taxable value" / "total" that names itself.
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "description": ("description", "particulars", "item description", "goods description",
                    "item", "goods", "service", "product", "nature of goods"),
    "hsn_sac": ("hsn/sac", "hsn sac", "hsn", "sac", "hsn code", "hsn/ sac"),
    "quantity": ("quantity", "qty", "qnty", "nos"),
    "unit": ("unit", "uom", "u.o.m", "per"),
    "unit_rate_paise": ("unit rate", "rate per unit", "rate", "price", "unit price", "mrp"),
    "gst_rate": ("gst rate", "gst%", "gst %", "tax rate", "tax%", "rate%", "gst"),
    "cgst_paise": ("cgst amount", "cgst amt", "cgst"),
    "sgst_paise": ("sgst amount", "sgst amt", "sgst", "utgst"),
    "igst_paise": ("igst amount", "igst amt", "igst"),
    "taxable_paise": ("taxable value", "taxable amount", "taxable", "net amount",
                      "assessable value", "amount", "value"),
    "line_total_paise": ("total amount", "line total", "total value", "grand total", "total"),
}

# Summary-row detection (M6/L7). A row is a sub-total / tax / summary row — NOT a real item —
# only when its FIRST cell is precisely one of these labels, so a genuine product whose name
# merely BEGINS with a keyword ("Total Station Survey Kit", "Discount Voucher") is kept.
#   * EXACT set: the whole first cell (trailing ':' tolerated) equals the label.
#   * PREFIX set: genuine ledger-adjustment / words rows that legitimately carry trailing text.
_NON_ITEM_EXACT: frozenset[str] = frozenset({
    "sub total", "subtotal", "sub-total", "total", "grand total", "discount", "round off",
    "round-off", "rounding", "rounding off", "cgst", "sgst", "igst", "utgst",
    "taxable value", "total taxable value", "total taxable",
})
_NON_ITEM_PREFIX: tuple[str, ...] = ("add:", "less:", "amount in words", "amount chargeable")


def _map_headers(header: list[str]) -> dict[str, int]:
    """Resolve canonical column → cell index by fuzzy header match (never by index).

    Each canonical column claims the header cell with the strongest synonym match; on a tie
    an earlier (more-specific) synonym and a not-yet-claimed cell win, so "amount" cannot
    steal the slot a "taxable value" header names outright.
    """
    keys = [match_key(h) for h in header]
    scored: list[tuple[int, int, str, int]] = []  # (-score, synonym_rank, canonical, idx)
    for canonical, synonyms in _COLUMN_SYNONYMS.items():
        for idx, key in enumerate(keys):
            if not key:
                continue
            for rank, syn in enumerate(synonyms):
                if key == syn:
                    scored.append((-3, rank, canonical, idx))
                    break
                if re.search(rf"\b{re.escape(syn)}\b", key):
                    scored.append((-2, rank, canonical, idx))
                    break
                if syn in key:
                    scored.append((-1, rank, canonical, idx))
                    break
    scored.sort()
    mapping: dict[str, int] = {}
    used: set[int] = set()
    for _score, _rank, canonical, idx in scored:
        if canonical in mapping or idx in used:
            continue
        mapping[canonical] = idx
        used.add(idx)
    return mapping


def _looks_like_header(row: list[str]) -> bool:
    """A header row maps ≥3 canonical columns (used to detect a multi-page continuation)."""
    return len(_map_headers(row)) >= 3


def _cell(row: list[str], mapping: dict[str, int], canonical: str) -> str | None:
    idx = mapping.get(canonical)
    if idx is None or idx >= len(row):
        return None
    val = row[idx]
    return val if (val is not None and str(val).strip()) else None


@dataclass
class _RawLine:
    """Parsed-but-not-yet-enveloped item row (paise ints / Decimals + verbatim strings)."""
    raw: dict[str, str]
    description: str | None
    hsn: str | None
    quantity: Decimal | None
    unit: str | None
    unit_rate: int | None
    taxable: int | None
    gst_rate: Decimal | None
    cgst: int | None
    sgst: int | None
    igst: int | None
    line_total: int | None


def _is_summary_first_cell(raw_first: str) -> bool:
    """True iff a row's FIRST cell is precisely a summary/tax label (anchored, not a prefix
    of a real product name)."""
    key = match_key(raw_first or "")
    if not key:
        return False
    if key.rstrip(":").strip() in _NON_ITEM_EXACT:
        return True
    return any(key.startswith(p) for p in _NON_ITEM_PREFIX)


def _is_item_row(raw_first: str, r: _RawLine) -> bool:
    """Keep a row only if it isn't a summary row and carries real item money."""
    if _is_summary_first_cell(raw_first):
        return False
    # Needs at least a description or HSN AND at least one money figure to be an item.
    has_desc = bool((r.description or "").strip()) or bool((r.hsn or "").strip())
    has_money = any(v is not None for v in (r.taxable, r.line_total, r.unit_rate))
    return has_desc and has_money


def _parse_tables(tables: list[list[list[str]]]) -> list[_RawLine]:
    """Stitch multi-page tables, map columns by header, and return item rows only.

    The first table that carries a recognizable header row establishes the column mapping;
    later tables without their own header are treated as continuations of it (multi-page
    line items). Non-item summary rows (sub-total / tax / discount) are skipped.
    """
    mapping: dict[str, int] = {}
    out: list[_RawLine] = []
    for table in tables:
        rows = [[("" if c is None else str(c)) for c in row] for row in table if row]
        if not rows:
            continue
        start = 0
        if _looks_like_header(rows[0]):
            mapping = _map_headers(rows[0])
            start = 1
        if not mapping:
            continue  # a table we can't map to canonical columns (e.g. a stray layout table)
        for row in rows[start:]:
            if all(not str(c).strip() for c in row):
                continue
            r = _RawLine(
                raw={"row": " | ".join(str(c) for c in row)},
                description=_cell(row, mapping, "description"),
                hsn=_cell(row, mapping, "hsn_sac"),
                quantity=_num_or_none(_cell(row, mapping, "quantity")),
                unit=_cell(row, mapping, "unit"),
                unit_rate=_money_to_paise(_cell(row, mapping, "unit_rate_paise")),
                taxable=_money_to_paise(_cell(row, mapping, "taxable_paise")),
                gst_rate=_num_or_none(_cell(row, mapping, "gst_rate")),
                cgst=_money_to_paise(_cell(row, mapping, "cgst_paise")),
                sgst=_money_to_paise(_cell(row, mapping, "sgst_paise")),
                igst=_money_to_paise(_cell(row, mapping, "igst_paise")),
                line_total=_money_to_paise(_cell(row, mapping, "line_total_paise")),
            )
            first = row[0] if row else ""
            if _is_item_row(first, r):
                out.append(r)
    return out


# --------------------------------------------------------------------------- borderless fallback

def _group_word_rows(words: list[_Word], y_tol: float = 3.0) -> list[list[_Word]]:
    """Group one page's words into visual rows by their `top` coordinate (left→right)."""
    rows: list[list[_Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        for row in rows:
            if abs(row[0].top - w.top) <= y_tol:
                row.append(w)
                break
        else:
            rows.append([w])
    for row in rows:
        row.sort(key=lambda w: w.x0)
    return rows


def _word_column(text: str) -> str | None:
    """The canonical column a SINGLE header word names (best synonym), or None if generic.

    Uses the same exact/word-boundary/substring ladder as `_map_headers`, so it agrees with
    how a resolved header cell is mapped. This lets a multi-word label be folded only when its
    trailing word reinforces the SAME column (a generic "Value" after "Taxable"), never when
    it names a DIFFERENT one (a "Rate" after "Unit").
    """
    key = match_key(text)
    if not key:
        return None
    best: str | None = None
    best_score = 0
    best_rank = 1 << 30
    for canonical, synonyms in _COLUMN_SYNONYMS.items():
        for rank, syn in enumerate(synonyms):
            if key == syn:
                score = 3
            elif re.search(rf"\b{re.escape(syn)}\b", key):
                score = 2
            elif syn in key:
                score = 1
            else:
                continue
            if (score, -rank) > (best_score, -best_rank):
                best, best_score, best_rank = canonical, score, rank
            break
    return best


@dataclass
class _HeaderCell:
    text: str
    center: float


def _header_cells(header: list[_Word]) -> list[_HeaderCell]:
    """Segment a header row's words into COLUMN CELLS, folding a multi-word label ("Taxable
    Value") into ONE span so it isn't split into two phantom columns.

    A cell grows to include the next word only while that word is GENERIC (maps to no column)
    or reinforces the SAME canonical column the cell already names — so "Taxable" + "Value"
    fold together (both → taxable) while "Unit" + "Rate" stay apart (unit vs unit-rate). This
    is alignment-independent: it never relies on inter-word gaps, which collapse when money
    headers are right-aligned. Each cell's `center` is its span midpoint; data words are later
    bucketed to the NEAREST center, so a right-aligned value files under its own column.
    """
    words = sorted(header, key=lambda w: w.x0)
    cells: list[_HeaderCell] = []
    i = 0
    while i < len(words):
        group = [words[i]]
        col = _word_column(words[i].text)
        i += 1
        while i < len(words):
            nxt = _word_column(words[i].text)
            if nxt is not None and nxt != col:
                break
            if col is None:
                col = nxt
            group.append(words[i])
            i += 1
        text = " ".join(w.text for w in group)
        center = (min(w.x0 for w in group) + max(w.x1 for w in group)) / 2.0
        cells.append(_HeaderCell(text=text, center=center))
    return cells


def _nearest_cell(x_center: float, cells: list[_HeaderCell]) -> int:
    """Index of the header cell whose center is nearest `x_center`."""
    return min(range(len(cells)), key=lambda j: abs(cells[j].center - x_center))


def _words_table(words: list[_Word]) -> list[list[list[str]]]:
    """Reconstruct the item grid from word boxes when `extract_tables()` found no ruled grid.

    A borderless invoice carries no edges for pdfplumber's table detector, so we rebuild the
    grid geometrically: per page, group words into rows, find the header row by column-synonym
    match (never by index), fold the header words into column CELLS (multi-word labels kept
    whole), then assign every following row's words to the column whose CENTER is nearest the
    word's own center. Nearest-center (rather than a left-edge band) is what keeps a
    right-aligned money value — whose x0 sits well under the next column's left edge — filed in
    its own column. The output has the same shape `extract_tables()` returns, so `_parse_tables`
    maps columns + drops the sub-total/tax/summary rows exactly as it does for a ruled table.
    """
    by_page: dict[int, list[_Word]] = {}
    for w in words:
        by_page.setdefault(w.page, []).append(w)
    out: list[list[list[str]]] = []
    for page_no in sorted(by_page):
        rows = _group_word_rows(by_page[page_no])
        header_idx = next(
            (i for i, row in enumerate(rows) if _looks_like_header([w.text for w in row])),
            None)
        if header_idx is None:
            continue
        cells = _header_cells(rows[header_idx])
        table: list[list[str]] = [[c.text for c in cells]]
        for row in rows[header_idx + 1:]:
            row_cells = [""] * len(cells)
            for w in row:
                j = _nearest_cell((w.x0 + w.x1) / 2.0, cells)
                row_cells[j] = f"{row_cells[j]} {w.text}".strip() if row_cells[j] else w.text
            table.append(row_cells)
        out.append(table)
    return out


# --------------------------------------------------------------------------- field builders

def _missing(engine: str = NAME) -> Field:  # type: ignore[type-arg]
    return Field(value_normalized=None, value_raw="", confidence=0.0,
                 source_engine=engine, status=FieldStatus.MISSING)


def _mk(value: object, raw: str, conf: float, status: FieldStatus,
        line: _Line | None) -> Field:  # type: ignore[type-arg]
    return Field(
        value_normalized=value, value_raw=raw, confidence=conf, source_engine=NAME,
        page=line.page if line else None, bbox=line.bbox if line else None, status=status,
    )


# --------------------------------------------------------------------------- arithmetic

@dataclass
class _Corrob:
    """Which corroborating checks ACTUALLY RAN (had the inputs to run). A check that could not
    run must NOT lend a field the top (0.95) confidence band — that would be a vacuous claim.
    """
    taxable_ran: bool     # Σ-line-taxable vs total-taxable was computable
    per_line_ran: bool    # at least one line had both a taxable and a gst_rate to cross-check
    grand_ran: bool       # taxable + taxes + round-off vs grand-total was computable
    supply_ran: bool      # supply type was determinable AND a tax head was present to check


def _supply_type(pos_code: str | None, supplier_code: str | None,
                 buyer_code: str | None) -> bool | None:
    """Intra (True) / inter (False) / indeterminate (None).

    Prefer place-of-supply vs supplier; when POS is absent, fall back to supplier-vs-buyer
    GSTIN state codes so the split can still be checked. Only genuinely unknown → None.
    """
    if pos_code and supplier_code:
        return pos_code == supplier_code
    if supplier_code and buyer_code:
        return supplier_code == buyer_code
    return None


def _supply_type_consistent(pos_code: str | None, supplier_code: str | None,
                            buyer_code: str | None, cgst: int, sgst: int, igst: int) -> bool:
    """Lenient contradiction check: True unless the tax split positively disagrees.

    Intra-state must be CGST+SGST with IGST=0; inter-state must be IGST with CGST=SGST=0.
    When place-of-supply is absent the supply type is INFERRED from the supplier-vs-buyer
    GSTIN state codes (M4) so a wrong tax head is still caught; only a truly indeterminate
    supply type → True (no false review flag).
    """
    intra = _supply_type(pos_code, supplier_code, buyer_code)
    if intra is None:
        return True
    if intra:                          # intra-state
        return igst == 0 and (cgst > 0 or sgst > 0)
    return cgst == 0 and sgst == 0     # inter-state


# --------------------------------------------------------------------------- the extractor

class TextLayerExtractor:
    """Deterministic text-layer GST-invoice extractor (see module docstring)."""

    name = NAME

    def extract(self, pdf_bytes: bytes, *, doc_type: str = "gst_invoice") -> ExtractedInvoice:
        if doc_type != "gst_invoice":
            raise ValueError(f"unsupported doc_type {doc_type!r} (only 'gst_invoice')")

        page_count = self._probe(pdf_bytes)

        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = list(pdf.pages)
                if not pages:  # pragma: no cover - _probe already guards zero pages
                    raise InvoiceExtractionError("zero-page PDF")
                page_texts = [(p.extract_text() or "") for p in pages]
                total_chars = sum(len(t) for t in page_texts)
                if total_chars < _MIN_CHARS_PER_PAGE * len(pages):
                    return self._needs_ocr(len(pages), page_texts)
                words: list[_Word] = []
                for i, p in enumerate(pages, start=1):
                    words.extend(_words_of_page(p, i))
                tables: list[list[list[str]]] = []
                for p in pages:
                    for tbl in (p.extract_tables() or []):
                        tables.append([[("" if c is None else str(c)) for c in row]
                                       for row in tbl])
        except InvoiceExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001 - pdfplumber raises many types on bad input
            raise InvoiceExtractionError("could not read the PDF text layer") from exc

        raw_text = _ILLEGAL_CTRL.sub("", "\n".join(page_texts))
        return self._build(page_count, raw_text, words, tables)

    # -- structure probe ----------------------------------------------------

    @staticmethod
    def _probe(pdf_bytes: bytes) -> int:
        """pypdf structural probe. Raises `InvoiceExtractionError` on bad/encrypted input."""
        if not pdf_bytes:
            raise InvoiceExtractionError("empty upload — not a PDF")
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception as exc:  # noqa: BLE001 - pypdf raises many types on a bad file
            raise InvoiceExtractionError("unreadable file — not a valid PDF") from exc
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")  # try the empty owner/user password
            except Exception as exc:  # noqa: BLE001
                raise InvoiceExtractionError("encrypted PDF — cannot read") from exc
            if int(opened) == 0:
                raise InvoiceExtractionError("encrypted PDF — cannot read")
        try:
            n = len(reader.pages)
        except Exception as exc:  # noqa: BLE001
            raise InvoiceExtractionError("corrupt PDF — page tree unreadable") from exc
        if n == 0:
            raise InvoiceExtractionError("zero-page PDF")
        return n

    # -- needs-OCR soft failure --------------------------------------------

    def _needs_ocr(self, page_count: int, page_texts: list[str]) -> ExtractedInvoice:
        """No usable text layer → all fields MISSING, parked for the deferred OCR engine."""
        raw_text = _ILLEGAL_CTRL.sub("", "\n".join(page_texts))
        header = InvoiceHeader(
            supplier_name=_missing(), supplier_gstin=_missing(), supplier_address=_missing(),
            buyer_name=_missing(), buyer_gstin=_missing(), buyer_address=_missing(),
            invoice_number=_missing(), invoice_date=_missing(), place_of_supply=_missing(),
            po_ref=_missing(),
        )
        totals = InvoiceTotals(
            total_taxable_paise=_missing(), total_cgst_paise=_missing(),
            total_sgst_paise=_missing(), total_igst_paise=_missing(),
            round_off_paise=_missing(), grand_total_paise=_missing(),
            amount_in_words=_missing(),
        )
        arithmetic = ArithmeticChecks(
            lines_sum_matches_taxable=True, per_line_tax_consistent=True,
            totals_add_to_grand=True, supply_type_consistent=True, max_abs_delta_paise=0,
        )
        reasons = ["no text layer — scanned/image PDF, OCR deferred"]
        return ExtractedInvoice(
            schema_version=CANONICAL_SCHEMA_VERSION, doc_type="gst_invoice", source_engine=NAME,
            page_count=page_count, needs_ocr=True, review_needed=True, review_reasons=reasons,
            header=header, lines=[], totals=totals, arithmetic=arithmetic, raw_text=raw_text,
            content_hash=_content_hash(raw_text),
            dedup_key=_dedup_key(None, None, None, None),
        )

    # -- the real build -----------------------------------------------------

    def _build(self, page_count: int, raw_text: str, words: list[_Word],
               tables: list[list[list[str]]]) -> ExtractedInvoice:
        lines = _rebuild_lines(words)

        # ---- GSTINs (supplier = top-most block; buyer = strictly below "Bill To") -----
        cands = _gstin_candidates(words)
        billto = next((ln for ln in lines if _BILLTO_RE.search(ln.text)), None)
        supplier_c, buyer_c, supplier_ambiguous, buyer_ambiguous = _assign_gstins(cands, billto)

        supplier_code = supplier_c.value[:2] if supplier_c else None
        buyer_code = buyer_c.value[:2] if buyer_c else None

        # ---- place of supply (needed for the supply-type check) ----------------------
        pos_hit = _find_first(lines, _POS_RE)
        pos_code, pos_norm = _normalize_pos(pos_hit[0]) if pos_hit else (None, None)

        # ---- line items --------------------------------------------------------------
        # Ruled grid first (edge-based extract_tables); if that yields no item rows the
        # invoice is likely borderless, so rebuild the grid from word geometry and re-map.
        raw_lines = _parse_tables(tables)
        if not raw_lines:
            raw_lines = _parse_tables(_words_table(words))

        # ---- totals (label-anchored) -------------------------------------------------
        tt_hit = _find_last(lines, _TOTAL_TAXABLE_RE)
        cg_hit = _find_last(lines, _TOTAL_CGST_RE)
        sg_hit = _find_last(lines, _TOTAL_SGST_RE)
        ig_hit = _find_last(lines, _TOTAL_IGST_RE)
        ro_hit = _find_last(lines, _ROUND_OFF_RE)
        gt_hit = _find_last(lines, _GRAND_TOTAL_RE)
        words_hit = _find_last(lines, _WORDS_RE)

        total_taxable = _money_to_paise(tt_hit[0]) if tt_hit else None
        total_cgst = _money_to_paise(cg_hit[0]) if cg_hit else None
        total_sgst = _money_to_paise(sg_hit[0]) if sg_hit else None
        total_igst = _money_to_paise(ig_hit[0]) if ig_hit else None
        round_off = _money_to_paise(ro_hit[0]) if ro_hit else None
        grand_total = _money_to_paise(gt_hit[0]) if gt_hit else None

        # ---- arithmetic cross-checks (integer paise) ---------------------------------
        arithmetic, corrob = _run_arithmetic(
            raw_lines, total_taxable, total_cgst, total_sgst, total_igst, round_off,
            grand_total, pos_code, supplier_code, buyer_code,
        )

        # ---- envelope every field with a calibrated confidence -----------------------
        header = _build_header(lines, supplier_c, buyer_c, billto, pos_hit, pos_norm,
                               pos_code, arithmetic, corrob, supplier_ambiguous,
                               buyer_ambiguous)
        invoice_lines = _build_lines(raw_lines, arithmetic, corrob)
        totals = _build_totals(tt_hit, cg_hit, sg_hit, ig_hit, ro_hit, gt_hit, words_hit,
                               total_taxable, total_cgst, total_sgst, total_igst, round_off,
                               grand_total, arithmetic, corrob)

        # ---- review gate (collect ALL reasons) ---------------------------------------
        reasons = _review_reasons(header, totals, invoice_lines, supplier_c, buyer_c, arithmetic)

        return ExtractedInvoice(
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


# --------------------------------------------------------------------------- GSTIN helpers

def _gstin_candidates(words: list[_Word]) -> list[_Gstin]:
    """Every GSTIN-shaped token with its box (checksum validity assessed later)."""
    out: list[_Gstin] = []
    for w in words:
        m = _GSTIN_RE.search(w.text)
        if m:
            out.append(_Gstin(value=m.group(0), page=w.page, x0=w.x0, top=w.top,
                              x1=w.x1, bottom=w.bottom))
    return out


# Two buyer candidates whose tops differ by less than this (PDF points ≈ one text line) sit
# in the same band beneath the anchor → we cannot tell the buyer's GSTIN from a neighbour's
# (e.g. a "Ship To" block), so we keep the nearest but mark it low-confidence, never a guess.
_GSTIN_ROW_BAND = 18.0


def _assign_gstins(
    cands: list[_Gstin], billto: _Line | None
) -> tuple[_Gstin | None, _Gstin | None, bool, bool]:
    """Assign (supplier, buyer, supplier_ambiguous, buyer_ambiguous) from the candidates.

    Supplier is the top-most GSTIN (suppliers head the masthead). The buyer's GSTIN must lie
    strictly BELOW the "Bill To" anchor (same page / its column band beneath it) — preventing
    the silent supplier⇄buyer swap when the supplier's GSTIN sits just ABOVE the anchor. If no
    candidate lies below, the buyer is UNKNOWN (None) not guessed; two candidates sharing the
    first band below → nearest kept but flagged.

    A BUYER-FIRST masthead (the "Bill To" anchor sits ABOVE the top-most GSTIN, so the
    top-most is really the buyer's) breaks the top-most-is-supplier assumption — we detect it
    (`supplier.top > billto.top`) and flag BOTH identities uncertain (a required-field
    LOW_CONFIDENCE → review) rather than silently swap.
    """
    if not cands:
        return None, None, False, False
    supplier = min(cands, key=lambda c: (c.page, c.top, c.x0))
    buyer_first = (billto is not None and supplier.page == billto.page
                   and supplier.top > billto.top)
    if billto is None or len(cands) < 2:
        return supplier, None, buyer_first, buyer_first

    below = [c for c in cands
             if c is not supplier and c.page == billto.page and c.top > billto.top]
    if not below:
        return supplier, None, buyer_first, buyer_first

    below.sort(key=lambda c: (c.top, abs(c.x0 - billto.x0)))
    buyer = below[0]
    ambiguous = len(below) >= 2 and (below[1].top - buyer.top) < _GSTIN_ROW_BAND
    return supplier, buyer, buyer_first, (ambiguous or buyer_first)


def _normalize_pos(raw: str) -> tuple[str | None, str | None]:
    """"Karnataka (29)" / "Karnataka" → (code, "Name (NN)"). Unknown → (None, collapsed raw)."""
    text = collapse_ws(raw)
    m = re.search(r"\(?\b([0-9]{2})\b\)?", text)
    code = m.group(1) if m else None
    name_part = re.sub(r"\(?\b[0-9]{2}\b\)?", "", text).strip(" ()-:")
    if code is None:
        code = STATE_NAME_TO_CODE.get(match_key(name_part))
    if code is None:
        return None, (text or None)
    display_name = name_part or next(
        (k.title() for k, v in STATE_NAME_TO_CODE.items() if v == code), code)
    return code, f"{display_name} ({code})"


# --------------------------------------------------------------------------- header envelope

def _build_header(lines: list[_Line], supplier_c: _Gstin | None, buyer_c: _Gstin | None,
                  billto: _Line | None, pos_hit: tuple[str, _Line] | None,
                  pos_norm: str | None, pos_code: str | None,
                  arith: ArithmeticChecks, corrob: _Corrob,
                  supplier_ambiguous: bool, buyer_ambiguous: bool) -> InvoiceHeader:
    # The supply-type check only corroborates a GSTIN when it actually RAN (M5) — an
    # indeterminate/absent split must not lend the top confidence band.
    supply_ok = arith.supply_type_consistent and corrob.supply_ran

    supplier_gstin = _gstin_field(supplier_c, supply_ok, ambiguous=supplier_ambiguous)
    buyer_gstin = _gstin_field(buyer_c, supply_ok, ambiguous=buyer_ambiguous)

    inv_no_hit = _find_first(lines, _INV_NO_RE)
    invoice_number: TextField = (
        _mk(inv_no_hit[0], inv_no_hit[0], _CONF_OK, FieldStatus.OK, inv_no_hit[1])
        if inv_no_hit else _missing())

    inv_date_hit = _find_first(lines, _INV_DATE_RE)
    if inv_date_hit:
        parsed = _parse_invoice_date(inv_date_hit[0])
        invoice_date: DateField = _mk(
            parsed, inv_date_hit[0],
            _CONF_OK if parsed else _CONF_WEAK,
            FieldStatus.OK if parsed else FieldStatus.LOW_CONFIDENCE,
            inv_date_hit[1])
    else:
        invoice_date = _missing()

    if pos_hit and pos_code:
        place_of_supply: TextField = _mk(
            pos_norm, pos_hit[0], _CONF_STRONG if supply_ok else _CONF_OK,
            FieldStatus.OK, pos_hit[1])  # supply_ok already folds in corrob.supply_ran (M5)
    elif pos_hit:
        place_of_supply = _mk(pos_norm, pos_hit[0], _CONF_WEAK,
                              FieldStatus.LOW_CONFIDENCE, pos_hit[1])
    else:
        place_of_supply = _missing()

    po_hit = _find_first(lines, _PO_RE)
    po_ref: TextField = (_mk(po_hit[0], po_hit[0], _CONF_OK, FieldStatus.OK, po_hit[1])
                         if po_hit else _missing())

    supplier_name, supplier_address = _supplier_block(lines, billto, supplier_c)
    buyer_name, buyer_address = _buyer_block(lines, billto, buyer_c)

    return InvoiceHeader(
        supplier_name=supplier_name, supplier_gstin=supplier_gstin,
        supplier_address=supplier_address, buyer_name=buyer_name, buyer_gstin=buyer_gstin,
        buyer_address=buyer_address, invoice_number=invoice_number, invoice_date=invoice_date,
        place_of_supply=place_of_supply, po_ref=po_ref,
    )


def _gstin_field(c: _Gstin | None, supply_ok: bool, *, ambiguous: bool = False) -> TextField:
    """Envelope a GSTIN: valid+corroborated→0.95, valid→0.80, bad-checksum/ambiguous→0.50 LOW.

    An `ambiguous` buyer (two GSTINs sharing the first band below "Bill To") is a positional
    guess, so it is capped at LOW_CONFIDENCE even with a valid checksum — never a silent OK.
    """
    if c is None:
        return _missing()
    line = _Line(text=c.value, x0=c.x0, x1=c.x1, top=c.top, bottom=c.bottom, page=c.page)
    if valid_gstin(c.value):
        if ambiguous:
            return _mk(c.value, c.value, _CONF_WEAK, FieldStatus.LOW_CONFIDENCE, line)
        conf = _CONF_STRONG if supply_ok else _CONF_OK
        return _mk(c.value, c.value, conf, FieldStatus.OK, line)
    return _mk(c.value, c.value, _CONF_WEAK, FieldStatus.LOW_CONFIDENCE, line)


def _text_lines_only(lines: list[_Line]) -> list[_Line]:
    return [ln for ln in lines if ln.text.strip()]


def _supplier_block(lines: list[_Line], billto: _Line | None,
                    supplier_c: _Gstin | None) -> tuple[TextField, TextField]:
    """Positional supplier name/address (top block, above "Bill To") — weak by construction."""
    page1 = [ln for ln in _text_lines_only(lines) if ln.page == 1]
    top_limit = billto.top if (billto and billto.page == 1) else float("inf")
    block = [ln for ln in page1 if ln.top < top_limit]
    name_ln = next((ln for ln in block if not _is_noise_line(ln.text)), None)
    name: TextField = (_mk(collapse_ws(name_ln.text), name_ln.text, _CONF_WEAK,
                           FieldStatus.LOW_CONFIDENCE, name_ln)
                       if name_ln else _missing())
    addr = _address_between(block, name_ln, supplier_c)
    return name, addr


def _buyer_block(lines: list[_Line], billto: _Line | None,
                 buyer_c: _Gstin | None) -> tuple[TextField, TextField]:
    """Buyer name anchored on "Bill To" (0.80); address is the positional run beneath it."""
    if billto is None:
        return _missing(), _missing()
    m = _BILLTO_RE.search(billto.text)
    inline = collapse_ws(m.group(1)) if (m and m.group(1).strip()) else ""
    page_lines = [ln for ln in _text_lines_only(lines) if ln.page == billto.page]
    below = [ln for ln in page_lines if ln.top > billto.top]
    name_ln: _Line | None
    if inline:
        name: TextField = _mk(inline, billto.text, _CONF_OK, FieldStatus.OK, billto)
        name_ln = billto
    else:
        name_ln = next((ln for ln in below if not _is_noise_line(ln.text)), None)
        name = (_mk(collapse_ws(name_ln.text), name_ln.text, _CONF_WEAK,
                    FieldStatus.LOW_CONFIDENCE, name_ln) if name_ln else _missing())
    addr = _address_between(below, name_ln, buyer_c)
    return name, addr


def _address_between(block: list[_Line], after: _Line | None,
                     stop_at: _Gstin | None) -> TextField:
    """Join the positional lines between a name line and the block's GSTIN into an address."""
    if after is None:
        return _missing()
    stop_top = stop_at.top if stop_at else float("inf")
    picked = [ln for ln in block
              if ln.top > after.top and ln.top < stop_top and not _is_noise_line(ln.text)]
    if not picked:
        return _missing()
    text = collapse_ws(" ".join(ln.text for ln in picked))
    if not text:
        return _missing()
    return _mk(text, text, _CONF_WEAK, FieldStatus.LOW_CONFIDENCE, picked[0])


_NOISE_RE = re.compile(r"^(?:tax\s*invoice|invoice|gstin|gst\s*no|bill\s*to|ship\s*to)\b",
                       re.IGNORECASE)


def _is_noise_line(text: str) -> bool:
    """A title / label / GSTIN-only line that isn't a name or address."""
    t = collapse_ws(text)
    if not t or _NOISE_RE.match(t):
        return True
    return bool(_GSTIN_RE.search(t)) and len(t) <= 20


# --------------------------------------------------------------------------- line envelope

def _build_lines(raw_lines: list[_RawLine], arith: ArithmeticChecks,
                 corrob: _Corrob) -> list[InvoiceLine]:
    # Top band only when the corroborating checks BOTH passed AND actually ran (M5).
    corroborated = (arith.per_line_tax_consistent and arith.lines_sum_matches_taxable
                    and corrob.per_line_ran and corrob.taxable_ran)
    money_conf = _CONF_STRONG if corroborated else _CONF_OK
    out: list[InvoiceLine] = []
    for i, r in enumerate(raw_lines, start=1):
        out.append(InvoiceLine(
            line_no=i,
            description=_txt(r.description),
            hsn_sac=_txt(r.hsn),
            quantity=_numf(r.quantity),
            unit=_txt(r.unit),
            unit_rate_paise=_moneyf(r.unit_rate, money_conf),
            taxable_paise=_moneyf(r.taxable, money_conf),
            gst_rate=_numf(r.gst_rate),
            cgst_paise=_moneyf(r.cgst, money_conf),
            sgst_paise=_moneyf(r.sgst, money_conf),
            igst_paise=_moneyf(r.igst, money_conf),
            line_total_paise=_moneyf(r.line_total, money_conf),
        ))
    return out


def _txt(value: str | None) -> TextField:
    if value is None or not str(value).strip():
        return _missing()
    v = collapse_ws(str(value))
    return _mk(v, str(value), _CONF_OK, FieldStatus.OK, None)


def _numf(value: Decimal | None) -> NumField:
    if value is None:
        return _missing()
    return _mk(value, str(value), _CONF_OK, FieldStatus.OK, None)


def _moneyf(value: int | None, conf: float) -> MoneyField:
    if value is None:
        return _missing()
    return _mk(value, str(value), conf, FieldStatus.OK, None)


# --------------------------------------------------------------------------- totals envelope

def _build_totals(tt_hit: tuple[str, _Line] | None, cg_hit: tuple[str, _Line] | None,
                  sg_hit: tuple[str, _Line] | None, ig_hit: tuple[str, _Line] | None,
                  ro_hit: tuple[str, _Line] | None, gt_hit: tuple[str, _Line] | None,
                  words_hit: tuple[str, _Line] | None,
                  total_taxable: int | None, total_cgst: int | None, total_sgst: int | None,
                  total_igst: int | None, round_off: int | None, grand_total: int | None,
                  arith: ArithmeticChecks, corrob: _Corrob) -> InvoiceTotals:
    # Each total earns the top band only when its corroborating check passed AND ran (M5):
    # the taxable total is corroborated by the Σ-lines check, the tax heads by the per-line
    # tax check, and grand-total / round-off by the totals-add-up check.
    taxable_ok = arith.lines_sum_matches_taxable and corrob.taxable_ran
    grand_ok = arith.totals_add_to_grand and corrob.grand_ran
    tax_split_ok = arith.per_line_tax_consistent and corrob.per_line_ran

    return InvoiceTotals(
        total_taxable_paise=_total_field(tt_hit, total_taxable, taxable_ok),
        total_cgst_paise=_total_field(cg_hit, total_cgst, tax_split_ok),
        total_sgst_paise=_total_field(sg_hit, total_sgst, tax_split_ok),
        total_igst_paise=_total_field(ig_hit, total_igst, tax_split_ok),
        round_off_paise=_total_field(ro_hit, round_off, grand_ok),
        grand_total_paise=_total_field(gt_hit, grand_total, grand_ok),
        amount_in_words=(_mk(collapse_ws(words_hit[0]), words_hit[0], _CONF_OK,
                             FieldStatus.OK, words_hit[1]) if words_hit else _missing()),
    )


def _total_field(hit: tuple[str, _Line] | None, value: int | None,
                 corroborated: bool) -> MoneyField:
    if hit is None or value is None:
        return _missing()
    conf = _CONF_STRONG if corroborated else _CONF_OK
    return _mk(value, hit[0], conf, FieldStatus.OK, hit[1])


# --------------------------------------------------------------------------- arithmetic run

def _run_arithmetic(raw_lines: list[_RawLine], total_taxable: int | None,
                    total_cgst: int | None, total_sgst: int | None, total_igst: int | None,
                    round_off: int | None, grand_total: int | None, pos_code: str | None,
                    supplier_code: str | None,
                    buyer_code: str | None) -> tuple[ArithmeticChecks, _Corrob]:
    deltas: list[int] = []

    line_taxable_sum = sum(r.taxable for r in raw_lines if r.taxable is not None)
    taxable_ran = bool(raw_lines) and total_taxable is not None
    if taxable_ran:
        d = abs(line_taxable_sum - (total_taxable or 0))
        deltas.append(d)
        lines_sum_ok = d <= TOTALS_TOL_PAISE
    else:
        lines_sum_ok = not raw_lines or total_taxable is None  # nothing to contradict

    per_line_ran = False
    per_line_ok = True
    for r in raw_lines:
        if r.taxable is None or r.gst_rate is None:
            continue
        per_line_ran = True
        expected = int((Decimal(r.taxable) * r.gst_rate / Decimal(100)).to_integral_value())
        got = (r.cgst or 0) + (r.sgst or 0) + (r.igst or 0)
        d = abs(expected - got)
        deltas.append(d)
        if d > LINE_TAX_TOL_PAISE:
            per_line_ok = False

    grand_ran = grand_total is not None and total_taxable is not None
    if grand_ran:
        computed = ((total_taxable or 0) + (total_cgst or 0) + (total_sgst or 0)
                    + (total_igst or 0) + (round_off or 0))
        d = abs(computed - (grand_total or 0))
        deltas.append(d)
        totals_ok = d <= TOTALS_TOL_PAISE
    else:
        totals_ok = grand_total is None or total_taxable is None  # nothing to contradict

    supply_ok = _supply_type_consistent(
        pos_code, supplier_code, buyer_code, total_cgst or 0, total_sgst or 0, total_igst or 0)
    supply_ran = (
        _supply_type(pos_code, supplier_code, buyer_code) is not None
        and any(t is not None for t in (total_cgst, total_sgst, total_igst)))

    checks = ArithmeticChecks(
        lines_sum_matches_taxable=lines_sum_ok, per_line_tax_consistent=per_line_ok,
        totals_add_to_grand=totals_ok, supply_type_consistent=supply_ok,
        max_abs_delta_paise=max(deltas) if deltas else 0,
    )
    corrob = _Corrob(taxable_ran=taxable_ran, per_line_ran=per_line_ran,
                     grand_ran=grand_ran, supply_ran=supply_ran)
    return checks, corrob


# --------------------------------------------------------------------------- review gate

def _review_reasons(header: InvoiceHeader, totals: InvoiceTotals, lines: list[InvoiceLine],
                    supplier_c: _Gstin | None, buyer_c: _Gstin | None,
                    arith: ArithmeticChecks) -> list[str]:
    """Collect ALL review triggers (never stop at the first)."""
    reasons: list[str] = []
    field_by_path: dict[str, Field] = {  # type: ignore[type-arg]
        "header.supplier_gstin": header.supplier_gstin,
        "header.invoice_number": header.invoice_number,
        "header.invoice_date": header.invoice_date,
        "totals.total_taxable_paise": totals.total_taxable_paise,
        "totals.grand_total_paise": totals.grand_total_paise,
    }
    for path in REQUIRED_FIELD_PATHS:
        f = field_by_path[path]
        if f.status is FieldStatus.MISSING:
            reasons.append(f"required field {path} is missing")
        elif f.status is FieldStatus.LOW_CONFIDENCE:
            reasons.append(f"required field {path} is low-confidence")

    if not lines:
        reasons.append("no line items found in the invoice")

    if supplier_c is not None and not valid_gstin(supplier_c.value):
        reasons.append("supplier_gstin failed the GSTIN checksum")
    if buyer_c is not None and not valid_gstin(buyer_c.value):
        reasons.append("buyer_gstin failed the GSTIN checksum")

    if not arith.lines_sum_matches_taxable:
        reasons.append("line taxable amounts do not sum to the total taxable value")
    if not arith.per_line_tax_consistent:
        reasons.append("a line's tax does not match its taxable value × GST rate")
    if not arith.totals_add_to_grand:
        reasons.append("taxable + taxes + round-off does not equal the grand total")
    if not arith.supply_type_consistent:
        reasons.append("intra/inter-state tax split is inconsistent with place of supply")

    seen: set[str] = set()
    deduped: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped


# --------------------------------------------------------------------------- hashing

def _normalize_raw_text(raw_text: str) -> str:
    return collapse_ws(raw_text).casefold()


def _content_hash(raw_text: str) -> str:
    return hashlib.sha256(_normalize_raw_text(raw_text).encode("utf-8")).hexdigest()


def _dedup_key(supplier_gstin: str | None, invoice_number: str | None,
               invoice_date: date | None, grand_total_paise: int | None) -> str:
    parts = [
        collapse_ws(supplier_gstin or "").upper(),
        match_key(invoice_number) if invoice_number else "",
        invoice_date.isoformat() if invoice_date else "",
        str(grand_total_paise) if grand_total_paise is not None else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
