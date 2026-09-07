"""Extraction tests for `TextLayerExtractor` over synthetic, text-layer GST invoices.

The invoices are generated in-process with reportlab (a self-contained helper below —
reportlab is a TEST-ONLY dependency, never imported by runtime code) so every labelled
value is known by construction. We render a real header block + a ruled line-item table +
labelled totals, then assert the extractor's paise integers, per-field statuses, confidence
banding, arithmetic flags, and the review gate against the ground truth.

Cases (per the design's extractor acceptance list):
  (a) happy intra-state (CGST+SGST)  -> all required OK, review False, arithmetic all True
  (b) inter-state IGST, 3 lines/2 HSNs, different column order + a sub-total row to skip
  (c) a supplier GSTIN with a bad checksum -> that field LOW_CONFIDENCE + a review reason
  (d) totals that don't add up -> review True + the failing arithmetic flag
  (e) a blank/image-only PDF (no text layer) -> needs_ocr True + all fields MISSING
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.modules.expense.canonical import FieldStatus
from app.modules.expense.eval.synth import (
    GSTIN_BUYER_KA,
    GSTIN_BUYER_MH,
    GSTIN_SUPPLIER_GJ,
    GSTIN_SUPPLIER_MH,
    HashRateInvoiceSpec,
    HashRateLine,
    InvoiceSpec,
    LineSpec,
    build_hash_rate_invoice_pdf,
    build_invoice_pdf,
)
from app.modules.expense.text_layer import (
    InvoiceExtractionError,
    TextLayerExtractor,
    _map_headers,
    _money_to_paise,
    _parse_tables,
    _words_of_page,
)
from app.modules.masterdata.normalize import _gstin_check_char

# --------------------------------------------------------------------------- GSTIN fixtures


def _gstin(first14: str) -> str:
    """A structurally valid, checksum-correct GSTIN from its first 14 chars."""
    return first14 + _gstin_check_char(first14)


def _break_checksum(gstin: str) -> str:
    """Flip the check digit so the GSTIN is regex-valid but fails `valid_gstin`."""
    wrong = "0" if gstin[14] != "0" else "1"
    return gstin[:14] + wrong


SUPPLIER_KA = _gstin("29AABCU9603R1Z")   # Karnataka (29)
SUPPLIER_MH = _gstin("27AABCU9603R1Z")   # Maharashtra (27)
BUYER_KA = _gstin("29AAECS1234F1Z")      # Karnataka (29)


# --------------------------------------------------------------------------- PDF builders


def _build_invoice_pdf(
    *,
    supplier_name: str,
    supplier_address: str,
    supplier_gstin: str,
    buyer_name: str,
    buyer_gstin: str,
    invoice_no: str,
    invoice_date: str,
    place_of_supply: str,
    po_ref: str,
    headers: list[str],
    rows: list[list[str]],
    totals: list[tuple[str, str]],
    buyer_address: str = "14 Residency Road, Bengaluru 560025",
    include_place_of_supply: bool = True,
) -> bytes:
    """Render a single-page GST invoice: header paragraphs + a ruled table + totals lines.

    ``include_place_of_supply=False`` omits the Place-of-Supply line so the supply type must be
    inferred from the supplier↔buyer GSTIN state codes (M4).
    """
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9, leading=12)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=36, bottomMargin=36,
                            leftMargin=36, rightMargin=36)
    story: list[object] = [
        Paragraph(f"<b>{supplier_name}</b>", body),
        Paragraph(supplier_address, body),
        Paragraph(f"GSTIN: {supplier_gstin}", body),
        Spacer(1, 8),
        Paragraph("<b>TAX INVOICE</b>", body),
        Spacer(1, 8),
        Paragraph(f"Bill To: {buyer_name}", body),
        Paragraph(buyer_address, body),
        Paragraph(f"GSTIN: {buyer_gstin}", body),
        Spacer(1, 6),
        Paragraph(f"Invoice No: {invoice_no}     Invoice Date: {invoice_date}", body),
    ]
    if include_place_of_supply:
        story.append(Paragraph(f"Place of Supply: {place_of_supply}", body))
    story.append(Paragraph(f"PO Ref: {po_ref}", body))
    story.append(Spacer(1, 10))
    table = Table([headers, *rows], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))
    for label, value in totals:
        story.append(Paragraph(f"{label}: {value}", body))
    doc.build(story)
    return buf.getvalue()


def _build_blank_pdf() -> bytes:
    """A one-page PDF with graphics but NO text layer (simulates a scanned image)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFillColorRGB(0.9, 0.9, 0.9)
    c.rect(72, 72, 400, 600, fill=1, stroke=0)
    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------------- concrete docs


