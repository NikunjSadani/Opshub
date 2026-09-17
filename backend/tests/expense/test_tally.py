"""Tests for the Tally 'Tax Invoice' extractor + the Tally-aware composite.

Two fixture streams:

* a GOLDEN test against the REAL ``Sales_TI_2026_1193.pdf`` — path/env guarded so it SKIPS
  when the file is absent (CI stays green; the file carries real bank/GSTIN data and is NEVER
  committed). Run it locally by placing the PDF at the default path below or pointing
  ``OPSHUB_TALLY_GOLDEN_PDF`` at it.
* SYNTHETIC fixtures rendered in-process by ``tally_synth`` (reportlab, TEST-ONLY) — each
  reproduces a shape the handoff parser dropped and the new engine must handle, with money
  asserted in EXACT integer paise. Every audit defect below maps to a named test.

Audit-defect → test (one named test per defect, listed against its test function below):
  MONEY (verbatim Decimal→paise; real 0.00 stays 0)  test_money_verbatim_paise,
      test_zero_amount_stays_zero_not_none
  FRACTIONAL GST (2.5% / 1.5% / 0.125% / 9%)  test_five_percent_line,
      test_fractional_and_three_digit_hsn, test_fractional_rate_helpers
  WRAPPED multi-line descriptions  test_wrapped_description_not_dropped
  NO-DECIMAL amounts + 3-DIGIT HSN/SAC  test_fractional_and_three_digit_hsn
  IGST / inter-state (grand_total + tax not None)  test_igst_interstate
  MULTI-PAGE (iterate all pages)  test_multi_page_items
  ReDoS (pathological Total line is fast)  test_redos_pathological_total_line_is_fast
  HEADER-META geometry (no adjacent bleed)  golden, test_two_line_items
  VALIDATION→REVIEW (grand mismatch → LOW)  test_grand_total_mismatch_low_confidence_blocks
  printed-taxable ≠ item-sum → review  test_printed_taxable_mismatch_flagged
  per-line tax mismatch → review  test_per_line_tax_mismatch_flagged
  detection + unchanged fallback  test_detection_*, test_fallback_*, test_needs_ocr_*
"""
from __future__ import annotations

import os
import pathlib
import time
from datetime import date
from decimal import Decimal

import pytest

from app.modules.expense.canonical import FieldStatus
from app.modules.expense.extractor import get_extractor
from app.modules.expense.tally import (
    NAME,
    TallyAwareExtractor,
    TallyInvoiceExtractor,
    _clean_runs,
    _Col,
    _Grid,
    _is_dispatch_note,
    _is_item_start,
    _is_tally_tax_invoice,
    _last_money,
    _num_or_none,
    _parse_item_row,
    _peel_lead_sl,
    _proportional_split,
    _split_glued_tail,
    _Word,
)
from app.modules.expense.text_layer import (
    _GRAND_TOTAL_RE,
    _TOTAL_CGST_RE,
    _Line,
    _money_to_paise,
)
from tests.expense.tally_synth import (
    GSTIN_BUYER_WB,
    GSTIN_BUYER_WB2,
    GSTIN_SUPPLIER_KA,
    GSTIN_SUPPLIER_WB,
    TGluedInvoice,
    TInvoice,
    TLine,
    build_tally_glued_igst_pdf,
    build_tally_pdf,
)

EX = TallyAwareExtractor()


def _extract(inv: TInvoice):  # type: ignore[no-untyped-def]
    pdf, gold = build_tally_pdf(inv)
    return EX.extract(pdf), gold


def _intra(**kw) -> TInvoice:  # type: ignore[no-untyped-def]
    base = dict(
        supplier_name="Acme WB Traders Pvt Ltd", supplier_gstin=GSTIN_SUPPLIER_WB,
        supplier_address="12 Park Street, Kolkata", buyer_name="Northstar Traders LLP",
        buyer_gstin=GSTIN_BUYER_WB, buyer_address="9 Salt Lake, Kolkata",
        invoice_number="TI/2026/2001", invoice_date=date(2026, 8, 27),
        place_of_supply="West Bengal", intra=True,
        lines=[TLine("Widget Assembly", "84713010", Decimal("2"), "PCS", 4690700, Decimal("18"))],
    )
    base.update(kw)
    return TInvoice(**base)  # type: ignore[arg-type]


# =========================================================================== GOLDEN (guarded)

_GOLDEN_PATH = pathlib.Path(
    os.environ.get("OPSHUB_TALLY_GOLDEN_PDF", r"C:\Users\nikun\Downloads\Sales_TI_2026_1193.pdf")
)


@pytest.mark.skipif(not _GOLDEN_PATH.is_file(),
                    reason=f"golden Tally PDF absent at {_GOLDEN_PATH} (real-data, not committed)")
