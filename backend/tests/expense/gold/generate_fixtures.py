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
    InvoiceSpec,
    LineSpec,
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


_FIXTURES: dict[str, InvoiceSpec] = {
    "intra_single_line": _intra_single_line(),
    "inter_multi_line": _inter_multi_line(),
    "edge_twopage_reorder": _edge_twopage_reorder(),
    "scanned_no_text": _scanned_no_text(),
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


if __name__ == "__main__":
    main()