def _intra_pdf(supplier_gstin: str = SUPPLIER_KA) -> bytes:
    # 1 line: qty 10 @ 1000.00 = 10,000.00 taxable; 18% -> CGST 900 + SGST 900; grand 11,800.
    return _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=supplier_gstin,
        buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA,
        invoice_no="INV-2026-0042",
        invoice_date="15-05-2026",
        place_of_supply="Karnataka (29)",
        po_ref="PO-778",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "CGST", "SGST", "IGST", "Total"],
        rows=[["Widget Assembly", "8471", "10", "Nos", "1,000.00", "10,000.00",
               "18", "900.00", "900.00", "0.00", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "900.00"), ("SGST", "900.00"),
                ("IGST", "0.00"), ("Round Off", "0.00"), ("Grand Total", "11,800.00"),
                ("Amount in words", "Rupees Eleven Thousand Eight Hundred Only")],
    )


def _inter_pdf() -> bytes:
    # MH supplier -> KA place of supply -> inter-state IGST. 3 lines, 2 HSNs, a sub-total row.
    # L1 8471 qty2@5000 =10,000 @18% -> IGST 1,800 ; L2 8471 qty1@2000 =2,000 @18% -> 360 ;
    # L3 9403 qty3@1000 = 3,000 @12% -> 360.  Taxable 15,000 ; IGST 2,520 ; grand 17,520.
    return _build_invoice_pdf(
        supplier_name="Deccan Electricals Limited",
        supplier_address="88 MIDC, Pune, Maharashtra 411001",
        supplier_gstin=SUPPLIER_MH,
        buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA,
        invoice_no="DE/2026/1187",
        invoice_date="02-06-2026",
        place_of_supply="Karnataka (29)",
        po_ref="PO-901",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "IGST", "Total"],
        rows=[
            ["Server Rack", "8471", "2", "Nos", "5,000.00", "10,000.00", "18",
             "1,800.00", "11,800.00"],
            ["Patch Panel", "8471", "1", "Nos", "2,000.00", "2,000.00", "18",
             "360.00", "2,360.00"],
            ["Cabinet Shelf", "9403", "3", "Nos", "1,000.00", "3,000.00", "12",
             "360.00", "3,360.00"],
            ["Sub Total", "", "", "", "", "15,000.00", "", "2,520.00", "17,520.00"],
        ],
        totals=[("Taxable Value", "15,000.00"), ("CGST", "0.00"), ("SGST", "0.00"),
                ("IGST", "2,520.00"), ("Round Off", "0.00"), ("Grand Total", "17,520.00"),
                ("Amount in words", "Rupees Seventeen Thousand Five Hundred Twenty Only")],
    )


def _mismatch_pdf() -> bytes:
    # Same as intra but the printed Grand Total is wrong (12,000 vs the correct 11,800).
    return _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=SUPPLIER_KA,
        buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA,
        invoice_no="INV-2026-0043",
        invoice_date="16-05-2026",
        place_of_supply="Karnataka (29)",
        po_ref="PO-779",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "CGST", "SGST", "IGST", "Total"],
        rows=[["Widget Assembly", "8471", "10", "Nos", "1,000.00", "10,000.00",
               "18", "900.00", "900.00", "0.00", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "900.00"), ("SGST", "900.00"),
                ("IGST", "0.00"), ("Round Off", "0.00"), ("Grand Total", "12,000.00")],
    )


# --------------------------------------------------------------------------- helpers

EX = TextLayerExtractor()


# =========================================================================== (a) happy intra


def test_a_happy_intra_state_all_required_ok_no_review() -> None:
    result = EX.extract(_intra_pdf())

    assert result.needs_ocr is False
    assert result.page_count == 1
    assert result.source_engine == "text_layer/1.0"
    assert result.schema_version == "gst_invoice/1.0.0"

    h = result.header
    assert h.supplier_gstin.value_normalized == SUPPLIER_KA
    assert h.supplier_gstin.status is FieldStatus.OK
    assert h.buyer_gstin.value_normalized == BUYER_KA
    assert h.buyer_gstin.status is FieldStatus.OK
    assert h.invoice_number.value_normalized == "INV-2026-0042"
    assert h.invoice_date.value_normalized is not None
    assert h.invoice_date.value_normalized.isoformat() == "2026-05-15"
    assert h.place_of_supply.value_normalized == "Karnataka (29)"

    # money as integer paise
    t = result.totals
    assert t.total_taxable_paise.value_normalized == 1_000_000
    assert t.total_cgst_paise.value_normalized == 90_000
    assert t.total_sgst_paise.value_normalized == 90_000
    assert t.total_igst_paise.value_normalized == 0
    assert t.grand_total_paise.value_normalized == 1_180_000

    assert len(result.lines) == 1
    ln = result.lines[0]
    assert ln.hsn_sac.value_normalized == "8471"
    assert ln.quantity.value_normalized is not None and int(ln.quantity.value_normalized) == 10
    assert ln.unit_rate_paise.value_normalized == 100_000
    assert ln.taxable_paise.value_normalized == 1_000_000
    assert ln.cgst_paise.value_normalized == 90_000
    assert ln.sgst_paise.value_normalized == 90_000
    assert ln.igst_paise.value_normalized == 0
    assert ln.line_total_paise.value_normalized == 1_180_000

    a = result.arithmetic
    assert a.lines_sum_matches_taxable is True
    assert a.per_line_tax_consistent is True
    assert a.totals_add_to_grand is True
    assert a.supply_type_consistent is True
    assert a.max_abs_delta_paise == 0

    # calibrated confidence banding: arithmetic-corroborated required fields hit the top band.
    assert h.supplier_gstin.confidence == 0.95
    assert t.total_taxable_paise.confidence == 0.95
    assert t.grand_total_paise.confidence == 0.95
    assert h.invoice_number.confidence == 0.80  # ids can't be arithmetic-corroborated

    assert result.review_needed is False
    assert result.review_reasons == []

    # hashes are present + well-formed
    assert len(result.content_hash) == 64
    assert len(result.dedup_key) == 64