def test_golden_real_tally_invoice_exact_paise() -> None:
    """The real Tech Gifsy → Britannia invoice (TI/2026/1193) → exact canonical paise, no review."""
    result = EX.extract(_GOLDEN_PATH.read_bytes())

    assert result.source_engine == NAME
    assert result.needs_ocr is False
    assert result.page_count == 1

    h = result.header
    assert h.invoice_number.value_normalized == "TI/2026/1193"
    assert h.invoice_number.status is FieldStatus.OK
    assert h.invoice_date.value_normalized == date(2026, 8, 27)
    assert h.supplier_gstin.value_normalized == "19AAACT9811F1Z9"
    assert h.buyer_gstin.value_normalized == "19AABCB2066P1ZC"
    assert h.buyer_gstin.status is FieldStatus.OK
    assert h.place_of_supply.value_normalized == "West Bengal (19)"

    t = result.totals
    assert t.total_taxable_paise.value_normalized == 9_381_400
    assert t.total_cgst_paise.value_normalized == 844_326
    assert t.total_sgst_paise.value_normalized == 844_326
    assert t.round_off_paise.value_normalized == 48
    assert t.grand_total_paise.value_normalized == 11_070_100
    tax_total = ((t.total_cgst_paise.value_normalized or 0)
                 + (t.total_sgst_paise.value_normalized or 0)
                 + (t.total_igst_paise.value_normalized or 0))
    assert tax_total == 1_688_652

    assert len(result.lines) == 1
    ln = result.lines[0]
    assert ln.hsn_sac.value_normalized == "84713010"
    assert ln.quantity.value_normalized == Decimal("2")
    assert ln.unit_rate_paise.value_normalized == 4_690_700
    assert ln.taxable_paise.value_normalized == 9_381_400
    assert ln.cgst_paise.value_normalized == 844_326
    assert ln.sgst_paise.value_normalized == 844_326
    assert ln.line_total_paise.value_normalized == 11_070_052

    a = result.arithmetic
    assert a.lines_sum_matches_taxable and a.per_line_tax_consistent
    assert a.totals_add_to_grand and a.supply_type_consistent
    assert a.max_abs_delta_paise == 0
    assert result.review_needed is False
    assert result.review_reasons == []


# =========================================================================== detection + wiring


def test_detection_positive_on_tally_text() -> None:
    pdf, _ = build_tally_pdf(_intra())
    r = EX.extract(pdf)
    assert _is_tally_tax_invoice(r.raw_text) is True
    assert r.source_engine == NAME


def test_detection_negative_needs_two_signals() -> None:
    # Only the title — one signal — must NOT be classified as Tally.
    assert _is_tally_tax_invoice("Tax Invoice\nsome ordinary body text\nGrand Total: 100") is False
    # Title + banner = two signals → Tally.
    banner = ("Sl Description of Goods HSN/SAC Quantity Rate per Amount "
              "Taxable CGST SGST/UTGST Total")
    assert _is_tally_tax_invoice(f"Tax Invoice\n{banner}\n1 Foo 8471 1 100.00") is True


def test_detection_generic_tax_invoice_with_anchor_phrase_not_tally() -> None:
    """A GENERIC (non-Tally) GST invoice titled 'Tax Invoice' that also prints the common
    anchor phrase 'Amount Chargeable (in words)' — but has NO Tally split-tax column banner and
    NO positional 'Invoice No.'/'Dated' header-meta grid — must NOT be routed to the Tally
    engine. The old ≥2-of-{title, phrase, banner} gate false-positived on exactly this shape
    (title + phrase = 2) and mangled ordinary invoices; the tightened structural gate rejects
    it, so the composite delegates to the safe text-layer engine (`_is_tally_tax_invoice` False
    → the not-Tally branch → `TextLayerExtractor`)."""
    text = (
        "Tax Invoice\n"
        "Some Vendor Pvt Ltd\n"
        "Invoice Number: INV-1    Invoice Date: 01-01-2026\n"
        "Description Qty Rate Amount\n"
        "1 Widget 2 100.00 200.00\n"
        "Amount Chargeable (in words) INR Two Hundred Only\n"
        "This is a Computer Generated Invoice\n"
        "Grand Total: 200.00\n")
    assert _is_tally_tax_invoice(text) is False


def test_get_extractor_default_is_tally_aware() -> None:
    assert isinstance(get_extractor(), TallyAwareExtractor)
    assert isinstance(get_extractor("auto"), TallyAwareExtractor)
    # The generic engine is still reachable by name (the eval gold harness uses it).
    from app.modules.expense.text_layer import TextLayerExtractor
    assert isinstance(get_extractor("text_layer"), TextLayerExtractor)


