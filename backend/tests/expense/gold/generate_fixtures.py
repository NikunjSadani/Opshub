"""Regenerate the eval gold fixtures deterministically.

Run from the backend dir::

    .venv/Scripts/python.exe tests/expense/gold/generate_fixtures.py

Writes, for each fixture id, ``<id>/{source.pdf, expected.json, meta.json}``. The PDFs +
expected.json are committed; this script exists so the set is reproducible and extendable.
Because ``synth`` sets ``rl_config.invariant``, re-running produces byte-stable PDFs.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

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

GOLD_DIR = Path(__file__).resolve().parent


def _intra_single_line() -> InvoiceSpec:
    return InvoiceSpec(
        supplier_name="Umang Traders",
        supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd",
        buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="UMG/2026/0042",
        invoice_date=date(2026, 5, 15),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        po_ref="PO-77120",
        lines=[
            LineSpec("Steel almirah 6ft", "9403", Decimal("3"), "NOS", 550000, Decimal("18")),
        ],
        amount_in_words="Nineteen Thousand Four Hundred Seventy Only",
    )


def _inter_multi_line() -> InvoiceSpec:
    return InvoiceSpec(
        supplier_name="Gujarat Poly Pack LLP",
        supplier_gstin=GSTIN_SUPPLIER_GJ,
        supplier_address="Plot 22 GIDC, Vapi, Gujarat 396195",
        buyer_name="Southern Retail Pvt Ltd",
        buyer_gstin=GSTIN_BUYER_KA,
        buyer_address="45 MG Road, Bengaluru, Karnataka 560001",
        invoice_number="GPP-INV-2026-118",
        invoice_date=date(2026, 6, 2),
        place_of_supply="Karnataka (29)",
        intra_state=False,
        lines=[
            LineSpec("PP woven bag 50kg", "6305", Decimal("2"), "BAG", 125000, Decimal("18")),
            LineSpec("PP liner sheet", "6305", Decimal("5"), "NOS", 40000, Decimal("12")),
            LineSpec("HDPE drum 200L", "3923", Decimal("1"), "NOS", 999900, Decimal("28")),
        ],
        round_off_paise=28,  # 14,49,900 taxable + 3,48,972 IGST = 17,98,872 → +0.28 → 17,99,000
        amount_in_words="Seventeen Thousand Nine Hundred Ninety Nine Only",
    )


def _edge_twopage_reorder() -> InvoiceSpec:
    return InvoiceSpec(
        supplier_name="Northline Hardware Co",
        supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="7 Nashik Highway, Pune, Maharashtra 411001",
        buyer_name="Gifsy Solutions Ltd",
        buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="NHC/26-27/0301",
        invoice_date=date(2026, 7, 9),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        # Reordered columns (Qty/HSN pulled ahead of Description) to exercise the extractor's
        # BY-HEADER (never by-index) column mapping over a full GST line grid.
        column_order=("qty", "hsn", "description", "unit", "rate", "taxable", "gst",
                      "cgst", "sgst", "igst", "total"),
        discount_subtotal=True,
        two_page=True,
        page_break_after=2,
        lines=[
            LineSpec("M8 hex bolt (box)", "7318", Decimal("10"), "BOX", 15000, Decimal("5")),
            LineSpec("Angle grinder 850W", "8467", Decimal("4"), "NOS", 220000, Decimal("18")),
            LineSpec("Copper lug 25mm", "8536", Decimal("7"), "PKT", 33333, Decimal("12")),
            # One line carries a +₹1 vendor-rounding wobble on its tax (exercises the ±₹1 tol).
            LineSpec("Cordless drill 18V", "8467", Decimal("2"), "NOS", 500000, Decimal("18"),
                     tax_delta_paise=100),
        ],
        amount_in_words="Twenty Four Thousand Six Hundred One Only",
    )


def _scanned_no_text() -> InvoiceSpec:
    return InvoiceSpec(
        supplier_name="", supplier_gstin="", supplier_address="",
        buyer_name="", buyer_gstin="", buyer_address="",
        invoice_number="", invoice_date=date(2026, 1, 1), place_of_supply="",
        intra_state=True, lines=[], scanned=True,
    )


# ---------------------------------------------------------------------------
# Adversarial fixtures — each reproduces a CONFIRMED extractor defect and is doc-exact ONLY
# with its fix in place, so it fails the extractor without the fix (a real regression guard).
# ---------------------------------------------------------------------------


def _h1_supplier_gstin_above_billto() -> InvoiceSpec:
    # H1: intra-state (same state → a silent swap would NOT trip any state cross-check), with
    # the supplier↔buyer gap REMOVED so the SUPPLIER GSTIN sits just above "Bill To" and is the
    # Euclidean-nearest token. Doc-exact only if the buyer is taken strictly BELOW the anchor.
    return InvoiceSpec(
        supplier_name="Umang Traders",
        supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd",
        buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="UMG/2026/0099",
        invoice_date=date(2026, 5, 20),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        po_ref="PO-88010",
        tight_header=True,
        lines=[LineSpec("Office chair", "9401", Decimal("4"), "NOS", 450000, Decimal("18"))],
        amount_in_words="Twenty One Thousand Two Hundred Forty Only",
    )


def _h2_borderless_right_aligned() -> InvoiceSpec:
    # H2: BORDERLESS grid (no rules → word-geometry fallback) with RIGHT-aligned money columns
    # and a multi-word "Taxable Value" header. Doc-exact only with nearest-center bucketing +
    # multi-word header spans; the old left-edge banding drops/merges the taxable value.
    return InvoiceSpec(
        supplier_name="Gujarat Poly Pack LLP",
        supplier_gstin=GSTIN_SUPPLIER_GJ,
        supplier_address="Plot 22 GIDC, Vapi, Gujarat 396195",
        buyer_name="Southern Retail Pvt Ltd",
        buyer_gstin=GSTIN_BUYER_KA,
        buyer_address="45 MG Road, Bengaluru, Karnataka 560001",
        invoice_number="GPP-INV-2026-220",
        invoice_date=date(2026, 6, 12),
        place_of_supply="Karnataka (29)",
        intra_state=False,
        borderless=True,
        header_labels={"taxable": "Taxable Value", "hsn": "HSN"},
        lines=[
            LineSpec("PP bag", "6305", Decimal("2"), "BAG", 125000, Decimal("18")),
            LineSpec("HDPE drum", "3923", Decimal("1"), "NOS", 300000, Decimal("28")),
        ],
    )


def _m3_accounting_negative_round_off() -> InvoiceSpec:
    # M3: a NEGATIVE round-off printed in accounting parentheses "(0.30)". Doc-exact only if the
    # money tokenizer reads the parenthesised sign (a first-token grab loses it → +30, wrong).
    return InvoiceSpec(
        supplier_name="Northline Hardware Co",
        supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="7 Nashik Highway, Pune, Maharashtra 411001",
        buyer_name="Gifsy Solutions Ltd",
        buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="NHC/26-27/0410",
        invoice_date=date(2026, 7, 14),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        accounting_negatives=True,
        round_off_paise=-30,
        lines=[LineSpec("Bolt assortment", "7318", Decimal("3"), "BOX", 33000, Decimal("18"))],
        amount_in_words="One Thousand One Hundred Sixty Eight Only",
    )


def _m6_amount_total_columns() -> InvoiceSpec:
    # M6: the pre-tax column is headed "Amount" and sits beside a gross "Total". Doc-exact only
    # if the synonym resolution routes "Amount"→taxable and "Total"→line_total (no collision).
    return InvoiceSpec(
        supplier_name="Umang Traders",
        supplier_gstin=GSTIN_SUPPLIER_MH,
        supplier_address="14 Fort Road, Mumbai, Maharashtra 400001",
        buyer_name="Gifsy Solutions Ltd",
        buyer_gstin=GSTIN_BUYER_MH,
        buyer_address="Plot 9, Andheri East, Mumbai, Maharashtra 400069",
        invoice_number="UMG/2026/0140",
        invoice_date=date(2026, 5, 28),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        header_labels={"taxable": "Amount"},
        lines=[LineSpec("Steel rack", "9403", Decimal("2"), "NOS", 500000, Decimal("18"))],
        amount_in_words="Eleven Thousand Eight Hundred Only",
    )


def _tally_hash_rate_totals() -> HashRateInvoiceSpec:
    # A Tally "Tax Invoice" that FALLS THROUGH the Tally router into the generic engine: a bare
    # "#" invoice-number label, SGST/CGST/IGST columns that carry tax RATES (0 / 0 / 18%) not
    # amounts, a DOUBLED trailing "Amount" = the inclusive line total, and only a bare "TOTAL:"
    # line (no labelled taxable / grand total). Doc-exact only when the extractor reads the
    # bare-"#" number, maps the rate columns → gst_rate + the trailing Amount → line total, and
    # DERIVES both totals from the line items. Fictional data (no real names / GSTINs).
    return HashRateInvoiceSpec(
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


_FIXTURES: dict[str, InvoiceSpec] = {
    "intra_single_line": _intra_single_line(),
    "inter_multi_line": _inter_multi_line(),
    "edge_twopage_reorder": _edge_twopage_reorder(),
    "scanned_no_text": _scanned_no_text(),
    "h1_supplier_gstin_above_billto": _h1_supplier_gstin_above_billto(),
    "h2_borderless_right_aligned": _h2_borderless_right_aligned(),
    "m3_accounting_negative_round_off": _m3_accounting_negative_round_off(),
    "m6_amount_total_columns": _m6_amount_total_columns(),
}

# Fixtures built by the dedicated hash/rate/bare-TOTAL builder (their own spec + gold shape).
_HASH_RATE_FIXTURES: dict[str, HashRateInvoiceSpec] = {
    "tally_hash_rate_totals": _tally_hash_rate_totals(),
}


def _meta(fixture_id: str, spec: InvoiceSpec, gold: dict[str, object]) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "supply_type": "scanned" if spec.scanned else ("intra" if spec.intra_state else "inter"),
        "n_lines": len(gold["lines"]) if isinstance(gold["lines"], list) else 0,
        "page_count": gold["page_count"],
        "needs_ocr": gold["needs_ocr"],
        "review_needed": gold["review_needed"],
        "column_order": list(spec.column_order),
        "notes": _NOTES[fixture_id],
        "generator": "app.modules.expense.eval.synth.build_invoice_pdf (rl_config.invariant)",
        "schema_version": gold["schema_version"],
    }


_NOTES: dict[str, str] = {
    "intra_single_line": "Single-line intra-state invoice (CGST+SGST), zero round-off.",
    "inter_multi_line":
        "3 lines / 2 HSNs / mixed 18-12-28% rates, inter-state IGST, +Rs 0.28 round-off.",
    "edge_twopage_reorder": (
        "Line items span a page break; reordered columns (Qty|HSN|Description|... ) over a "
        "full GST grid; a discount/sub-total row; one line's tax carries a +₹1 wobble "
        "(±₹1 tolerance)."),
    "scanned_no_text": "Image-only / blank page: no text layer → needs_ocr, all fields MISSING.",
    "h1_supplier_gstin_above_billto": (
        "H1 guard: intra-state, supplier GSTIN sits just ABOVE 'Bill To' with NO gap; buyer "
        "must be taken strictly below the anchor (else a silent supplier⇄buyer swap)."),
    "h2_borderless_right_aligned": (
        "H2 guard: borderless grid (no rules) with RIGHT-aligned money + a multi-word 'Taxable "
        "Value' header; needs nearest-center bucketing + multi-word header spans."),
    "m3_accounting_negative_round_off": (
        "M3 guard: a negative round-off printed as accounting parentheses '(0.30)'; the money "
        "tokenizer must read the parenthesised sign (−₹0.30), not grab the first token (+₹0.30)."),
    "m6_amount_total_columns": (
        "M6 guard: pre-tax column headed 'Amount' beside a gross 'Total'; synonym resolution "
        "must route 'Amount'→taxable and 'Total'→line_total without colliding."),
    "tally_hash_rate_totals": (
        "Tally 'Tax Invoice' that falls through the Tally router into the generic engine: a "
        "bare '#' invoice-number label, SGST/CGST/IGST columns carrying tax RATES (0/0/18%) not "
        "amounts, a DOUBLED trailing 'Amount' = the inclusive line total, and only a bare "
        "'TOTAL:' line — so both totals are DERIVED from the line items (Σ taxable, Σ line "
        "total) and the arithmetic cross-checks corroborate them."),
}


def _hash_rate_meta(fixture_id: str, gold: dict[str, object]) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "supply_type": "inter",
        "n_lines": len(gold["lines"]) if isinstance(gold["lines"], list) else 0,
        "page_count": gold["page_count"],
        "needs_ocr": gold["needs_ocr"],
        "review_needed": gold["review_needed"],
        "column_order": ["sl", "description", "hsn", "rate", "qty", "taxable",
                         "sgst_rate", "cgst_rate", "igst_rate", "amount"],
        "notes": _NOTES[fixture_id],
        "generator": (
            "app.modules.expense.eval.synth.build_hash_rate_invoice_pdf (rl_config.invariant)"),
        "schema_version": gold["schema_version"],
    }


def main() -> None:
    for fixture_id, spec in _FIXTURES.items():
        pdf_bytes, gold = build_invoice_pdf(spec)
        out = GOLD_DIR / fixture_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "source.pdf").write_bytes(pdf_bytes)
        (out / "expected.json").write_text(
            json.dumps(gold, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        (out / "meta.json").write_text(
            json.dumps(_meta(fixture_id, spec, gold), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"wrote {fixture_id}: {len(pdf_bytes)} bytes, {len(gold['lines'])} lines")  # type: ignore[arg-type]
    for fixture_id, hr_spec in _HASH_RATE_FIXTURES.items():
        pdf_bytes, gold = build_hash_rate_invoice_pdf(hr_spec)
        out = GOLD_DIR / fixture_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "source.pdf").write_bytes(pdf_bytes)
        (out / "expected.json").write_text(
            json.dumps(gold, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8")
        (out / "meta.json").write_text(
            json.dumps(_hash_rate_meta(fixture_id, gold), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"wrote {fixture_id}: {len(pdf_bytes)} bytes, {len(gold['lines'])} lines")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