# =========================================================================== (b) inter IGST


def test_b_inter_state_igst_three_lines_two_hsns() -> None:
    result = EX.extract(_inter_pdf())

    assert result.needs_ocr is False
    # the sub-total row is skipped -> exactly 3 item lines
    assert len(result.lines) == 3
    hsns = [ln.hsn_sac.value_normalized for ln in result.lines]
    assert hsns == ["8471", "8471", "9403"]
    assert len({h for h in hsns}) == 2

    taxables = [ln.taxable_paise.value_normalized for ln in result.lines]
    assert taxables == [1_000_000, 200_000, 300_000]
    igsts = [ln.igst_paise.value_normalized for ln in result.lines]
    assert igsts == [180_000, 36_000, 36_000]
    # intra-only columns are absent in this layout -> MISSING, not zero-guessed
    assert result.lines[0].cgst_paise.status is FieldStatus.MISSING

    t = result.totals
    assert t.total_taxable_paise.value_normalized == 1_500_000
    assert t.total_igst_paise.value_normalized == 252_000
    assert t.grand_total_paise.value_normalized == 1_752_000

    a = result.arithmetic
    assert a.lines_sum_matches_taxable is True
    assert a.per_line_tax_consistent is True
    assert a.totals_add_to_grand is True
    assert a.supply_type_consistent is True

    assert result.review_needed is False
    assert result.review_reasons == []


# =========================================================================== (c) bad checksum


def test_c_bad_supplier_gstin_checksum_is_low_confidence_and_flagged() -> None:
    bad = _break_checksum(SUPPLIER_KA)
    result = EX.extract(_intra_pdf(supplier_gstin=bad))

    sg = result.header.supplier_gstin
    assert sg.value_normalized == bad                 # still captured verbatim
    assert sg.status is FieldStatus.LOW_CONFIDENCE
    assert sg.confidence == 0.50

    assert result.review_needed is True
    assert any("checksum" in r for r in result.review_reasons)
    # the buyer GSTIN is valid, so its checksum is NOT among the reasons
    assert not any("buyer_gstin failed" in r for r in result.review_reasons)


# =========================================================================== (d) totals wrong


def test_d_arithmetic_mismatch_flags_totals_add_check() -> None:
    result = EX.extract(_mismatch_pdf())

    # the printed grand total (12,000) != taxable+taxes+round-off (11,800)
    assert result.totals.grand_total_paise.value_normalized == 1_200_000
    assert result.arithmetic.totals_add_to_grand is False
    assert result.arithmetic.max_abs_delta_paise == 20_000  # ₹200 in paise

    assert result.review_needed is True
    assert any("grand total" in r for r in result.review_reasons)
    # grand total is only anchored, not corroborated -> middle confidence band
    assert result.totals.grand_total_paise.confidence == 0.80


# =========================================================================== (e) scanned/no-text


def test_e_blank_image_pdf_needs_ocr_all_missing() -> None:
    result = EX.extract(_build_blank_pdf())

    assert result.needs_ocr is True
    assert result.page_count == 1
    assert result.lines == []

    h = result.header
    assert h.supplier_gstin.status is FieldStatus.MISSING
    assert h.invoice_number.status is FieldStatus.MISSING
    assert result.totals.grand_total_paise.status is FieldStatus.MISSING

    assert result.review_needed is True
    assert any("OCR" in r for r in result.review_reasons)


# =========================================================================== domain errors


def test_non_pdf_bytes_raise_clean_domain_error() -> None:
    try:
        EX.extract(b"this is definitely not a pdf")
    except InvoiceExtractionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected InvoiceExtractionError on non-PDF input")


def test_unsupported_doc_type_raises_value_error() -> None:
    try:
        EX.extract(_intra_pdf(), doc_type="purchase_order")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on an unsupported doc_type")


# =========================================================================== ADVERSARIAL
# Each test below reproduces a CONFIRMED extractor defect and asserts the fixed behaviour;
# every one FAILS on the pre-fix extractor, so it is a genuine regression guard.


# --- H1: supplier GSTIN just ABOVE "Bill To" must not be mistaken for the buyer ------------