def test_fallback_non_tally_delegates_unchanged() -> None:
    """A non-Tally GST invoice (eval synth) is delegated to the text-layer engine untouched."""
    from app.modules.expense.eval.synth import (
        GSTIN_BUYER_MH,
        GSTIN_SUPPLIER_MH,
        InvoiceSpec,
        LineSpec,
        build_invoice_pdf,
    )
    spec = InvoiceSpec(
        supplier_name="Umang Traders", supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd", buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai 400069", invoice_number="UMG/2026/0099",
        invoice_date=date(2026, 5, 20), place_of_supply="Maharashtra (27)", intra_state=True,
        lines=[LineSpec("Office chair", "9401", Decimal("4"), "NOS", 450000, Decimal("18"))])
    pdf, _ = build_invoice_pdf(spec)
    r = EX.extract(pdf)
    assert r.source_engine == "text_layer/1.0"
    assert r.header.invoice_number.value_normalized == "UMG/2026/0099"
    assert r.totals.grand_total_paise.value_normalized == 2_124_000


def test_needs_ocr_blank_pdf_preserved() -> None:
    import io as _io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFillColorRGB(0.9, 0.9, 0.9)
    c.rect(72, 72, 400, 600, fill=1, stroke=0)
    c.showPage()
    c.save()
    r = EX.extract(buf.getvalue())
    assert r.needs_ocr is True
    assert r.review_needed is True


# =========================================================================== happy paths


def test_happy_intra_exact_paise_no_review() -> None:
    result, gold = _extract(_intra(round_off_paise=48))
    t = result.totals
    assert t.total_taxable_paise.value_normalized == 9_381_400
    assert t.total_cgst_paise.value_normalized == 844_326
    assert t.total_sgst_paise.value_normalized == 844_326
    assert t.round_off_paise.value_normalized == 48
    assert t.grand_total_paise.value_normalized == 11_070_100
    assert result.source_engine == NAME
    # per-field provenance is restamped to this engine (the reused builders default to another).
    assert t.grand_total_paise.source_engine == NAME
    assert result.lines[0].taxable_paise.source_engine == NAME
    assert result.review_needed is False and result.review_reasons == []


def test_igst_interstate() -> None:
    """Inter-state IGST: the IGST line AND the grand total + tax must parse (not None)."""
    result, gold = _extract(TInvoice(
        supplier_name="KA Supplier Ltd", supplier_gstin=GSTIN_SUPPLIER_KA,
        supplier_address="MG Road, Bengaluru", buyer_name="WB Buyer LLP",
        buyer_gstin=GSTIN_BUYER_WB2, buyer_address="Park Street, Kolkata",
        invoice_number="TI/2026/3001", invoice_date=date(2026, 8, 20),
        place_of_supply="West Bengal", intra=False,
        lines=[TLine("Server Rack", "84713010", Decimal("2"), "NOS", 500000, Decimal("18"))]))
    ln = result.lines[0]
    assert ln.igst_paise.value_normalized == 180_000
    assert ln.cgst_paise.status is FieldStatus.MISSING     # no CGST column → not zero-guessed
    t = result.totals
    assert t.total_igst_paise.value_normalized == 180_000
    assert t.grand_total_paise.value_normalized == 1_180_000
    assert result.arithmetic.supply_type_consistent is True
    assert result.review_needed is False


def test_five_percent_line() -> None:
    """A 5% line (2.5% CGST + 2.5% SGST) — the handoff's whole-% regex dropped it."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3002",
        lines=[TLine("Food item", "21069099", Decimal("10"), "PCS", 20000, Decimal("5"))]))
    ln = result.lines[0]
    assert ln.taxable_paise.value_normalized == 200_000
    assert ln.gst_rate.value_normalized == Decimal("5")
    assert ln.cgst_paise.value_normalized == 5_000
    assert ln.sgst_paise.value_normalized == 5_000
    assert result.totals.grand_total_paise.value_normalized == 210_000
    assert result.review_needed is False


def test_fractional_and_three_digit_hsn() -> None:
    """A fractional 1.5%+1.5% rate, a 3-digit SAC ('998'), and NO-DECIMAL whole-rupee amounts."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3004", whole_rupees=True,
        lines=[TLine("Consulting service", "998", Decimal("3"), "NOS", 100000, Decimal("3"))]))
    assert len(result.lines) == 1
    ln = result.lines[0]
    assert ln.hsn_sac.value_normalized == "998"           # 3-digit HSN/SAC kept, not dropped
    assert ln.taxable_paise.value_normalized == 300_000    # no-decimal "3,000" parsed
    assert ln.cgst_paise.value_normalized == 4_500
    assert ln.sgst_paise.value_normalized == 4_500
    assert result.totals.grand_total_paise.value_normalized == 309_000
    assert result.review_needed is False


