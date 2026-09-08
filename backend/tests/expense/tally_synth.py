"""Synthetic Tally 'Tax Invoice' PDF generator (TEST-ONLY — reportlab never runs at runtime).

Mirrors the ``app.modules.expense.eval.synth`` style: ``build_tally_pdf(spec)`` renders a real
text-layer PDF whose geometry matches a genuine Tally tax invoice (the two-row column banner,
right-aligned money, per-line CGST/SGST or IGST sub-columns, a grand-total row, an
amount-in-words line, the "Computer Generated Invoice" footer) and returns
``(pdf_bytes, gold)`` where ``gold`` is the canonical expected tree, known BY CONSTRUCTION.

Money is integer paise end to end (house convention); quantities / rates are ``Decimal``. The
column x-anchors reproduce the real Tally layout on a wide page so nearest-centre bucketing +
the two-row-banner grid builder resolve every column exactly as they do on a real invoice.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from reportlab import rl_config
from reportlab.pdfgen import canvas

from app.modules.expense.canonical import CANONICAL_SCHEMA_VERSION

rl_config.invariant = 1  # deterministic, byte-stable output

# Wide page: a real Tally invoice runs its columns out past A4 width. A generous layout with
# right-aligned money headers + values (below) keeps nearest-centre bucketing robust no matter
# how wide a value renders — exactly like a real, roomy Tally column grid.
_PAGE_W, _PAGE_H = 1015.0, 595.0

# Text columns: left x-anchor for both the header label and the value (left-aligned).
_TEXT_L: dict[str, float] = {"sl": 35.0, "desc": 60.0, "hsn": 300.0, "qty": 360.0, "per": 478.0}

# Money columns: right-edge x for both the header label and the value (both right-aligned, so a
# header token's centre tracks its value's centre regardless of value width).
_MONEY_R: dict[str, float] = {
    "rate": 470.0, "amount": 560.0, "taxable": 650.0,
    "cgst_rate": 700.0, "cgst_amt": 780.0, "sgst_rate": 835.0, "sgst_amt": 915.0,
    "igst_rate": 700.0, "igst_amt": 790.0,
}
_TOTAL_R_INTRA, _TOTAL_R_INTER = 990.0, 875.0


@dataclass
class TLine:
    description: str
    hsn: str
    qty: Decimal
    unit: str
    unit_rate_paise: int
    gst_rate: Decimal            # COMBINED percent (e.g. 18 → 9%+9%; 5 → 2.5%+2.5%)
    tax_delta_paise: int = 0     # nudge the printed tax to exercise the ±₹1 tolerance
    wrap: str | None = None      # extra description text rendered on a wrapped continuation row


@dataclass
class TInvoice:
    supplier_name: str
    supplier_gstin: str
    supplier_address: str
    buyer_name: str
    buyer_gstin: str
    buyer_address: str
    invoice_number: str
    invoice_date: date
    place_of_supply: str
    lines: list[TLine]
    intra: bool
    round_off_paise: int = 0
    grand_total_override_paise: int | None = None   # force a printed-vs-recomputed mismatch
    total_taxable_override_paise: int | None = None  # force printed Total-row taxable ≠ item sum
    whole_rupees: bool = False   # render whole-rupee amounts with NO decimals ("500" not "500.00")
    page_break_after: int = 0    # >0 → split line items onto a 2nd page (banner repeated)
    intermediate_subtotal: bool = False  # with a page break, print a per-page "Total" SUBTOTAL
    #                              at the bottom of page 1 (the carried-forward running total a
    #                              real multi-page Tally invoice shows) — the money-halving bait


def _q0(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class _CLine:
    description: str
    hsn: str
    qty: Decimal
    unit: str
    unit_rate_paise: int
    taxable_paise: int
    gst_rate: Decimal
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_total_paise: int


def _compute(inv: TInvoice) -> tuple[list[_CLine], dict[str, int]]:
    out: list[_CLine] = []
    for ls in inv.lines:
        taxable = _q0(ls.qty * Decimal(ls.unit_rate_paise))
        if inv.intra:
            half = ls.gst_rate / Decimal(2)
            cgst = _q0(Decimal(taxable) * half / Decimal(100))
            sgst = _q0(Decimal(taxable) * half / Decimal(100)) + ls.tax_delta_paise
            igst = 0
        else:
            cgst = sgst = 0
            igst = _q0(Decimal(taxable) * ls.gst_rate / Decimal(100)) + ls.tax_delta_paise
        line_total = taxable + cgst + sgst + igst
        out.append(_CLine(ls.description, ls.hsn, ls.qty, ls.unit, ls.unit_rate_paise,
                          taxable, ls.gst_rate, cgst, sgst, igst, line_total))
    total_taxable = sum(c.taxable_paise for c in out)
    total_cgst = sum(c.cgst_paise for c in out)
    total_sgst = sum(c.sgst_paise for c in out)
    total_igst = sum(c.igst_paise for c in out)
    grand = total_taxable + total_cgst + total_sgst + total_igst + inv.round_off_paise
    totals = {
        "total_taxable_paise": total_taxable,
        "total_cgst_paise": total_cgst,
        "total_sgst_paise": total_sgst,
        "total_igst_paise": total_igst,
        "round_off_paise": inv.round_off_paise,
        "grand_total_paise": grand,
    }
    return out, totals


def _dec_str(d: Decimal) -> str:
    return format(d.normalize() if d == d.to_integral() else d, "f")


def _rate_str(rate: Decimal) -> str:
    return f"{_dec_str(rate)}%"


def _money(paise: int, whole: bool = False) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    if whole and p % 100 == 0:                      # no-decimal presentation ("500" not "500.00")
        return f"{sign}{p // 100:,}"
    return f"{sign}{p // 100:,}.{p % 100:02d}"


def _text_x(name: str) -> float:
    return _TEXT_L[name]


def _money_x(name: str, intra: bool) -> float:
    if name == "total":
        return _TOTAL_R_INTRA if intra else _TOTAL_R_INTER
    return _MONEY_R[name]


def _L(c: canvas.Canvas, x: float, y: float, text: str) -> None:
    c.drawString(x, y, text)


def _R(c: canvas.Canvas, x: float, y: float, text: str) -> None:
    c.drawRightString(x, y, text)


def _draw_masthead(c: canvas.Canvas, inv: TInvoice, y: float, *, continued: bool) -> float:
    c.setFont("Helvetica-Bold", 12)
    c.drawString(45.0, y, "Tax Invoice" + (" (continued)" if continued else ""))
    y -= 18.0
    c.setFont("Helvetica", 9)
    # Supplier masthead + the header-meta GRID (label row, value row beneath).
    c.drawString(45.0, y, inv.supplier_name)
    c.drawString(446.0, y, "Invoice No.")
    c.drawString(627.0, y, "Dated")
    y -= 12.0
    c.drawString(45.0, y, inv.supplier_address)
    c.drawString(446.0, y, inv.invoice_number)
    c.drawString(627.0, y, inv.invoice_date.strftime("%d-%b-%y"))
    y -= 12.0
    c.drawString(45.0, y, f"GSTIN/UIN: {inv.supplier_gstin}")
    y -= 12.0
    c.drawString(45.0, y, "Buyer's Order No.")       # a Tally anchor (strong signal 3)
    y -= 16.0
    c.drawString(45.0, y, "Buyer (Bill to)")
    y -= 12.0
    c.drawString(45.0, y, inv.buyer_name)
    y -= 12.0
    c.drawString(45.0, y, inv.buyer_address)
    y -= 12.0
    c.drawString(45.0, y, f"GSTIN/UIN : {inv.buyer_gstin}")
    y -= 12.0
    c.drawString(45.0, y, f"Place of Supply : {inv.place_of_supply}")
    return y - 18.0


def _draw_banner(c: canvas.Canvas, intra: bool, y: float) -> float:
    c.setFont("Helvetica-Bold", 7)
    _L(c, _text_x("sl"), y, "Sl")
    _L(c, _text_x("desc"), y, "Description of Goods")
    _L(c, _text_x("hsn"), y, "HSN/SAC")
    _L(c, _text_x("qty"), y, "Quantity")
    _R(c, _money_x("rate", intra), y, "Rate")
    _L(c, _text_x("per"), y, "per")
    _R(c, _money_x("amount", intra), y, "Amount")
    _R(c, _money_x("taxable", intra), y, "Taxable")
    if intra:
        _R(c, _money_x("cgst_amt", intra), y, "CGST")
        _R(c, _money_x("sgst_amt", intra), y, "SGST/UTGST")
    else:
        _R(c, _money_x("igst_amt", intra), y, "IGST")
    _R(c, _money_x("total", intra), y, "Total")
    y2 = y - 10.0
    _L(c, _text_x("sl"), y2, "No.")
    _R(c, _money_x("taxable", intra), y2, "Value")
    if intra:
        _R(c, _money_x("cgst_rate", intra), y2, "Rate")
        _R(c, _money_x("cgst_amt", intra), y2, "Amount")
        _R(c, _money_x("sgst_rate", intra), y2, "Rate")
        _R(c, _money_x("sgst_amt", intra), y2, "Amount")
    else:
        _R(c, _money_x("igst_rate", intra), y2, "Rate")
        _R(c, _money_x("igst_amt", intra), y2, "Amount")
    _R(c, _money_x("total", intra), y2, "Amount")
    return y2 - 14.0


def _draw_item(c: canvas.Canvas, inv: TInvoice, idx: int, cl: _CLine, y: float) -> float:
    intra, w = inv.intra, inv.whole_rupees
    c.setFont("Helvetica", 7)
    _L(c, _text_x("sl"), y, str(idx))
    _L(c, _text_x("desc"), y, cl.description)
    _L(c, _text_x("hsn"), y, cl.hsn)
    _L(c, _text_x("qty"), y, f"{_dec_str(cl.qty)} {cl.unit}")
    _R(c, _money_x("rate", intra), y, _money(cl.unit_rate_paise, w))
    _L(c, _text_x("per"), y, cl.unit)
    _R(c, _money_x("amount", intra), y, _money(cl.taxable_paise, w))
    _R(c, _money_x("taxable", intra), y, _money(cl.taxable_paise, w))
    if intra:
        _R(c, _money_x("cgst_rate", intra), y, _rate_str(cl.gst_rate / Decimal(2)))
        _R(c, _money_x("cgst_amt", intra), y, _money(cl.cgst_paise, w))
        _R(c, _money_x("sgst_rate", intra), y, _rate_str(cl.gst_rate / Decimal(2)))
        _R(c, _money_x("sgst_amt", intra), y, _money(cl.sgst_paise, w))
    else:
        _R(c, _money_x("igst_rate", intra), y, _rate_str(cl.gst_rate))
        _R(c, _money_x("igst_amt", intra), y, _money(cl.igst_paise, w))
    _R(c, _money_x("total", intra), y, _money(cl.line_total_paise, w))
    y -= 11.0
    if inv.lines[idx - 1].wrap:
        _L(c, _text_x("desc"), y, inv.lines[idx - 1].wrap or "")   # wrapped continuation row
        y -= 11.0
    return y


def build_tally_pdf(inv: TInvoice) -> tuple[bytes, dict[str, object]]:
    lines, totals = _compute(inv)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(_PAGE_W, _PAGE_H))
    c.setTitle("Tax Invoice")
    intra, w = inv.intra, inv.whole_rupees

    split = inv.page_break_after if 0 < inv.page_break_after < len(lines) else len(lines)
    page1 = list(enumerate(lines, start=1))[:split]
    page2 = list(enumerate(lines, start=1))[split:]

    y = _draw_masthead(c, inv, _PAGE_H - 30.0, continued=False)
    y = _draw_banner(c, intra, y)
    for idx, cl in page1:
        y = _draw_item(c, inv, idx, cl, y)

    if page2 and inv.intermediate_subtotal:
        # A per-page SUBTOTAL "Total" row at the foot of page 1 (the running carried-forward
        # total). Its lead token is "Total" — exactly what a naive parser mistakes for the
        # grand total, dropping page 2 and halving the invoice. Only the AMOUNT + TAXABLE
        # columns carry the page-1 running sum; no tax split, no grand.
        page1_taxable = sum(cl.taxable_paise for _, cl in page1)
        c.setFont("Helvetica", 7)
        c.drawString(250.0, y, "Total")
        _R(c, _money_x("amount", intra), y, _money(page1_taxable, w))
        _R(c, _money_x("taxable", intra), y, _money(page1_taxable, w))
        y -= 16.0

    if page2:                                         # second page (banner repeated)
        c.showPage()
        y = _draw_masthead(c, inv, _PAGE_H - 30.0, continued=True)
        y = _draw_banner(c, intra, y)
        for idx, cl in page2:
            y = _draw_item(c, inv, idx, cl, y)

    y -= 4.0
    # ---- summary rows (label-anchored) ----
    if intra:
        c.drawString(300.0, y, "CGST")
        _R(c, _money_x("amount", intra), y, _money(totals["total_cgst_paise"], w))
        y -= 11.0
        c.drawString(300.0, y, "SGST")
        _R(c, _money_x("amount", intra), y, _money(totals["total_sgst_paise"], w))
        y -= 11.0
    else:
        c.drawString(300.0, y, "IGST")
        _R(c, _money_x("amount", intra), y, _money(totals["total_igst_paise"], w))
        y -= 11.0
    if inv.round_off_paise:
        c.drawString(300.0, y, "ROUND OFF")
        _R(c, _money_x("amount", intra), y, _money(inv.round_off_paise, w))
        y -= 11.0

    # ---- grand-total row (bill total in the AMOUNT column) ----
    printed_grand = (inv.grand_total_override_paise
                     if inv.grand_total_override_paise is not None
                     else int(totals["grand_total_paise"]))
    total_qty = sum((c2.qty for c2 in lines), Decimal(0))
    printed_taxable = (inv.total_taxable_override_paise
                       if inv.total_taxable_override_paise is not None
                       else int(totals["total_taxable_paise"]))
    c.drawString(250.0, y, "Total")
    _L(c, _text_x("qty"), y, f"{_dec_str(total_qty)}")
    _R(c, _money_x("amount", intra), y, _money(printed_grand, w))
    _R(c, _money_x("taxable", intra), y, _money(printed_taxable, w))
    if intra:
        _R(c, _money_x("cgst_amt", intra), y, _money(int(totals["total_cgst_paise"]), w))
        _R(c, _money_x("sgst_amt", intra), y, _money(int(totals["total_sgst_paise"]), w))
    else:
        _R(c, _money_x("igst_amt", intra), y, _money(int(totals["total_igst_paise"]), w))
    y -= 16.0

    c.setFont("Helvetica", 8)
    c.drawString(45.0, y, "Amount Chargeable (in words) INR As Rendered Only")
    y -= 14.0
    c.drawString(45.0, y, "This is a Computer Generated Invoice")
    c.showPage()
    c.save()

    gold = _build_gold(inv, lines, totals)
    return buf.getvalue(), gold


def _build_gold(inv: TInvoice, lines: list[_CLine],
                totals: dict[str, int]) -> dict[str, object]:
    return {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "doc_type": "gst_invoice",
        "header": {
            "supplier_gstin": inv.supplier_gstin,
            "buyer_gstin": inv.buyer_gstin,
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date,
            "place_of_supply": inv.place_of_supply,
        },
        "totals": dict(totals),
        "lines": [
            {
                "description": c.description, "hsn_sac": c.hsn, "quantity": c.qty,
                "unit": c.unit, "unit_rate_paise": c.unit_rate_paise,
                "taxable_paise": c.taxable_paise, "gst_rate": c.gst_rate,
                "cgst_paise": c.cgst_paise, "sgst_paise": c.sgst_paise,
                "igst_paise": c.igst_paise, "line_total_paise": c.line_total_paise,
            }
            for c in lines
        ],
    }


# ===========================================================================================
# GLUED-TOKEN Tally layout (regression fixture for the item-row token-splitter)
# ===========================================================================================
# A real Tally "Sales TI" IGST invoice renders its tight right-aligned tax columns so that
# pdfplumber GLUES adjacent cells into a single word: the Sl digit onto the description start
# ("1Acme"), a uom+rate+per run ("PCS40,000.00PCS"), and a taxable+IGST-rate+IGST-amount run
# ("40,000.0018%7,200.00"). This builder reproduces that geometry (column x-anchors matching a
# genuine Tally grid; FICTIONAL data) by drawing those runs as CONTIGUOUS strings so the text
# layer carries the same glued tokens — the exact shape that yields 0 line items without the
# splitter, and the canonical line WITH it.


@dataclass
class TGluedInvoice:
    supplier_name: str
    supplier_gstin: str
    supplier_address: str
    buyer_name: str
    buyer_gstin: str
    buyer_address: str
    invoice_number: str
    invoice_date: date
    place_of_supply: str
    description: str
    description_first: str    # the description's first word, GLUED to the Sl digit ("1Acme")
    hsn: str
    qty: Decimal
    unit: str
    unit_rate_paise: int
    gst_rate: Decimal         # inter-state IGST percent (e.g. 18)


# Column x-anchors of a genuine Tally IGST grid (right edge for money, left for text). The tight
# tax columns are what make abutting cells glue into one token under pdfplumber word extraction.
_G_SL_X = 44.0
_G_HSN_X = 540.0
_G_QTY_X = 580.0
_G_UOMRATE_X = 590.0          # left x of the glued "PCS<rate>PCS" run
_G_AMOUNT_R = 685.0
_G_TAXBLK_X = 688.0           # left x of the glued "<taxable><igst%><igst>" run
_G_TAXABLE_R = 721.0
_G_IGST_R = 765.0
_G_TOTAL_R = 804.0


def _g_banner(c: canvas.Canvas, y: float) -> float:
    c.setFont("Helvetica-Bold", 7)
    c.drawString(_G_SL_X, y, "Sl")
    c.drawString(257.0, y, "Description of Goods")
    c.drawString(537.0, y, "HSN/SAC")
    c.drawString(574.0, y, "Quantity")
    c.drawString(613.0, y, "Rate")
    c.drawString(639.0, y, "per")
    c.drawString(656.0, y, "Amount")
    c.drawString(691.0, y, "Taxable")
    c.drawString(732.0, y, "IGST")
    c.drawString(776.0, y, "Total")
    y2 = y - 11.0
    c.drawString(_G_SL_X, y2, "No.")
    c.drawString(690.0, y2, "Value")
    c.drawString(724.0, y2, "Rate")
    c.drawString(744.0, y2, "Amount")
    c.drawString(774.0, y2, "Amount")
    return y2 - 14.0


def build_tally_glued_igst_pdf(inv: TGluedInvoice) -> tuple[bytes, dict[str, object]]:
    """Render a single-line inter-state Tally IGST invoice whose item + total rows carry GLUED
    multi-column tokens, and return ``(pdf_bytes, gold)`` in the eval-gold record shape."""
    taxable = _q0(inv.qty * Decimal(inv.unit_rate_paise))
    igst = _q0(Decimal(taxable) * inv.gst_rate / Decimal(100))
    line_total = taxable + igst
    grand = line_total

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(_PAGE_W, _PAGE_H))
    c.setTitle("Tax Invoice")

    masthead_inv = TInvoice(
        supplier_name=inv.supplier_name, supplier_gstin=inv.supplier_gstin,
        supplier_address=inv.supplier_address, buyer_name=inv.buyer_name,
        buyer_gstin=inv.buyer_gstin, buyer_address=inv.buyer_address,
        invoice_number=inv.invoice_number, invoice_date=inv.invoice_date,
        place_of_supply=inv.place_of_supply, intra=False, lines=[])
    y = _draw_masthead(c, masthead_inv, _PAGE_H - 30.0, continued=False)
    y = _g_banner(c, y)

    # ---- item row: contiguous draws so pdfplumber glues the intended runs ----
    c.setFont("Helvetica", 7)
    rate_s = _money(inv.unit_rate_paise)
    tax_s = _money(taxable)
    igst_s = _money(igst)
    # (a) Sl digit glued onto the description start; the rest of the description follows.
    desc_rest = inv.description[len(inv.description_first):].lstrip()
    c.drawString(_G_SL_X, y, f"1{inv.description_first} {desc_rest}".rstrip())
    c.drawString(_G_HSN_X, y, inv.hsn)
    c.drawString(_G_QTY_X, y, _dec_str(inv.qty))
    # (b) glued uom+rate+per, and glued taxable+IGST-rate+IGST-amount (one drawString each). The
    # inclusive "Amount" cell is intentionally omitted (Tally often prints only the Taxable Value
    # on the item row); taxable comes from the tax block, so no separate Amount value is needed.
    c.drawString(_G_UOMRATE_X, y, f"{inv.unit}{rate_s}{inv.unit}")
    c.drawString(_G_TAXBLK_X, y, f"{tax_s}{_rate_str(inv.gst_rate)}{igst_s}")
    c.drawRightString(_G_TOTAL_R, y, _money(line_total))
    y -= 14.0

    # ---- IGST summary line (label-anchored total) + the grand-total row ----
    c.drawString(520.0, y, "IGST")
    c.drawRightString(_G_AMOUNT_R, y, igst_s)
    y -= 14.0
    c.drawString(521.0, y, "Total")
    c.drawRightString(_G_AMOUNT_R, y, _money(grand))
    c.drawRightString(_G_TAXABLE_R, y, tax_s)
    c.drawRightString(_G_IGST_R, y, igst_s)
    y -= 16.0

    c.setFont("Helvetica", 8)
    c.drawString(45.0, y, "Amount Chargeable (in words) INR As Rendered Only")
    y -= 14.0
    c.drawString(45.0, y, "This is a Computer Generated Invoice")
    c.showPage()
    c.save()

    gold: dict[str, object] = {
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "doc_type": "gst_invoice",
        "page_count": 1,
        "needs_ocr": False,
        "review_needed": False,
        "header": {
            "supplier_name": inv.supplier_name,
            "supplier_gstin": inv.supplier_gstin,
            "supplier_address": inv.supplier_address,
            "buyer_name": inv.buyer_name,
            "buyer_gstin": inv.buyer_gstin,
            "buyer_address": inv.buyer_address,
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat(),
            "place_of_supply": inv.place_of_supply,
            "po_ref": None,
        },
        "totals": {
            "total_taxable_paise": taxable,
            "total_cgst_paise": None,
            "total_sgst_paise": None,
            "total_igst_paise": igst,
            "round_off_paise": None,
            "grand_total_paise": grand,
            "amount_in_words": None,
        },
        "lines": [
            {
                "line_no": 1,
                "description": inv.description,
                "hsn_sac": inv.hsn,
                "quantity": _dec_str(inv.qty),
                "unit": inv.unit,
                "unit_rate_paise": inv.unit_rate_paise,
                "taxable_paise": taxable,
                "gst_rate": _dec_str(inv.gst_rate),
                "cgst_paise": None,
                "sgst_paise": None,
                "igst_paise": igst,
                "line_total_paise": line_total,
            }
        ],
    }
    return buf.getvalue(), gold


# A few checksum-valid GSTINs (verified via masterdata.normalize.valid_gstin), reused across
# fixtures. Kept here so a fixture never spuriously trips the checksum gate.
GSTIN_SUPPLIER_WB = "19AAACT9811F1Z9"   # West Bengal (19)
GSTIN_BUYER_WB = "19AABCB2066P1ZC"      # West Bengal (19) → intra with a WB supplier
GSTIN_SUPPLIER_KA = "29AABCU9603R1ZJ"   # Karnataka (29)
GSTIN_BUYER_WB2 = "19AABCB2066P1ZC"     # West Bengal (19) → inter vs a Karnataka supplier