def test_h1_supplier_gstin_above_billto_is_not_swapped_with_buyer() -> None:
    """Tight header (no supplier↔buyer gap) on a SAME-STATE invoice: the supplier GSTIN is the
    Euclidean-nearest token to "Bill To", yet the buyer must be taken strictly BELOW it."""
    spec = InvoiceSpec(
        supplier_name="Umang Traders", supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd", buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="UMG/2026/0099", invoice_date=date(2026, 5, 20),
        place_of_supply="Maharashtra (27)", intra_state=True, po_ref="PO-88010",
        tight_header=True,
        lines=[LineSpec("Office chair", "9401", Decimal("4"), "NOS", 450000, Decimal("18"))],
    )
    pdf, _ = build_invoice_pdf(spec)
    result = EX.extract(pdf)

    # No silent swap: supplier stays the top-most GSTIN, buyer the one below the anchor.
    assert result.header.supplier_gstin.value_normalized == GSTIN_SUPPLIER_MH
    assert result.header.buyer_gstin.value_normalized == GSTIN_BUYER_MH
    assert result.header.supplier_gstin.status is FieldStatus.OK


# --- H2: borderless right-aligned money + a multi-word header cell --------------------------


def test_h2_borderless_right_aligned_money_maps_taxable() -> None:
    """A borderless grid with RIGHT-aligned money and a 'Taxable Value' header: the taxable
    column must be read (nearest-center bucketing + multi-word header span), not dropped."""
    spec = InvoiceSpec(
        supplier_name="Gujarat Poly Pack LLP", supplier_gstin=GSTIN_SUPPLIER_GJ,
        supplier_address="Plot 22 GIDC, Vapi, Gujarat 396195",
        buyer_name="Southern Retail Pvt Ltd", buyer_gstin="29AAECS4321L1Z5",
        buyer_address="45 MG Road, Bengaluru, Karnataka 560001",
        invoice_number="GPP-INV-2026-220", invoice_date=date(2026, 6, 12),
        place_of_supply="Karnataka (29)", intra_state=False,
        borderless=True, header_labels={"taxable": "Taxable Value", "hsn": "HSN"},
        lines=[
            LineSpec("PP bag", "6305", Decimal("2"), "BAG", 125000, Decimal("18")),
            LineSpec("HDPE drum", "3923", Decimal("1"), "NOS", 300000, Decimal("28")),
        ],
    )
    pdf, _ = build_invoice_pdf(spec)
    result = EX.extract(pdf)

    assert len(result.lines) == 2
    assert [ln.taxable_paise.value_normalized for ln in result.lines] == [250_000, 300_000]
    assert [ln.igst_paise.value_normalized for ln in result.lines] == [45_000, 84_000]
    assert result.arithmetic.lines_sum_matches_taxable is True
    assert result.review_needed is False


# --- M3: money tokenizer must validate the WHOLE cell (sign / EU comma / clamp) -------------


def test_m3_money_tokenizer_reads_signed_and_rejects_malformed() -> None:
    assert _money_to_paise("1,180.00") == 118_000
    assert _money_to_paise("(1,180.00)") == -118_000          # accounting negative
    assert _money_to_paise("1,180.00-") == -118_000           # trailing sign
    assert _money_to_paise("(0.30)") == -30                   # small paren round-off
    assert _money_to_paise("₹ 1,180.00") == 118_000       # ₹ ornament stripped
    # Malformed / ambiguous → None (never a silently-truncated wrong value):
    assert _money_to_paise("1.234,50") is None                # European decimal comma
    assert _money_to_paise("1，180.00") is None            # fullwidth comma
    assert _money_to_paise("1,180.00 xyz") is None            # leftover junk
    # Clamp: an out-of-range figure (beyond the challan money ceiling) → None, not overflow.
    assert _money_to_paise("999999999999999999.00") is None


def test_m3_accounting_paren_round_off_is_negative() -> None:
    """The end-to-end round-off path reads a parenthesised negative from the totals block."""
    spec = InvoiceSpec(
        supplier_name="Northline Hardware Co", supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="7 Nashik Highway, Pune, Maharashtra 411001",
        buyer_name="Gifsy Solutions Ltd", buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="NHC/26-27/0410", invoice_date=date(2026, 7, 14),
        place_of_supply="Maharashtra (27)", intra_state=True,
        accounting_negatives=True, round_off_paise=-30,
        lines=[LineSpec("Bolt assortment", "7318", Decimal("3"), "BOX", 33000, Decimal("18"))],
    )
    pdf, _ = build_invoice_pdf(spec)
    result = EX.extract(pdf)
    assert result.totals.round_off_paise.value_normalized == -30
    assert result.arithmetic.totals_add_to_grand is True


def test_m3_european_format_money_is_flagged_not_silently_wrong() -> None:
    """A line taxable printed in European format (1.234,50) must fail closed to MISSING and
    trip review — never be silently truncated to a wrong 123 paise."""
    pdf = _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=SUPPLIER_KA, buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA, invoice_no="INV-2026-0050", invoice_date="18-05-2026",
        place_of_supply="Karnataka (29)", po_ref="PO-780",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "CGST", "SGST", "IGST", "Total"],
        # Only the taxable cell is European-format; the other money cells are valid so the row
        # still registers as an item and the taxable field's failure is observable.
        rows=[["Widget Assembly", "8471", "10", "Nos", "1,000.00", "1.234,50",
               "18", "900.00", "900.00", "0.00", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "900.00"), ("SGST", "900.00"),
                ("IGST", "0.00"), ("Round Off", "0.00"), ("Grand Total", "11,800.00")],
    )
    result = EX.extract(pdf)
    assert len(result.lines) == 1
    assert result.lines[0].taxable_paise.status is FieldStatus.MISSING
    assert result.review_needed is True