def test_wrapped_description_not_dropped() -> None:
    """A description wrapped onto a 2nd physical line must be stitched, not drop the item."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3003",
        lines=[
            TLine("LAPTOP HP", "84713010", Decimal("2"), "PCS", 4690700, Decimal("18"),
                  wrap="ATHLON 8GB 512 SSD DOS"),
            TLine("Mouse", "84716060", Decimal("5"), "PCS", 50000, Decimal("18"))]))
    assert len(result.lines) == 2
    assert result.lines[0].description.value_normalized == "LAPTOP HP ATHLON 8GB 512 SSD DOS"
    assert result.lines[0].taxable_paise.value_normalized == 9_381_400
    assert result.lines[1].taxable_paise.value_normalized == 250_000
    assert result.arithmetic.lines_sum_matches_taxable is True
    assert result.review_needed is False


def test_dispatch_note_continuation_dropped_not_stitched() -> None:
    """A 'DESPATCHED BY BRAND DIRECTLY' courier annotation (real Bajaj phrasing) printed as a
    text-only wrapped row UNDER the product line is a dispatch note, NOT a description
    continuation — it must be DROPPED from the extracted description (numbers untouched), so the
    line reads just the product text rather than 'Lloyd 1.5T AC DESPATCHED BY BRAND DIRECTLY'."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3010",
        lines=[
            TLine("Lloyd 1.5T AC", "84151010", Decimal("2"), "PCS", 4690700, Decimal("18"),
                  wrap="DESPATCHED BY BRAND DIRECTLY"),
            TLine("Mouse", "84716060", Decimal("5"), "PCS", 50000, Decimal("18"))]))
    assert len(result.lines) == 2
    assert result.lines[0].description.value_normalized == "Lloyd 1.5T AC"   # dispatch note gone
    assert result.lines[0].taxable_paise.value_normalized == 9_381_400        # numbers unchanged
    assert result.lines[1].taxable_paise.value_normalized == 250_000
    assert result.arithmetic.lines_sum_matches_taxable is True
    assert result.review_needed is False


def test_real_description_continuation_still_stitched_not_over_trimmed() -> None:
    """GUARD against over-trimming: a LEGITIMATE wrapped description fragment ('with stabilizer')
    is NOT a dispatch note and must still be stitched onto the line — the narrow dispatch matcher
    must never swallow a real continuation."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3011",
        lines=[TLine("Split AC", "84151010", Decimal("2"), "PCS", 4690700, Decimal("18"),
                     wrap="with stabilizer")]))
    assert len(result.lines) == 1
    assert result.lines[0].description.value_normalized == "Split AC with stabilizer"
    assert result.lines[0].taxable_paise.value_normalized == 9_381_400
    assert result.review_needed is False


def test_is_dispatch_note_matcher() -> None:
    """The narrow matcher: a note BEGINS with the despatch/dispatch verb AND carries a note
    keyword (by/through/directly). A real description fragment — even one that merely wraps
    onto a word like 'Dispatch Console' — is NOT dropped."""
    assert _is_dispatch_note("DESPATCHED BY BRAND DIRECTLY") is True
    assert _is_dispatch_note("Dispatched by brand directly") is True
    assert _is_dispatch_note("  Despatch through courier  ") is True
    # Not dispatch notes — a real continuation must survive.
    assert _is_dispatch_note("with stabilizer") is False
    assert _is_dispatch_note("ATHLON 8GB 512 SSD DOS") is False
    assert _is_dispatch_note("ready for dispatch soon") is False   # verb not at the start
    assert _is_dispatch_note("dispatcher unit model X") is False   # \b guards the word boundary
    # A real product description that wraps onto a 'Dispatch…' word must NOT be dropped —
    # it lacks a dispatch-note keyword (the tightened matcher's over-trim guard).
    assert _is_dispatch_note("Dispatch Console Pro") is False
    assert _is_dispatch_note("Despatch Tracker Model X") is False


def test_two_line_items() -> None:
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3007",
        lines=[
            TLine("Server Rack", "84713010", Decimal("2"), "NOS", 500000, Decimal("18")),
            TLine("Cabinet Shelf", "9403", Decimal("3"), "NOS", 100000, Decimal("12"))]))
    assert [ln.hsn_sac.value_normalized for ln in result.lines] == ["84713010", "9403"]
    assert [ln.taxable_paise.value_normalized for ln in result.lines] == [1_000_000, 300_000]
    assert result.totals.total_taxable_paise.value_normalized == 1_300_000
    assert result.review_needed is False


def test_multi_page_items() -> None:
    """Four line items across two pages WITH an intermediate per-page 'Total' SUBTOTAL at the
    foot of page 1 (the carried-forward running total a real multi-page Tally invoice prints).

    This is the exact money-halving hazard the old parser missed: it returned on the FIRST
    'Total' row, so it kept only the two page-1 items and read the page-1 subtotal (₹2,000) as
    the grand total — a self-consistent but HALVED invoice that passed every arithmetic check
    review-clean. All four items must be captured across both pages and the DOCUMENT grand /
    taxable stored, NEVER the halved page-1 subtotal, and the correct invoice must reconcile
    clean (an intermediate subtotal is not itself a review trigger when the sum reconciles)."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/4001", page_break_after=2, intermediate_subtotal=True,
        lines=[TLine(f"Item {i}", "8471", Decimal("1"), "NOS", 100000, Decimal("18"))
               for i in range(4)]))
    assert result.page_count == 2
    assert len(result.lines) == 4
    assert [ln.taxable_paise.value_normalized for ln in result.lines] == [100_000] * 4
    t = result.totals
    assert t.total_taxable_paise.value_normalized == 400_000     # full doc taxable, not 200k
    assert t.grand_total_paise.value_normalized == 472_000       # full grand, not the halved 200k
    assert t.grand_total_paise.value_normalized != 200_000       # never the page-1 subtotal
    assert t.grand_total_paise.status is FieldStatus.OK          # stored clean, not flagged-wrong
    assert result.review_needed is False and result.review_reasons == []


