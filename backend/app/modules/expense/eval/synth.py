"""Synthetic GST-invoice PDF generator for the eval gold set.

``build_invoice_pdf(spec)`` renders a real, text-layer PDF with **reportlab** and returns
``(pdf_bytes, gold)`` where ``gold`` is the canonical expected value tree — known BY
CONSTRUCTION, never re-parsed. The extractor / integration harness can reuse this exact
signature to fabricate gold at will; the eval scorers consume the returned dict directly.

Supported shape knobs (all deterministic — ``rl_config.invariant`` pins dates/ids):

* intra-state (CGST + SGST) vs inter-state (IGST), chosen by ``spec.intra_state``;
* N line items, mixed HSNs and GST rates;
* a signed round-off line;
* a custom column order (e.g. ``Qty | HSN | Description | Rate | Taxable Value | ...``);
* a discount / sub-total presentation row (cosmetic — does not alter canonical totals);
* a forced page break so line items span two pages;
* a per-line tax wobble (``LineSpec.tax_delta_paise``) to exercise the ±₹1 tolerance;
* ``scanned=True`` → an image-only / blank page with NO text layer (expects needs_ocr).

Money is integer paise end to end (house convention); quantities / rates are ``Decimal``.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from reportlab import rl_config
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app.modules.expense.canonical import CANONICAL_SCHEMA_VERSION
from app.modules.masterdata.normalize import valid_gstin

# Deterministic output: fixed creation date + no random file ids, so committed fixtures are
# byte-stable across regenerations.
rl_config.invariant = 1

_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def make_gstin(first14: str) -> str:
    """Complete a 14-char GSTIN prefix (state[2] + PAN[10] + entity[1] + 'Z') with the
    single valid GSTN check character. Raises if no completion validates."""
    for ch in _GSTIN_ALPHABET:
        candidate = first14 + ch
        if valid_gstin(candidate):
            return candidate
    raise ValueError(f"no valid GSTIN check char for prefix {first14!r}")


# A few real, checksum-valid GSTINs (verified via masterdata.normalize.valid_gstin) so
# fixtures never trip the extractor's checksum gate spuriously.
GSTIN_SUPPLIER_MH = "27AAPFU0939F1ZV"  # Maharashtra
GSTIN_BUYER_MH = make_gstin("27ABCDE1234F1Z")  # Maharashtra (intra with supplier)
GSTIN_SUPPLIER_GJ = "24AAACG1234H1Z6"  # Gujarat
GSTIN_BUYER_KA = "29AAECS4321L1Z5"     # Karnataka (inter-state vs a Gujarat supplier)


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------


@dataclass
class LineSpec:
    description: str
    hsn_sac: str
    quantity: Decimal
    unit: str
    unit_rate_paise: int
    gst_rate: Decimal            # percent, e.g. Decimal("18")
    tax_delta_paise: int = 0     # inject vendor rounding onto the line tax (± up to ₹1)


@dataclass
class InvoiceSpec:
    supplier_name: str
    supplier_gstin: str
    supplier_address: str
    buyer_name: str
    buyer_gstin: str
    buyer_address: str
    invoice_number: str
    invoice_date: date
    place_of_supply: str          # "State (NN)"
    lines: list[LineSpec]
    intra_state: bool
    po_ref: str | None = None
    round_off_paise: int = 0
    amount_in_words: str | None = None
    column_order: tuple[str, ...] = (
        "description", "hsn", "qty", "unit", "rate", "taxable", "gst",
        "cgst", "sgst", "igst", "total")
    discount_subtotal: bool = False
    two_page: bool = False
    page_break_after: int = 0     # rows on page 1 before a forced break (0 → auto/none)
    scanned: bool = False
    # --- adversarial shape knobs (regression fixtures) --------------------------------------
    tight_header: bool = False    # remove the supplier↔buyer gap so the SUPPLIER GSTIN sits
    #                               just ABOVE "Bill To" (H1: silent supplier⇄buyer swap bait)
    borderless: bool = False      # draw the item table with NO rules + RIGHT-aligned money, so
    #                               extract_tables() fails and the word-geometry fallback runs
    #                               against real right-aligned money columns (H2)
    accounting_negatives: bool = False   # render negative money as "(0.30)" accounting parens
    #                                       instead of "-0.30" (M3: signed paren parsing)
    header_labels: dict[str, str] = field(default_factory=dict)  # per-key header label override
    #                               (M6: relabel the taxable column "Amount" beside a "Total")
    _title: str = field(default="Tax Invoice", repr=False)


# ---------------------------------------------------------------------------
# Canonical computation (by construction)
# ---------------------------------------------------------------------------


def _q0(d: Decimal) -> int:
    """Round a Decimal to whole paise (HALF_UP) → int."""
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class _ComputedLine:
    line_no: int
    description: str
    hsn_sac: str
    quantity: Decimal
    unit: str
    unit_rate_paise: int
    taxable_paise: int
    gst_rate: Decimal
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_total_paise: int


def _compute_line(idx: int, ls: LineSpec, intra: bool) -> _ComputedLine:
    taxable = _q0(ls.quantity * Decimal(ls.unit_rate_paise))
    tax_total = _q0(Decimal(taxable) * ls.gst_rate / Decimal(100))
    if intra:
        cgst = tax_total // 2
        sgst = tax_total - cgst + ls.tax_delta_paise
        igst = 0
    else:
        cgst = 0
        sgst = 0
        igst = tax_total + ls.tax_delta_paise
    line_total = taxable + cgst + sgst + igst
    return _ComputedLine(
        line_no=idx,
        description=ls.description,
        hsn_sac=ls.hsn_sac,
        quantity=ls.quantity,
        unit=ls.unit,
        unit_rate_paise=ls.unit_rate_paise,
        taxable_paise=taxable,
        gst_rate=ls.gst_rate,
        cgst_paise=cgst,
        sgst_paise=sgst,
        igst_paise=igst,
        line_total_paise=line_total,
    )


def _compute(spec: InvoiceSpec) -> tuple[list[_ComputedLine], dict[str, int]]:
    lines = [_compute_line(i + 1, ls, spec.intra_state) for i, ls in enumerate(spec.lines)]
    total_taxable = sum(cl.taxable_paise for cl in lines)
    total_cgst = sum(cl.cgst_paise for cl in lines)
    total_sgst = sum(cl.sgst_paise for cl in lines)
    total_igst = sum(cl.igst_paise for cl in lines)
    grand_total = (
        total_taxable + total_cgst + total_sgst + total_igst + spec.round_off_paise)
    totals = {
        "total_taxable_paise": total_taxable,
        "total_cgst_paise": total_cgst,
        "total_sgst_paise": total_sgst,
        "total_igst_paise": total_igst,
        "round_off_paise": spec.round_off_paise,
        "grand_total_paise": grand_total,
    }
    return lines, totals


def _build_gold(
    spec: InvoiceSpec, lines: list[_ComputedLine], totals: dict[str, int], page_count: int
) -> dict[str, object]:
    if spec.scanned:
        # Image-only page: nothing is extractable, so every field is MISSING and the doc
        # must route to OCR. Lines are empty by construction.
        return {
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "doc_type": "gst_invoice",
            "page_count": 1,
            "needs_ocr": True,
            "review_needed": True,
            "header": {a: None for a in _HEADER_ATTRS},
            "totals": {a: None for a in _TOTALS_ATTRS},
            "lines": [],
        }
    header = {
        "supplier_name": spec.supplier_name,
        "supplier_gstin": spec.supplier_gstin,
        "supplier_address": spec.supplier_address,
        "buyer_name": spec.buyer_name,
        "buyer_gstin": spec.buyer_gstin,
        "buyer_address": spec.buyer_address,
        "invoice_number": spec.invoice_number,
        "invoice_date": spec.invoice_date.isoformat(),
        "place_of_supply": spec.place_of_supply,
        "po_ref": spec.po_ref,
    }
    gold_lines: list[dict[str, object]] = [
        {
            "line_no": cl.line_no,
            "description": cl.description,
            "hsn_sac": cl.hsn_sac,
            "quantity": _dec_str(cl.quantity),
            "unit": cl.unit,
            "unit_rate_paise": cl.unit_rate_paise,
            "taxable_paise": cl.taxable_paise,
            "gst_rate": _dec_str(cl.gst_rate),
            "cgst_paise": cl.cgst_paise,
            "sgst_paise": cl.sgst_paise,
            "igst_paise": cl.igst_paise,
            "line_total_paise": cl.line_total_paise,
        }
        for cl in lines
    ]
    gold_totals: dict[str, object] = dict(totals)
    gold_totals["amount_in_words"] = spec.amount_in_words
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "doc_type": "gst_invoice",
        "page_count": page_count,
        "needs_ocr": False,
        "review_needed": False,
        "header": header,
        "totals": gold_totals,
        "lines": gold_lines,
    }


_HEADER_ATTRS = (
    "supplier_name", "supplier_gstin", "supplier_address", "buyer_name", "buyer_gstin",
    "buyer_address", "invoice_number", "invoice_date", "place_of_supply", "po_ref",
)
_TOTALS_ATTRS = (
    "total_taxable_paise", "total_cgst_paise", "total_sgst_paise", "total_igst_paise",
    "round_off_paise", "grand_total_paise", "amount_in_words",
)


def _dec_str(d: Decimal) -> str:
    """Plain-string a Decimal without exponent notation (e.g. '3', '18.00')."""
    return format(d.normalize() if d == d.to_integral() else d, "f")


# ---------------------------------------------------------------------------
# Rendering (reportlab canvas)
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = A4
_MARGIN = 18 * mm


def _rupees(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}{p // 100:,}.{p % 100:02d}"


def _money_str(paise: int, accounting: bool) -> str:
    """Render money for a cell/total line. With ``accounting`` a negative prints in
    parentheses ``(0.30)`` (the common ledger convention) instead of ``-0.30``."""
    if accounting and paise < 0:
        p = abs(paise)
        return f"({p // 100:,}.{p % 100:02d})"
    return _rupees(paise)


# A realistic GST line-item table names taxable value and each tax head in its OWN column
# (CGST / SGST for intra-state, IGST for inter-state — the not-applicable heads print 0.00).
# The header labels here are what the extractor resolves columns BY (never by index), so they
# must read like a real invoice: "Taxable Value", "CGST", "SGST", "IGST", "Total".
_COLUMNS: dict[str, str] = {
    "description": "Description",
    "hsn": "HSN/SAC",
    "qty": "Qty",
    "unit": "Unit",
    "rate": "Rate",
    "taxable": "Taxable",
    "gst": "GST%",
    "cgst": "CGST",
    "sgst": "SGST",
    "igst": "IGST",
    "total": "Total",
}


def _cell(cl: _ComputedLine, key: str) -> str:
    if key == "description":
        return cl.description
    if key == "hsn":
        return cl.hsn_sac
    if key == "qty":
        return _dec_str(cl.quantity)
    if key == "unit":
        return cl.unit
    if key == "rate":
        return _rupees(cl.unit_rate_paise)
    if key == "taxable":
        return _rupees(cl.taxable_paise)
    if key == "gst":
        return f"{_dec_str(cl.gst_rate)}%"
    if key == "cgst":
        return _rupees(cl.cgst_paise)
    if key == "sgst":
        return _rupees(cl.sgst_paise)
    if key == "igst":
        return _rupees(cl.igst_paise)
    if key == "total":
        return _rupees(cl.line_total_paise)
    return ""


def _draw_header_block(c: canvas.Canvas, spec: InvoiceSpec, y: float, continued: bool) -> float:
    """Render a realistic single-column masthead: supplier block, a "Bill To:" buyer block,
    then the invoice meta lines.

    Each identity/meta line sits on its OWN visual row (never a two-column overlay), so the
    extractor reads the supplier name as the top non-noise line, anchors the buyer on
    "Bill To:", and keeps the supplier GSTIN as the top-most GSTIN — the exact structure its
    header logic is designed for. Field VALUES are the spec's, verbatim.
    """
    c.setFont("Helvetica-Bold", 14)
    title = spec._title + (" (continued)" if continued else "")
    c.drawString(_MARGIN, y, title)
    y -= 8 * mm

    def _line(text: str, font: str = "Helvetica", size: float = 9, gap: float = 5) -> None:
        nonlocal y
        c.setFont(font, size)
        c.drawString(_MARGIN, y, text)
        y -= gap * mm

    _line(spec.supplier_name, font="Helvetica-Bold", size=11)
    _line(spec.supplier_address)
    _line(f"GSTIN: {spec.supplier_gstin}")
    # A clear separation before the buyer block so the buyer GSTIN — not the supplier's —
    # is the one nearest the "Bill To" anchor the extractor keys the buyer off. With
    # ``tight_header`` this gap is REMOVED, so the supplier GSTIN sits just above "Bill To"
    # (the Euclidean-nearest token) — the exact bait for the silent supplier⇄buyer swap (H1).
    if not spec.tight_header:
        y -= 10 * mm
    _line(f"Bill To: {spec.buyer_name}")
    _line(spec.buyer_address)
    _line(f"GSTIN: {spec.buyer_gstin}")
    y -= 2 * mm
    _line(f"Invoice No: {spec.invoice_number}    "
          f"Invoice Date: {spec.invoice_date.strftime('%d-%m-%Y')}")
    _line(f"Place of Supply: {spec.place_of_supply}")
    if spec.po_ref:
        _line(f"PO Ref: {spec.po_ref}")
    return float(y - 2 * mm)


def _column_x(cols: tuple[str, ...], borderless: bool = False) -> list[float]:
    usable = _PAGE_W - 2 * _MARGIN
    # Description gets the widest column; the rest split evenly but stay wide enough that a
    # 9-char money value ("16,500.00") and the header labels never overflow into (and
    # interleave with) the neighbouring cell — overflow is what corrupts edge-based table
    # detection. In the BORDERLESS layout the money columns (whose header + value are both
    # right-aligned) are widened so a wide label like "Taxable Value" clears its left
    # neighbour instead of colliding into one run.
    def _weight(k: str) -> float:
        if k == "description":
            return 2.2 if borderless else 2.0
        if borderless and k in _MONEY_KEYS:
            return 1.6
        return 0.9 if borderless else 1.0

    weights = [_weight(k) for k in cols]
    total = sum(weights)
    xs: list[float] = []
    x = _MARGIN
    for w in weights:
        xs.append(x)
        x += usable * (w / total)
    return xs


# Row band height + text insets for the ruled grid. A real GST invoice draws the line-item
# table as a visible box with column/row rules; drawing them (rather than borderless
# positioned text) is what makes the grid detectable by pdfplumber's edge-based
# `extract_tables()`. Cell VALUES are unchanged — only the surrounding rules are new.
_ROW_H = 6 * mm
_CELL_TX = 1.5          # x inset so text clears the left column rule
_CELL_TY = 1.8 * mm     # baseline inset above the row's bottom rule


# Money columns carry right-aligned figures on a real invoice (H2 borderless fixture).
_MONEY_KEYS = frozenset({"rate", "taxable", "cgst", "sgst", "igst", "total"})

# Borderless right-aligned money is inset further from its column's right edge than the text
# inset, so a wide value never butts up against the next (left-aligned) cell within
# pdfplumber's word tolerance and get glued into one token.
_MONEY_INSET = 7.0


def _header_label(spec: InvoiceSpec, key: str) -> str:
    """The rendered header label for a column key, honouring per-spec overrides (M6)."""
    return spec.header_labels.get(key, _COLUMNS[key])


def _draw_grid_table(c: canvas.Canvas, spec: InvoiceSpec, cols: tuple[str, ...],
                     xs: list[float], page_lines: list[_ComputedLine], y_top: float) -> float:
    """Render the header + this page's item rows as a RULED (bordered) table.

    Draws the full grid — a horizontal rule between every row and a vertical rule at every
    column boundary — so the item table looks like a real ruled GST invoice AND registers as
    a table for `pdfplumber.extract_tables()`. Returns the y just below the table.
    """
    right = _PAGE_W - _MARGIN
    n_rows = 1 + len(page_lines)                 # header row + one band per item
    bottom = y_top - n_rows * _ROW_H
    c.setLineWidth(0.5)
    for i in range(n_rows + 1):                  # horizontal rules (top, between, bottom)
        yy = y_top - i * _ROW_H
        c.line(_MARGIN, yy, right, yy)
    for x in [*xs, right]:                        # vertical rules (column boundaries + box)
        c.line(x, y_top, x, bottom)

    c.setFont("Helvetica-Bold", 7)
    header_base = y_top - _ROW_H + _CELL_TY
    for key, x in zip(cols, xs, strict=True):
        c.drawString(x + _CELL_TX, header_base, _header_label(spec, key))

    c.setFont("Helvetica", 7)
    for ri, cl in enumerate(page_lines, start=1):
        base = y_top - (ri + 1) * _ROW_H + _CELL_TY
        for key, x in zip(cols, xs, strict=True):
            c.drawString(x + _CELL_TX, base, _cell(cl, key))
    return float(bottom)


def _draw_borderless_table(c: canvas.Canvas, spec: InvoiceSpec, cols: tuple[str, ...],
                           xs: list[float], page_lines: list[_ComputedLine],
                           y_top: float) -> float:
    """Render the header + item rows as a BORDERLESS grid with RIGHT-aligned money (H2).

    No rules are drawn, so `pdfplumber.extract_tables()` finds no grid and the extractor falls
    back to word geometry. Text columns are left-aligned; money columns (and their headers)
    are RIGHT-aligned to the column's right edge — the real-invoice layout that made the old
    left-edge banding misfile a value into the next column. Returns the y just below the rows.
    """
    right = _PAGE_W - _MARGIN
    edges = [*xs[1:], right]                       # right edge of each column
    header_base = y_top - _ROW_H + _CELL_TY
    c.setFont("Helvetica-Bold", 7)
    for key, x_left, x_right in zip(cols, xs, edges, strict=True):
        label = _header_label(spec, key)
        if key in _MONEY_KEYS:
            c.drawRightString(x_right - _MONEY_INSET, header_base, label)
        else:
            c.drawString(x_left + _CELL_TX, header_base, label)

    c.setFont("Helvetica", 7)
    for ri, cl in enumerate(page_lines, start=1):
        base = y_top - (ri + 1) * _ROW_H + _CELL_TY
        for key, x_left, x_right in zip(cols, xs, edges, strict=True):
            val = _cell(cl, key)
            if key in _MONEY_KEYS:
                c.drawRightString(x_right - _MONEY_INSET, base, val)
            else:
                c.drawString(x_left + _CELL_TX, base, val)
    return float(y_top - (1 + len(page_lines)) * _ROW_H)


def _draw_totals_block(
    c: canvas.Canvas, spec: InvoiceSpec, totals: dict[str, int], y: float
) -> None:
    c.setFont("Helvetica", 9)
    x_label = _PAGE_W - _MARGIN - 70 * mm
    x_val = _PAGE_W - _MARGIN - 5 * mm
    # Print every tax head AND round-off explicitly (0.00 where not applicable) so the
    # extractor reads a value for each — a not-applicable head is a real 0, not MISSING, and
    # the gold expects 0 there.
    rows: list[tuple[str, int]] = [
        ("Taxable Value", totals["total_taxable_paise"]),
        ("CGST", totals["total_cgst_paise"]),
        ("SGST", totals["total_sgst_paise"]),
        ("IGST", totals["total_igst_paise"]),
        ("Round Off", totals["round_off_paise"]),
    ]
    for label, val in rows:
        c.drawString(x_label, y, label)
        c.drawRightString(x_val, y, _money_str(val, spec.accounting_negatives))
        y -= 5 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x_label, y, "Grand Total")
    c.drawRightString(x_val, y, _money_str(totals["grand_total_paise"], spec.accounting_negatives))
    y -= 6 * mm
    if spec.amount_in_words:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(_MARGIN, y, f"Amount in words: {spec.amount_in_words}")


def _draw_discount_subtotal(c: canvas.Canvas, subtotal_paise: int, y: float) -> float:
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(_MARGIN, y, "Sub-Total")
    c.drawRightString(_PAGE_W - _MARGIN, y, _rupees(subtotal_paise))
    y -= 5 * mm
    c.drawString(_MARGIN, y, "Discount")
    c.drawRightString(_PAGE_W - _MARGIN, y, _rupees(0))
    return float(y - 5 * mm)


def build_invoice_pdf(spec: InvoiceSpec) -> tuple[bytes, dict[str, object]]:
    """Render ``spec`` to a PDF and return ``(pdf_bytes, gold)``.

    ``gold`` matches the shape :func:`app.modules.expense.eval.scorers.score` expects.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(spec._title)

    if spec.scanned:
        # No text: a single filled rectangle stands in for a scanned image. pdfplumber /
        # any text-layer extractor finds nothing → needs_ocr.
        c.setFillGray(0.85)
        c.rect(_MARGIN, _MARGIN, _PAGE_W - 2 * _MARGIN, _PAGE_H - 2 * _MARGIN, fill=1, stroke=0)
        c.showPage()
        c.save()
        gold = _build_gold(spec, [], {}, page_count=1)
        return buf.getvalue(), gold

    lines, totals = _compute(spec)
    cols = spec.column_order
    xs = _column_x(cols, borderless=spec.borderless)

    # Decide the split point for a two-page invoice.
    split = len(lines)
    if spec.two_page:
        split = spec.page_break_after or max(1, len(lines) // 2)
    pages: list[list[_ComputedLine]] = [lines[:split]]
    if split < len(lines):
        pages.append(lines[split:])

    page_count = len(pages)
    for pi, page_lines in enumerate(pages):
        y = _PAGE_H - _MARGIN
        y = _draw_header_block(c, spec, y, continued=(pi > 0))
        if spec.borderless:
            y = _draw_borderless_table(c, spec, cols, xs, page_lines, y)
        else:
            y = _draw_grid_table(c, spec, cols, xs, page_lines, y)
        is_last = pi == page_count - 1
        if is_last:
            if spec.discount_subtotal:
                y = _draw_discount_subtotal(c, totals["total_taxable_paise"], y - 2 * mm)
            _draw_totals_block(c, spec, totals, y - 4 * mm)
        c.showPage()
    c.save()

    gold = _build_gold(spec, lines, totals, page_count=page_count)
    return buf.getvalue(), gold


# ---------------------------------------------------------------------------
# Tally-style "Tax Invoice" with a bare "#" number label, tax-RATE columns and a bare
# "TOTAL:" line (no labelled totals) — the real-world shape that falls THROUGH the Tally
# router into the generic text-layer engine. Self-contained (no coupling to the knob-driven
# builder above) so it can never perturb the other committed fixtures.
# ---------------------------------------------------------------------------


@dataclass
class HashRateLine:
    description: str
    hsn_sac: str
    quantity: Decimal
    unit_rate_paise: int
    gst_rate: Decimal            # COMBINED percent, carried in the IGST rate column (e.g. 18)


@dataclass
class HashRateInvoiceSpec:
    """A single-column masthead + a ruled item grid whose SGST/CGST/IGST columns print RATES
    (``0``/``0``/``18%``) not amounts, a DOUBLED trailing ``Amount`` = the inclusive line total,
    a bare ``#`` invoice-number label, and only a bare ``TOTAL:`` line (no labelled totals)."""

    supplier_name: str
    supplier_gstin: str
    supplier_address: str
    buyer_name: str
    buyer_gstin: str
    buyer_address: str
    invoice_number: str
    invoice_date: date
    place_of_supply: str
    po_ref: str
    lines: list[HashRateLine]


# Column layout as VERTICAL-RULE boundaries in PDF points (11 boundaries → 10 columns). Text
# anchors are DERIVED from these boundaries so every value sits inside its own cell (a value
# drawn outside its ruled cell is what splits/truncates a column when pdfplumber re-reads the
# grid). Money columns are right-aligned to their right rule; text columns left-aligned to
# their left rule.
_HR_BOUNDS: list[float] = [51, 68, 190, 238, 278, 306, 374, 400, 426, 456, 544]
_HR_HEADERS: list[tuple[str, str, bool]] = [   # (key, label, right_aligned)
    ("sl", "#", False),
    ("desc", "Item & Description", False),
    ("hsn", "HSN/SAC", False),
    ("rate", "Rate", True),
    ("qty", "Qty", True),
    ("taxable", "Taxable Amount", True),
    ("sgst", "SGST", True),
    ("cgst", "CGST", True),
    ("igst", "IGST", True),
    ("amount", "Amount", True),
]
_HR_KEY_IDX: dict[str, int] = {h[0]: i for i, h in enumerate(_HR_HEADERS)}
_HR_ROW_H = 6 * mm


def _hr_anchor(key: str, ralign: bool) -> float:
    """Left rule + inset (left-aligned) or right rule − inset (right-aligned) for a column."""
    j = _HR_KEY_IDX[key]
    return (_HR_BOUNDS[j + 1] - 3.0) if ralign else (_HR_BOUNDS[j] + 2.0)


@dataclass
class _HRComputed:
    line_no: int
    description: str
    hsn_sac: str
    quantity: Decimal
    unit_rate_paise: int
    taxable_paise: int
    gst_rate: Decimal
    line_total_paise: int


def _hr_compute(spec: HashRateInvoiceSpec) -> tuple[list[_HRComputed], int, int]:
    """Compute each row's paise (taxable, inter-state IGST, inclusive line total) by
    construction, plus the two derived totals (Σ taxable, Σ line total)."""
    rows: list[_HRComputed] = []
    total_taxable = 0
    grand = 0
    for i, ls in enumerate(spec.lines, start=1):
        taxable = _q0(ls.quantity * Decimal(ls.unit_rate_paise))
        igst = _q0(Decimal(taxable) * ls.gst_rate / Decimal(100))
        line_total = taxable + igst
        total_taxable += taxable
        grand += line_total
        rows.append(_HRComputed(
            line_no=i, description=ls.description, hsn_sac=ls.hsn_sac, quantity=ls.quantity,
            unit_rate_paise=ls.unit_rate_paise, taxable_paise=taxable, gst_rate=ls.gst_rate,
            line_total_paise=line_total))
    return rows, total_taxable, grand


def build_hash_rate_invoice_pdf(spec: HashRateInvoiceSpec) -> tuple[bytes, dict[str, object]]:
    """Render ``spec`` and return ``(pdf_bytes, gold)`` in the scorer gold shape. The two
    totals are DERIVED by the extractor from the line items (no labelled totals are printed)."""
    rows, total_taxable, grand = _hr_compute(spec)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle("Tax Invoice")

    # ---- single-column masthead (predictable positional name/address extraction) ----
    y = _PAGE_H - _MARGIN
    c.setFont("Helvetica-Bold", 11)
    c.drawString(_MARGIN, y, spec.supplier_name)
    y -= 5 * mm
    c.setFont("Helvetica", 9)
    c.drawString(_MARGIN, y, spec.supplier_address)
    y -= 5 * mm
    c.drawString(_MARGIN, y, f"GST: {spec.supplier_gstin}")
    y -= 7 * mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(_MARGIN, y, "TAX INVOICE")
    y -= 7 * mm
    c.setFont("Helvetica", 9)
    c.drawString(_MARGIN, y, f"Bill To: {spec.buyer_name}")
    y -= 5 * mm
    c.drawString(_MARGIN, y, spec.buyer_address)
    y -= 5 * mm
    c.drawString(_MARGIN, y, f"GST: {spec.buyer_gstin}")
    y -= 7 * mm
    # A BARE "#" invoice-number label (no "Invoice No"), the exact Tally shape that forces the
    # generic extractor's bare-"#" fallback.
    c.drawString(_MARGIN, y, f"# : {spec.invoice_number}")
    y -= 5 * mm
    c.drawString(_MARGIN, y, f"Date : {spec.invoice_date.strftime('%d-%m-%Y')}")
    y -= 5 * mm
    c.drawString(_MARGIN, y, f"Place of Supply: {spec.place_of_supply}")
    y -= 5 * mm
    c.drawString(_MARGIN, y, f"PO No. : {spec.po_ref}")
    y -= 8 * mm

    # ---- ruled item grid (SGST/CGST/IGST columns carry RATES; trailing Amount = line total) --
    n_rows = 1 + len(rows)
    y_top = y
    bottom = y_top - n_rows * _HR_ROW_H
    c.setLineWidth(0.5)
    for i in range(n_rows + 1):
        yy = y_top - i * _HR_ROW_H
        c.line(_HR_BOUNDS[0], yy, _HR_BOUNDS[-1], yy)
    for x in _HR_BOUNDS:
        c.line(x, y_top, x, bottom)

    def _put(key: str, label: str, ralign: bool, base_y: float, font: str, size: float) -> None:
        c.setFont(font, size)
        if ralign:
            c.drawRightString(_hr_anchor(key, True), base_y, label)
        else:
            c.drawString(_hr_anchor(key, False), base_y, label)

    hbase = y_top - _HR_ROW_H + 1.8 * mm
    for key, label, ralign in _HR_HEADERS:
        _put(key, label, ralign, hbase, "Helvetica-Bold", 7)

    for ri, row in enumerate(rows, start=1):
        base = y_top - (ri + 1) * _HR_ROW_H + 1.8 * mm
        _put("sl", str(row.line_no), False, base, "Helvetica", 7)
        _put("desc", row.description, False, base, "Helvetica", 7)
        _put("hsn", row.hsn_sac, False, base, "Helvetica", 7)
        _put("rate", _rupees(row.unit_rate_paise), True, base, "Helvetica", 7)
        _put("qty", _dec_str(row.quantity), True, base, "Helvetica", 7)
        _put("taxable", _rupees(row.taxable_paise), True, base, "Helvetica", 7)
        _put("sgst", "0", True, base, "Helvetica", 7)                       # SGST RATE 0%
        _put("cgst", "0", True, base, "Helvetica", 7)                       # CGST RATE 0%
        _put("igst", f"{_dec_str(row.gst_rate)}%", True, base, "Helvetica", 7)
        _put("amount", _rupees(row.line_total_paise), True, base, "Helvetica", 7)

    # ---- a bare "TOTAL:" line (no labelled taxable / grand total → both must be DERIVED) ----
    y = bottom - 6 * mm
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(_hr_anchor("igst", True), y, "TOTAL:")
    c.drawRightString(_hr_anchor("amount", True), y, _rupees(grand))
    c.showPage()
    c.save()

    gold_lines: list[dict[str, object]] = [
        {
            "line_no": row.line_no,
            "description": row.description,
            "hsn_sac": row.hsn_sac,
            "quantity": _dec_str(row.quantity),
            "unit": None,                 # this layout has no Unit column
            "unit_rate_paise": row.unit_rate_paise,
            "taxable_paise": row.taxable_paise,
            "gst_rate": _dec_str(row.gst_rate),
            "cgst_paise": None,           # rate-only layout: no printed tax AMOUNTS
            "sgst_paise": None,
            "igst_paise": None,
            "line_total_paise": row.line_total_paise,
        }
        for row in rows
    ]
    gold: dict[str, object] = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "doc_type": "gst_invoice",
        "page_count": 1,
        "needs_ocr": False,
        "review_needed": False,
        "header": {
            "supplier_name": spec.supplier_name,
            "supplier_gstin": spec.supplier_gstin,
            "supplier_address": spec.supplier_address,
            "buyer_name": spec.buyer_name,
            "buyer_gstin": spec.buyer_gstin,
            "buyer_address": spec.buyer_address,
            "invoice_number": spec.invoice_number,
            "invoice_date": spec.invoice_date.isoformat(),
            "place_of_supply": spec.place_of_supply,
            "po_ref": spec.po_ref,
        },
        "totals": {
            "total_taxable_paise": total_taxable,
            "total_cgst_paise": None,
            "total_sgst_paise": None,
            "total_igst_paise": None,
            "round_off_paise": None,
            "grand_total_paise": grand,
            "amount_in_words": None,
        },
        "lines": gold_lines,
    }
    return buf.getvalue(), gold