# --- M4: infer intra/inter from GSTIN state codes when Place of Supply is absent ------------


def test_m4_wrong_tax_head_flagged_when_place_of_supply_absent() -> None:
    """Same-state supplier & buyer (intra), but the invoice wrongly charges IGST and there is
    NO Place of Supply line. The supply type must be inferred from the GSTIN state codes and
    the wrong head flagged — not punted."""
    pdf = _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=SUPPLIER_KA,               # 29 (Karnataka)
        buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA,                     # 29 (Karnataka) → intra-state
        invoice_no="INV-2026-0051", invoice_date="19-05-2026",
        place_of_supply="", po_ref="PO-781", include_place_of_supply=False,
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "IGST", "Total"],
        rows=[["Widget Assembly", "8471", "10", "Nos", "1,000.00", "10,000.00",
               "18", "1,800.00", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "0.00"), ("SGST", "0.00"),
                ("IGST", "1,800.00"), ("Round Off", "0.00"), ("Grand Total", "11,800.00")],
    )
    result = EX.extract(pdf)
    assert result.header.place_of_supply.status is FieldStatus.MISSING
    assert result.arithmetic.supply_type_consistent is False
    assert result.review_needed is True
    assert any("intra/inter" in r for r in result.review_reasons)


def test_nil_rated_invoice_supply_type_consistent() -> None:
    """A 0%/exempt invoice (no tax head at all) is consistent with ANY supply type — it must
    NOT trip the intra/inter tax-split contradiction flag (was a false review flag for
    nil-rated intra-state sales). A REAL contradiction (a tax head on the wrong side) still
    flags."""
    from app.modules.expense.text_layer import _supply_type_consistent

    # nil-rated intra-state (pos == supplier) and inter-state: both consistent (no tax head).
    assert _supply_type_consistent("27", "27", "27", 0, 0, 0) is True
    assert _supply_type_consistent("27", "19", "27", 0, 0, 0) is True
    # regression: a genuine wrong-head still flags, and a correct split stays consistent.
    assert _supply_type_consistent("27", "27", "27", 0, 0, 5000) is False   # intra w/ IGST
    assert _supply_type_consistent("27", "27", "27", 5000, 5000, 0) is True  # intra CGST+SGST
    assert _supply_type_consistent("27", "19", "27", 5000, 0, 0) is False    # inter w/ CGST


# --- M5: a total must not earn the 0.95 band when no corroborating check actually ran -------


def test_m5_totals_not_top_confidence_when_no_lines_to_corroborate() -> None:
    """An invoice whose only table row is a summary row yields NO line items, so the Σ-lines
    check never RUNS. The taxable total (which that check corroborates) must be capped at OK
    (0.80), not handed a vacuous 0.95 — while the grand total, whose OWN totals-add check DID
    run and pass, legitimately keeps the top band."""
    pdf = _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=SUPPLIER_KA, buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA, invoice_no="INV-2026-0052", invoice_date="20-05-2026",
        place_of_supply="Karnataka (29)", po_ref="PO-782",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "CGST", "SGST", "IGST", "Total"],
        rows=[["Sub Total", "", "", "", "", "10,000.00", "", "", "", "", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "900.00"), ("SGST", "900.00"),
                ("IGST", "0.00"), ("Round Off", "0.00"), ("Grand Total", "11,800.00")],
    )
    result = EX.extract(pdf)
    assert result.lines == []                                  # the summary row is not an item
    assert result.totals.total_taxable_paise.value_normalized == 1_000_000
    assert result.totals.total_taxable_paise.confidence == 0.80  # Σ-lines check never ran
    assert result.totals.grand_total_paise.confidence == 0.95    # totals-add check ran + passed


# --- M6: pre-tax "Amount" beside gross "Total" must both map (no synonym collision) ---------


def test_m6_amount_and_total_columns_do_not_collide() -> None:
    spec = InvoiceSpec(
        supplier_name="Umang Traders", supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd", buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="UMG/2026/0140", invoice_date=date(2026, 5, 28),
        place_of_supply="Maharashtra (27)", intra_state=True,
        header_labels={"taxable": "Amount"},
        lines=[LineSpec("Steel rack", "9403", Decimal("2"), "NOS", 500000, Decimal("18"))],
    )
    pdf, _ = build_invoice_pdf(spec)
    result = EX.extract(pdf)
    ln = result.lines[0]
    assert ln.taxable_paise.value_normalized == 1_000_000     # "Amount" → pre-tax taxable
    assert ln.line_total_paise.value_normalized == 1_180_000  # "Total"  → gross line total
    assert result.arithmetic.lines_sum_matches_taxable is True
    assert result.review_needed is False


# --- L7: a real product whose description starts with a summary keyword is KEPT -------------