def test_multi_page_unreconciled_subtotal_blocks() -> None:
    """SAFETY NET: a multi-page invoice with a per-page subtotal whose all-page line sum does
    NOT reconcile to the printed DOCUMENT taxable → BOTH required totals LOW_CONFIDENCE + a
    review reason (→ NEEDS_REVIEW, confirm blocked). Never a silently-stored halved/wrong total.
    """
    result, gold = _extract(_intra(
        invoice_number="TI/2026/4002", page_break_after=2, intermediate_subtotal=True,
        total_taxable_override_paise=999_999,
        lines=[TLine(f"Item {i}", "8471", Decimal("1"), "NOS", 100000, Decimal("18"))
               for i in range(4)]))
    assert len(result.lines) == 4                                 # items still read whole
    t = result.totals
    assert t.total_taxable_paise.status is FieldStatus.LOW_CONFIDENCE
    assert t.grand_total_paise.status is FieldStatus.LOW_CONFIDENCE
    assert result.review_needed is True
    assert any("could not be reconciled" in r for r in result.review_reasons)


# =========================================================================== review wiring


def test_grand_total_mismatch_low_confidence_blocks() -> None:
    """A printed grand total that disagrees with the recompute → grand_total LOW_CONFIDENCE
    (a REQUIRED field → NEEDS_REVIEW → confirm blocked), never a silently-stored wrong number."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3005", grand_total_override_paise=9_999_999))
    gt = result.totals.grand_total_paise
    assert gt.value_normalized == 9_999_999            # captured verbatim
    assert gt.status is FieldStatus.LOW_CONFIDENCE      # but flagged, not trusted
    assert gt.confidence == 0.50
    assert result.arithmetic.totals_add_to_grand is False
    assert result.review_needed is True
    assert any("grand total" in r for r in result.review_reasons)


def test_printed_taxable_mismatch_flagged() -> None:
    """Printed Total-row taxable ≠ Σ line items → total_taxable LOW_CONFIDENCE + review; the
    PRINTED value is preferred for storage (then cross-checked)."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3008", total_taxable_override_paise=999_999))
    tt = result.totals.total_taxable_paise
    assert tt.value_normalized == 999_999               # printed Total-row value preferred
    assert tt.status is FieldStatus.LOW_CONFIDENCE
    assert result.review_needed is True
    assert any("taxable" in r for r in result.review_reasons)


def test_per_line_tax_mismatch_flagged() -> None:
    """A per-line tax that doesn't match taxable × rate (beyond the ₹1 tolerance) → the tax
    totals drop to LOW_CONFIDENCE + a review reason."""
    result, gold = _extract(_intra(
        invoice_number="TI/2026/3009",
        lines=[TLine("Y", "8471", Decimal("2"), "PCS", 500000, Decimal("18"),
                     tax_delta_paise=500)]))          # +₹5, beyond the ₹1 line-tax tolerance
    assert result.arithmetic.per_line_tax_consistent is False
    assert result.totals.total_cgst_paise.status is FieldStatus.LOW_CONFIDENCE
    assert result.review_needed is True


# =========================================================================== money discipline


def test_money_verbatim_paise() -> None:
    assert _money_to_paise("1,10,701.00") == 11_070_100   # Indian grouping
    assert _money_to_paise("93,814.00") == 9_381_400      # Western grouping
    assert _money_to_paise("500") == 50_000               # NO-decimal amount
    assert _money_to_paise("8,443.26") == 844_326
    assert _money_to_paise("(0.30)") == -30               # accounting negative


def test_zero_amount_stays_zero_not_none() -> None:
    """A real 0.00 must parse to integer 0 — never None (the handoff's ``sum(...) or None``
    idiom would have turned a genuine zero into a missing value)."""
    assert _money_to_paise("0.00") == 0
    assert _money_to_paise("0.00") is not None


def test_fractional_rate_helpers() -> None:
    """Fractional GST rates parse to exact Decimals (the handoff regex accepted only whole %)."""
    assert _num_or_none("2.5%") == Decimal("2.5")
    assert _num_or_none("0.125%") == Decimal("0.125")
    assert _num_or_none("9%") == Decimal("9")


# =========================================================================== ReDoS


def test_redos_pathological_total_line_is_fast() -> None:
    """A pathological long 'Total …' line (the shape that made the handoff's lazy multi-money
    grand-total regex backtrack O(n²)) must be read in well under 100ms.

    The Tally engine takes grand totals from WORD GEOMETRY, not that regex; the label regexes it
    does run are linear and every line is length-capped before the regex sees it.
    """
    hostile = "Total " + "1,234.56 " * 40_000 + "and no final anchor"
    line = _Line(text=hostile, x0=0.0, x1=1.0, top=0.0, bottom=1.0, page=1)

    start = time.perf_counter()
    for _ in range(5):
        assert _last_money([line], _TOTAL_CGST_RE) is None      # no CGST label → no match, fast
        # Even the handoff's vulnerable grand-total pattern is bounded by the length cap.
        _last_money([line], _GRAND_TOTAL_RE)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.1, f"totals parsing was slow ({elapsed:.3f}s) — possible ReDoS"


# =========================================================================== domain errors


def test_non_pdf_raises_clean_domain_error() -> None:
    from app.modules.expense.text_layer import InvoiceExtractionError
    with pytest.raises(InvoiceExtractionError):
        EX.extract(b"definitely not a pdf")


def test_unsupported_doc_type_rejected() -> None:
    pdf, _ = build_tally_pdf(_intra())
    with pytest.raises(ValueError):
        EX.extract(pdf, doc_type="purchase_order")
    with pytest.raises(ValueError):
        TallyInvoiceExtractor().extract(pdf, doc_type="purchase_order")


# =========================================================================== glued-token split
# A tight Tally IGST grid makes pdfplumber GLUE adjacent cells into one word token; the item-row
# token-splitter must fan each glued token back into its own column. These tests pin the
# splitter primitives, then the row-parser on the EXACT real-Bajaj glued geometry, then the
# committed gold fixture end-to-end.


def _w(text: str, x0: float, x1: float) -> _Word:
    return _Word(text=text, x0=x0, x1=x1, top=258.0, bottom=266.0, page=1)


def test_clean_runs_segments_glued_money_percent_uom() -> None:
    assert _clean_runs("PCS32,000.00PCS") == ["PCS", "32,000.00", "PCS"]
    # A 4-decimal glue splits AFTER the paise (money caps at 2 decimals), never swallowing the
    # abutting rate digits: taxable 32,000.00 + IGST-rate 18% + IGST-amount 5,760.00.
    assert _clean_runs("32,000.0018%5,760.00") == ["32,000.00", "18%", "5,760.00"]
    assert _clean_runs("1Lloyd") == ["1", "Lloyd"]
    assert _clean_runs("18%") == ["18%"]                      # a clean single run
    # A SUB-₹1,000 taxable carries no thousands comma, so the percent run must NOT eat its
    # paise: "50.0018%9.00" is taxable 50.00 + rate 18% + IGST 9.00 (regression: an unbounded
    # percent mantissa fused "50.0018%" and dropped the cheap line to review).
    assert _clean_runs("50.0018%9.00") == ["50.00", "18%", "9.00"]
    assert _clean_runs("999.0018%179.82") == ["999.00", "18%", "179.82"]
    assert _clean_runs("2.5%") == ["2.5%"]                    # a fractional rate still parses


def test_clean_runs_rejects_tokens_with_stray_glyphs() -> None:
    # A rupee-symbol artefact, a description fragment, and a one-decimal token do NOT decompose
    # cleanly (a stray '(' / ':' / ')' / '.5') → None → the token is left untouched (never split
    # into a spurious money value that would pollute a column).
    assert _clean_runs("(cid:299)") is None
    assert _clean_runs("2026(6") is None
    assert _clean_runs("1.5") is None
    assert _clean_runs("Ac,") is None


def test_split_glued_tail_idempotent_on_clean_and_junk_tokens() -> None:
    # A clean single money token is never fragmented.
    clean = _w("32,000.00", 649.0, 682.0)
    assert [x.text for x in _split_glued_tail(clean)] == ["32,000.00"]
    # A rupee-symbol artefact (Tally's "(cid:299)" before the bill total) is never fragmented
    # into a spurious "299" money value.
    junk = _w("(cid:299)", 644.0, 649.0)
    assert [x.text for x in _split_glued_tail(junk)] == ["(cid:299)"]