def test_l7_product_line_starting_with_summary_keyword_is_kept() -> None:
    pdf = _build_invoice_pdf(
        supplier_name="Acme Supplies Private Limited",
        supplier_address="12 Industrial Area, Bengaluru, Karnataka 560001",
        supplier_gstin=SUPPLIER_KA, buyer_name="Northstar Traders LLP",
        buyer_gstin=BUYER_KA, invoice_no="INV-2026-0053", invoice_date="21-05-2026",
        place_of_supply="Karnataka (29)", po_ref="PO-783",
        headers=["Description", "HSN/SAC", "Qty", "Unit", "Rate", "Taxable Value",
                 "GST%", "CGST", "SGST", "IGST", "Total"],
        rows=[["Total Station Survey Kit", "9015", "1", "Nos", "10,000.00", "10,000.00",
               "18", "900.00", "900.00", "0.00", "11,800.00"]],
        totals=[("Taxable Value", "10,000.00"), ("CGST", "900.00"), ("SGST", "900.00"),
                ("IGST", "0.00"), ("Round Off", "0.00"), ("Grand Total", "11,800.00")],
    )
    result = EX.extract(pdf)
    assert len(result.lines) == 1
    assert result.lines[0].description.value_normalized == "Total Station Survey Kit"


# --- L9: C0 control characters are stripped from stored text fields -------------------------


class _StubPage:
    """Minimal pdfplumber-page stand-in: yields one word carrying a C0 control char."""

    def extract_words(self, use_text_flow: bool = False) -> list[dict[str, object]]:
        return [{"text": "Acme\x07 Supplies\x1f", "x0": 10.0, "x1": 60.0,
                 "top": 20.0, "bottom": 30.0}]


def test_l9_control_chars_stripped_from_word_text() -> None:
    words = _words_of_page(_StubPage(), 1)
    assert len(words) == 1
    assert words[0].text == "Acme Supplies"                    # \x07 and \x1f removed
    assert "\x07" not in words[0].text and "\x1f" not in words[0].text


def test_assign_gstins_flags_buyer_first_masthead() -> None:
    """Re-audit residual: a BUYER-FIRST masthead ("Bill To" above the top-most GSTIN) must
    NOT silently swap supplier/buyer — both identities are flagged uncertain (→ review)."""
    from app.modules.expense.text_layer import _assign_gstins, _Gstin, _Line

    billto = _Line(text="Bill To:", x0=50, x1=90, top=100, bottom=110, page=1)
    buyer = _Gstin(value="27ABCDE1234F1Z0", page=1, x0=50, top=120, x1=200, bottom=130)
    supplier = _Gstin(value="27AAPFU0939F1ZV", page=1, x0=50, top=200, x1=200, bottom=210)
    _sup, _buy, supplier_ambiguous, buyer_ambiguous = _assign_gstins([buyer, supplier], billto)
    assert supplier_ambiguous is True
    assert buyer_ambiguous is True


def test_assign_gstins_ordinary_supplier_first_is_confident() -> None:
    """The normal masthead (supplier GSTIN at top, Bill To + buyer below) stays confident."""
    from app.modules.expense.text_layer import _assign_gstins, _Gstin, _Line

    supplier = _Gstin(value="27AAPFU0939F1ZV", page=1, x0=50, top=60, x1=200, bottom=70)
    billto = _Line(text="Bill To:", x0=50, x1=90, top=150, bottom=160, page=1)
    buyer = _Gstin(value="27ABCDE1234F1Z0", page=1, x0=50, top=180, x1=200, bottom=190)
    sup, buy, supplier_ambiguous, buyer_ambiguous = _assign_gstins([supplier, buyer], billto)
    assert sup is supplier and buy is buyer
    assert supplier_ambiguous is False and buyer_ambiguous is False


# =========================================================================== TALLY-STYLE
# A real Tally "Tax Invoice" that the Tally router does NOT recognize (its banner uses "Qty"
# not "Quantity" and has no "Total" word) falls through to this generic engine. It carries a
# bare "#" invoice-number label, SGST/CGST/IGST columns that hold tax RATES (0/0/18%) not
# amounts, a DOUBLED trailing "Amount" = the inclusive line total, and only a bare "TOTAL:"
# line. The generic engine must still resolve every required field. Each test below FAILS on
# the pre-fix extractor.


def _hash_rate_pdf() -> bytes:
    spec = HashRateInvoiceSpec(
        supplier_name="Northwind Traders Private Limited",
        supplier_gstin=GSTIN_SUPPLIER_GJ,
        supplier_address="Plot 22 GIDC, Vapi, Gujarat 396195",
        buyer_name="Southern Retail Pvt Ltd",
        buyer_gstin=GSTIN_BUYER_KA,
        buyer_address="45 MG Road, Bengaluru, Karnataka 560001",
        invoice_number="NW/2026/0451",
        invoice_date=date(2026, 3, 30),
        place_of_supply="Karnataka (29)",
        po_ref="PO-45021",
        lines=[
            HashRateLine("Ceramic heater 2kW", "85162900", Decimal("42"), 219500, Decimal("18")),
            HashRateLine("Freight and handling", "996511", Decimal("1"), 695000, Decimal("18")),
        ],
    )
    pdf, _ = build_hash_rate_invoice_pdf(spec)
    return pdf