def test_split_glued_tail_proportional_x_boxes() -> None:
    glued = _w("32,000.0018%5,760.00", 688.0, 765.0)
    parts = _split_glued_tail(glued)
    assert [p.text for p in parts] == ["32,000.00", "18%", "5,760.00"]
    # Sub-boxes tile the original [x0, x1] span left-to-right, contiguous, in-bounds.
    assert parts[0].x0 == pytest.approx(688.0)
    assert parts[-1].x1 == pytest.approx(765.0)
    for a, b in zip(parts, parts[1:], strict=False):         # adjacent pairs (len-1 of them)
        assert a.x1 == pytest.approx(b.x0)                    # contiguous, no gaps/overlap
        assert a.x0 < a.x1


def test_proportional_split_divides_by_character_length() -> None:
    parts = _proportional_split(_w("AB1234", 0.0, 60.0), ["AB", "1234"])
    assert [p.text for p in parts] == ["AB", "1234"]
    assert (parts[0].x0, parts[0].x1) == pytest.approx((0.0, 20.0))   # 2 of 6 chars
    assert (parts[1].x0, parts[1].x1) == pytest.approx((20.0, 60.0))  # 4 of 6 chars


def _bajaj_grid() -> _Grid:
    """The column grid of a real Bajaj 'Sales TI' IGST invoice (from its two-row banner)."""
    return _Grid(
        desc_tail_x=431.5,
        tail_cols=[
            _Col("hsn", 547.5), _Col("qty", 581.0), _Col("rate", 612.0), _Col("per", 632.5),
            _Col("amount", 660.0), _Col("taxable", 698.0), _Col("igst_rate", 728.5),
            _Col("igst_amt", 751.0), _Col("total", 784.5),
        ],
        intra=False,
    )


def test_peel_lead_sl_splits_glued_serial_only() -> None:
    grid = _bajaj_grid()
    peeled = _peel_lead_sl([_w("1Lloyd", 45.0, 71.0)], grid)
    assert [p.text for p in peeled] == ["1", "Lloyd"]
    assert peeled[0].text.isdigit() and peeled[0].x0 == pytest.approx(45.0)
    # A NON-glued serial (a bare "1") is returned unchanged.
    assert [p.text for p in _peel_lead_sl([_w("1", 45.0, 49.0)], grid)] == ["1"]


def test_parse_item_row_on_real_bajaj_glued_geometry() -> None:
    """The EXACT glued item row of Sales_TI_2026_1229 (real word boxes) → the canonical line.

    pdfplumber glues the Sl onto the description ("1Lloyd"), the uom+rate+per ("PCS32,000.00PCS")
    and the taxable+IGST-rate+IGST-amount ("32,000.0018%5,760.00"); the splitter must fan each
    back into its own column. Reproduced as raw ``_Word`` tokens so the assertion is exact and
    independent of any renderer (the direct row-parser proof the fix demands).
    """
    grid = _bajaj_grid()
    toks = [
        _w("1Lloyd", 45.0, 71.0), _w("1.5", 73.0, 83.0), _w("Ton", 85.0, 99.0),
        _w("3", 101.0, 105.0), _w("Star", 107.0, 122.0), _w("Inverter", 124.0, 151.0),
        _w("Split", 153.0, 169.0), _w("Ac,", 171.0, 183.0), _w("2026(6", 185.0, 208.0),
        _w("in", 210.0, 217.0), _w("1", 219.0, 223.0), _w("Convertible)", 225.0, 268.0),
        _w("84151010", 540.0, 563.0), _w("1", 580.0, 584.0),
        _w("PCS32,000.00PCS", 586.0, 641.0), _w("32,000.00", 649.0, 682.0),
        _w("32,000.0018%5,760.00", 688.0, 765.0), _w("37,760.00", 771.0, 804.0),
    ]
    peeled = _peel_lead_sl(sorted(toks, key=lambda w: w.x0), grid)
    assert _is_item_start(peeled, grid) is True
    row = _parse_item_row(peeled, grid)
    assert row.description == "Lloyd 1.5 Ton 3 Star Inverter Split Ac, 2026(6 in 1 Convertible)"
    assert row.hsn == "84151010"
    assert row.quantity == Decimal("1")
    assert row.unit == "PCS"
    assert row.unit_rate == 3_200_000       # ₹32,000 from the glued PCS<rate>PCS run
    assert row.taxable == 3_200_000         # ₹32,000 from the glued taxable+rate+amount run
    assert row.gst_rate == Decimal("18")
    assert row.igst == 576_000              # ₹5,760
    assert row.cgst is None and row.sgst is None
    assert row.line_total == 3_776_000      # ₹37,760