def test_tally_hash_rate_end_to_end_is_extracted() -> None:
    """Bare '#' number + rate-only tax columns + a doubled 'Amount' + a bare 'TOTAL:' line:
    every required field resolves, both totals are DERIVED from the lines, the arithmetic
    cross-checks corroborate them, and the doc is EXTRACTED (no review)."""
    result = EX.extract(_hash_rate_pdf())

    h = result.header
    assert h.invoice_number.value_normalized == "NW/2026/0451"     # bare-"#" label read
    assert h.invoice_number.status is FieldStatus.OK
    assert h.supplier_gstin.value_normalized == GSTIN_SUPPLIER_GJ
    assert h.buyer_gstin.value_normalized == GSTIN_BUYER_KA

    t = result.totals
    assert t.total_taxable_paise.value_normalized == 9_914_000     # Σ line taxable, DERIVED
    assert t.total_taxable_paise.status is FieldStatus.OK
    assert t.grand_total_paise.value_normalized == 11_698_520      # Σ line total, DERIVED
    assert t.grand_total_paise.status is FieldStatus.OK

    assert len(result.lines) == 2
    l1 = result.lines[0]
    assert l1.taxable_paise.value_normalized == 9_219_000
    assert l1.gst_rate.value_normalized is not None and int(l1.gst_rate.value_normalized) == 18
    assert l1.line_total_paise.value_normalized == 10_878_420      # trailing "Amount" = total
    # rate columns are RATES, not amounts → the tax-AMOUNT fields stay MISSING (never 0-guessed)
    assert l1.cgst_paise.status is FieldStatus.MISSING
    assert l1.sgst_paise.status is FieldStatus.MISSING
    assert l1.igst_paise.status is FieldStatus.MISSING

    a = result.arithmetic
    assert a.lines_sum_matches_taxable is True
    assert a.per_line_tax_consistent is True     # via implied tax = line_total − taxable
    assert a.totals_add_to_grand is True         # via Σ line_total == grand
    assert a.supply_type_consistent is True

    assert result.review_needed is False
    assert result.review_reasons == []


def test_parse_tables_finds_header_below_a_masthead_row() -> None:
    """The item header is NOT at row 0: a single ruled table carries a masthead + meta block
    ABOVE the item grid (real Tally geometry). The header must be found by scan, and the
    rate-only tax columns + the doubled trailing 'Amount' resolved."""
    tables = [[
        ["ACME WIDGETS LIMITED  GST: 27AAPFU0939F1ZV", "", "", "", "", "", "", "", "", ""],
        ["# : ACM/2026/0007", "", "Place of Supply Karnataka (29)", "", "", "", "", "", "", ""],
        ["Bill To", "", "Ship To", "", "", "", "", "", "", ""],
        ["#", "Item & Description", "HSN/SAC", "Rate", "Qty", "Taxable Amount",
         "SGST", "CGST", "IGST", "Amount"],
        ["1", "Widget", "8471", "100.00", "2", "200.00", "0", "0", "18%", "236.00"],
        ["", "", "TOTAL:", "", "", "", "", "", "", "236.00"],
    ]]
    rows = _parse_tables(tables)
    assert len(rows) == 1                        # only the item row (masthead + TOTAL skipped)
    r = rows[0]
    assert r.description == "Widget"
    assert r.hsn == "8471"
    assert r.taxable == 20_000                               # "Taxable Amount" → taxable
    assert r.line_total == 23_600                            # trailing "Amount" → line total
    assert r.gst_rate is not None and int(r.gst_rate) == 18  # IGST column carried the RATE
    assert r.cgst is None and r.sgst is None and r.igst is None   # rate columns, no amounts


def test_map_headers_doubled_amount_maps_second_to_line_total() -> None:
    """A more-specific 'Taxable Amount' claims taxable; the SECOND, trailing bare 'Amount'
    column maps to the inclusive line total (not left unmapped, not stealing taxable)."""
    header = ["#", "Item & Description", "HSN/SAC", "Rate", "Qty", "Taxable Amount",
              "SGST", "CGST", "IGST", "Amount"]
    mapping = _map_headers(header)
    assert mapping["taxable_paise"] == 5
    assert mapping["line_total_paise"] == 9


def test_map_headers_lone_amount_still_maps_to_taxable() -> None:
    """A SINGLE lone 'Amount' column (no more-specific taxable header) still resolves to the
    taxable column — the doubled-amount fallback must not hijack it to line_total."""
    header = ["Description", "HSN/SAC", "Qty", "Rate", "Amount"]
    mapping = _map_headers(header)
    assert mapping["taxable_paise"] == 4
    assert "line_total_paise" not in mapping


def test_bare_hash_invoice_regex_guards_banner_and_lone_hash() -> None:
    """The bare-'#' invoice-number label must lead its visual line and be followed by a COLON +
    a digit-bearing value, so it reads the meta line but NEVER an address/PO/phone hash, the
    item-table banner, or a lone '#'. (Regression guard — audit Finding 1: a whitespace-anchored
    '#' silently stole 'Shop #-12' / 'PO # : 4500123' / 'Contact # : 98…' as the invoice no.)"""
    from app.modules.expense.text_layer import _INV_NO_HASH_RE

    m = _INV_NO_HASH_RE.search("# : TI2025/10175 Place of Supply Madhya Pradesh (23)")
    assert m is not None and m.group(1) == "TI2025/10175"
    # NONE of these lead with "#:" — an address, a PO number, a phone, a door number, the item
    # banner (no colon), and a lone "#" must all fail to match.
    for text in (
        "Shop #-12, Industrial Estate, Pune",
        "PO # : 4500123",
        "Contact # : 9830011252",
        "Door #: 45 Brigade Road",
        "# Item & Description HSN/SAC Rate Qty Amount",
        "#  ",
    ):
        assert _INV_NO_HASH_RE.search(text) is None, text


def _raw_line(taxable: int | None, *, line_total: int | None = None,
              gst_rate: int | None = 18, cgst: int | None = None,
              sgst: int | None = None, igst: int | None = None) -> object:
    """A minimal `_RawLine` for the pure-function arithmetic tests."""
    from app.modules.expense.text_layer import _RawLine

    return _RawLine(
        raw={}, description="Item", hsn="8471", quantity=Decimal("1"), unit="NOS",
        unit_rate=None, taxable=taxable,
        gst_rate=(Decimal(gst_rate) if gst_rate is not None else None),
        cgst=cgst, sgst=sgst, igst=igst, line_total=line_total)


def test_derived_taxable_stays_missing_when_a_line_taxable_is_unparsed() -> None:
    """DERIVING the taxable total from the line items must require EVERY line's taxable to have
    parsed — one unparsed line would silently understate the total and the Σ-lines check would
    trivially pass. A partial parse stays MISSING → review, never a clean-looking wrong number.
    (Regression guard — audit Finding 2.)"""
    # Line 2's taxable is a European-format cell ("1.234,50") → `_money_to_paise` returns None.
    tables = [[
        ["Description", "HSN/SAC", "Qty", "Rate", "Taxable"],
        ["Widget A", "8471", "2", "100.00", "200.00"],
        ["Widget B", "8471", "3", "400.00", "1.234,50"],
    ]]
    rows = _parse_tables(tables)
    assert len(rows) == 2 and rows[1].taxable is None  # the bad cell did not parse
    # No total label anywhere (empty words), so the total would be DERIVED — and must NOT be,
    # because a line taxable is missing.
    result = EX._build(1, "Widget A\nWidget B", [], tables)
    assert result.totals.total_taxable_paise.status is FieldStatus.MISSING
    assert result.totals.total_taxable_paise.value_normalized is None


def test_grand_total_check_not_masked_when_tax_heads_are_printed() -> None:
    """When the tax-head totals ARE printed, a wrong one (e.g. a 10x CGST typo) must fail the
    totals-add-to-grand check — the Σ-line-total corroboration only applies when NO tax heads
    are printed (a rate-only layout). (Regression guard — audit Finding 3.)"""
    from app.modules.expense.text_layer import _run_arithmetic

    # One line: taxable 20000, inclusive line total 23600 (18% GST). Grand total 23600 is right.
    line = _raw_line(20000, line_total=23600, cgst=1800, sgst=1800)
    # WRONG printed CGST total (90000 instead of 1800); SGST correct.
    checks, _ = _run_arithmetic(
        [line], 20000, 90000, 1800, None, None, 23600,
        pos_code="29", supplier_code="29", buyer_code="29")
    assert checks.totals_add_to_grand is False  # the 10x CGST is NOT hidden by Σ line_total

    # Control: with NO tax-head totals printed (rate-only), Σ line_total == grand corroborates.
    checks2, _ = _run_arithmetic(
        [line], 20000, None, None, None, None, 23600,
        pos_code="29", supplier_code="29", buyer_code="29")
    assert checks2.totals_add_to_grand is True


def test_header_scan_ignores_a_prose_row_without_an_item_anchor() -> None:
    """The below-row-0 header scan must demand a real item-table anchor (HSN or Qty) — a prose
    notes/terms row that coincidentally maps three generic money synonyms must NOT be taken as
    the header (which would shift every column). (Regression guard — audit Finding 4.)"""
    tables = [[
        ["NORTHWIND TRADERS  GST: 27AAPFU0939F1ZV", "", "", "", "", ""],
        # Decoy prose row: maps rate/description/total but carries NO hsn/qty column.
        ["Rate contract", "Service order", "Terms", "Total due on delivery", "", ""],
        # The REAL item header (has HSN + Qty).
        ["Description", "HSN/SAC", "Qty", "Rate", "Taxable", "Amount"],
        ["Widget", "8471", "2", "100.00", "200.00", "236.00"],
    ]]
    rows = _parse_tables(tables)
    assert len(rows) == 1
    r = rows[0]
    # Columns resolved off the REAL header, not the decoy: a proper HSN + taxable + line total.
    assert r.hsn == "8471"
    assert r.taxable == 20_000
    assert r.line_total == 23_600