def test_parse_item_row_bounds_missplit_rate_to_0_100() -> None:
    """A mis-split value that lands in the rate column but is NOT a plausible 0..100 percent
    (a stray money figure) must never be read as a gst_rate (it would overflow NUMERIC(5,2))."""
    grid = _Grid(desc_tail_x=100.0,
                 tail_cols=[_Col("taxable", 200.0), _Col("igst_rate", 300.0),
                            _Col("igst_amt", 400.0)],
                 intra=False)
    toks = [_w("9", 10.0, 14.0), _w("3,200.00", 195.0, 205.0),
            _w("5,760.00", 295.0, 305.0), _w("576.00", 395.0, 405.0)]
    row = _parse_item_row(toks, grid)
    assert row.taxable == 320_000           # ₹3,200.00
    assert row.igst == 57_600               # ₹576.00
    assert row.gst_rate is None             # 5760 is not a 0..100 rate → dropped, not persisted


def test_glued_igst_synth_extracts_canonical_line_no_review() -> None:
    """The synthetic glued-token IGST invoice extracts the canonical line with NO review."""
    inv = TGluedInvoice(
        supplier_name="Acme Cooling Devices Private Limited", supplier_gstin=GSTIN_SUPPLIER_KA,
        supplier_address="MG Road, Bengaluru", buyer_name="Northstar Retail LLP",
        buyer_gstin=GSTIN_BUYER_WB2, buyer_address="Park Street, Kolkata",
        invoice_number="TI/2026/7788", invoice_date=date(2026, 7, 15),
        place_of_supply="West Bengal", description="Acme Cooler Deluxe Split AC",
        description_first="Acme", hsn="84151010", qty=Decimal("1"), unit="PCS",
        unit_rate_paise=4_000_000, gst_rate=Decimal("18"))
    pdf, gold = build_tally_glued_igst_pdf(inv)
    r = EX.extract(pdf)
    assert r.source_engine == NAME
    assert len(r.lines) == 1
    ln = r.lines[0]
    assert ln.description.value_normalized == "Acme Cooler Deluxe Split AC"
    assert ln.hsn_sac.value_normalized == "84151010"
    assert ln.quantity.value_normalized == Decimal("1")
    assert ln.unit.value_normalized == "PCS"
    assert ln.unit_rate_paise.value_normalized == 4_000_000
    assert ln.taxable_paise.value_normalized == 4_000_000
    assert ln.gst_rate.value_normalized == Decimal("18")
    assert ln.igst_paise.value_normalized == 720_000
    assert ln.cgst_paise.status is FieldStatus.MISSING
    assert ln.line_total_paise.value_normalized == 4_720_000
    t = r.totals
    assert t.total_taxable_paise.value_normalized == 4_000_000
    assert t.total_igst_paise.value_normalized == 720_000
    assert t.grand_total_paise.value_normalized == 4_720_000
    assert r.arithmetic.lines_sum_matches_taxable and r.arithmetic.per_line_tax_consistent
    assert r.arithmetic.totals_add_to_grand and r.arithmetic.supply_type_consistent
    assert r.review_needed is False and r.review_reasons == []


def test_gold_tally_glued_igst_line_exact() -> None:
    """The committed gold fixture (tests/expense/gold/tally_glued_igst) extracts to its
    expected.json line + totals + ids EXACTLY via the Tally engine (a regression guard on the
    glued-token splitter; the text-layer accuracy harness excludes this Tally-only fixture)."""
    import json
    import pathlib

    d = pathlib.Path(__file__).parent / "gold" / "tally_glued_igst"
    gold = json.loads((d / "expected.json").read_text(encoding="utf-8"))
    r = EX.extract((d / "source.pdf").read_bytes())

    assert r.source_engine == NAME
    assert r.review_needed is bool(gold["review_needed"]) is False
    h, gh = r.header, gold["header"]
    assert h.invoice_number.value_normalized == gh["invoice_number"]
    assert h.invoice_date.value_normalized == date.fromisoformat(gh["invoice_date"])
    assert h.supplier_gstin.value_normalized == gh["supplier_gstin"]
    assert h.buyer_gstin.value_normalized == gh["buyer_gstin"]

    assert len(r.lines) == len(gold["lines"]) == 1
    ln, gl = r.lines[0], gold["lines"][0]
    assert ln.description.value_normalized == gl["description"]
    assert ln.hsn_sac.value_normalized == gl["hsn_sac"]
    assert ln.quantity.value_normalized == Decimal(gl["quantity"])
    assert ln.unit.value_normalized == gl["unit"]
    assert ln.unit_rate_paise.value_normalized == gl["unit_rate_paise"]
    assert ln.taxable_paise.value_normalized == gl["taxable_paise"]
    assert ln.gst_rate.value_normalized == Decimal(gl["gst_rate"])
    assert ln.igst_paise.value_normalized == gl["igst_paise"]
    assert ln.line_total_paise.value_normalized == gl["line_total_paise"]

    t, gt = r.totals, gold["totals"]
    assert t.total_taxable_paise.value_normalized == gt["total_taxable_paise"]
    assert t.total_igst_paise.value_normalized == gt["total_igst_paise"]
    assert t.grand_total_paise.value_normalized == gt["grand_total_paise"]
