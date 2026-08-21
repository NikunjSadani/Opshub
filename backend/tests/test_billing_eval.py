"""Prove the billing eval scorers on CONSTRUCTED predictions (no live engine imported).

We synthesize the gold BY CONSTRUCTION (``synth``) and grade fabricated predictions against
it — a perfect prediction/mapping scores 1.0, and deliberately-wrong ones trip the frozen
gate. The real extractor/matcher-vs-gold test is wired at integration; the frozen minimums
here are the seam that later turns accuracy into a regression gate.

Two surfaces:
* extraction — the sales invoice canonical is the expense GST-invoice schema (inverted roles),
  so the reused field/line scorers must grade a perfect sales prediction as exact;
* match — the new PO-match scorer must reward a perfect mapping, fail a wrong mapping, and
  CATCH a false auto-match on a genuinely-unmatched line (the dangerous money error).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.modules.billing.eval import scorers
from app.modules.billing.eval.scorers import (
    aggregate_match,
    check_match_thresholds,
    score,
    score_match,
)
from app.modules.billing.eval.synth import (
    CLIENT_INTRA_ADDRESS,
    CLIENT_INTRA_GSTIN,
    CLIENT_INTRA_NAME,
    SUPPLIER_US_GSTIN,
    SUPPLIER_US_NAME,
    SalesInvoiceSpec,
    SalesLineSpec,
    build_match_scenario,
    build_sales_invoice_pdf,
)
from app.modules.expense.canonical import (
    ArithmeticChecks,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceHeader,
    InvoiceLine,
    InvoiceTotals,
)

_MONEY_KEYS = {
    "unit_rate_paise", "taxable_paise", "cgst_paise", "sgst_paise", "igst_paise",
    "line_total_paise", "total_taxable_paise", "total_cgst_paise", "total_sgst_paise",
    "total_igst_paise", "round_off_paise", "grand_total_paise",
}
_NUM_KEYS = {"quantity", "gst_rate"}
_DATE_KEYS = {"invoice_date"}


def _sales_spec() -> SalesInvoiceSpec:
    return SalesInvoiceSpec(
        buyer_name=CLIENT_INTRA_NAME,
        buyer_gstin=CLIENT_INTRA_GSTIN,
        buyer_address=CLIENT_INTRA_ADDRESS,
        invoice_number="TGS/2026/00417",
        invoice_date=date(2026, 8, 20),
        place_of_supply="Maharashtra (27)",
        intra_state=True,
        po_ref="PO-BAJAJ-9931",
        lines=[
            SalesLineSpec("Widget Assembly Type A", "847130", Decimal("100"), "NOS", 15000,
                          Decimal("18"), product_code="WID-100"),
            SalesLineSpec("Rubber Gasket 20mm", "401693", Decimal("50"), "NOS", 5000,
                          Decimal("18"), product_code="GSK-200"),
        ],
    )


# ---------------------------------------------------------------------------
# Prediction builder (sales gold dict -> canonical ExtractedInvoice)
# ---------------------------------------------------------------------------


def _norm(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key in _MONEY_KEYS:
        return int(value)
    if key in _NUM_KEYS:
        return Decimal(str(value))
    if key in _DATE_KEYS:
        return date.fromisoformat(value)
    return str(value)


def _mk_field(key: str, value: Any) -> Field[Any]:
    if value is None:
        return Field(None, "", 0.0, "text_layer/1.0", status=FieldStatus.MISSING)
    return Field(_norm(key, value), str(value), 0.95, "text_layer/1.0", status=FieldStatus.OK)


def _prediction_from_gold(gold: dict[str, Any]) -> ExtractedInvoice:
    h = gold["header"]
    header = InvoiceHeader(**{a: _mk_field(a, h[a]) for a in h})
    t = gold["totals"]
    totals = InvoiceTotals(**{a: _mk_field(a, t[a]) for a in t})
    lines: list[InvoiceLine] = []
    for row in gold["lines"]:
        ln = int(row["line_no"])
        fields = {a: _mk_field(a, row[a]) for a in row if a != "line_no"}
        lines.append(InvoiceLine(line_no=ln, **fields))
    arithmetic = ArithmeticChecks(True, True, True, True, 0)
    return ExtractedInvoice(
        schema_version=gold["schema_version"],
        doc_type=gold["doc_type"],
        source_engine="text_layer/1.0",
        page_count=int(gold.get("page_count", 1)),
        needs_ocr=bool(gold["needs_ocr"]),
        review_needed=bool(gold["review_needed"]),
        review_reasons=[],
        header=header,
        lines=lines,
        totals=totals,
        arithmetic=arithmetic,
        raw_text="",
        content_hash="",
        dedup_key="",
    )


# ---------------------------------------------------------------------------
# synth: byte-stability + inverted (sales) roles
# ---------------------------------------------------------------------------


def test_sales_pdf_is_byte_stable_and_is_pdf() -> None:
    pdf1, gold1 = build_sales_invoice_pdf(_sales_spec())
    pdf2, _ = build_sales_invoice_pdf(_sales_spec())
    assert pdf1.startswith(b"%PDF")
    assert pdf1 == pdf2, "sales invoice PDF must be byte-stable across regenerations"
    # Inverted roles: supplier is us, buyer is the client.
    assert gold1["header"]["supplier_gstin"] == SUPPLIER_US_GSTIN
    assert gold1["header"]["supplier_name"] == SUPPLIER_US_NAME
    assert gold1["header"]["buyer_gstin"] == CLIENT_INTRA_GSTIN


# ---------------------------------------------------------------------------
# extraction scorers (reused) grade a perfect sales prediction as exact
# ---------------------------------------------------------------------------


def test_perfect_sales_prediction_scores_exact() -> None:
    _, gold = build_sales_invoice_pdf(_sales_spec())
    report = score(_prediction_from_gold(gold), gold, fixture_id="sales_intra")
    assert report.doc_exact_rate == 1.0
    assert all(fs.passed for fs in report.per_field)
    assert report.line_item_prf == (1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# match scorer
# ---------------------------------------------------------------------------


def test_perfect_mapping_scores_1_and_passes_gate() -> None:
    sc = build_match_scenario()
    report = score_match(sc.gold_mapping, sc.gold_mapping, scenario_id=sc.scenario_id)
    assert (report.precision, report.recall, report.f1) == (1.0, 1.0, 1.0)
    assert report.false_auto_match_rate == 0.0
    assert report.unmatched_correct_rate == 1.0
    agg = aggregate_match([report])
    assert check_match_thresholds(agg) == []


def test_wrong_mapping_fails_the_gate() -> None:
    sc = build_match_scenario()
    pred = dict(sc.gold_mapping)
    # Break two real matches: point line 1 at the wrong PO line, drop line 2 entirely.
    pred[1] = 999          # substitution → FP + FN
    pred[2] = None         # missed a real match → FN
    report = score_match(pred, sc.gold_mapping, scenario_id=sc.scenario_id)
    assert report.f1 < 1.0
    agg = aggregate_match([report])
    violations = check_match_thresholds(agg)
    assert any("match_f1" in v for v in violations)


def test_false_auto_match_on_unmatched_line_is_caught() -> None:
    sc = build_match_scenario()
    # Line 3 is genuinely unmatched (gold None). Auto-matching it to a PO line is the
    # dangerous money error — it must show up as a false auto-match and trip the ceiling.
    assert sc.gold_mapping[3] is None
    pred = dict(sc.gold_mapping)
    pred[3] = 101
    report = score_match(pred, sc.gold_mapping, scenario_id=sc.scenario_id)
    assert report.false_auto_match_rate == 1.0   # 1 of 1 genuinely-unmatched line
    assert report.unmatched_correct_rate == 0.0
    agg = aggregate_match([report])
    violations = check_match_thresholds(agg)
    assert any("false_auto_match_rate" in v for v in violations)


def test_ambiguous_wrong_candidate_costs_f1() -> None:
    sc = build_match_scenario()
    # Line 5 is the ambiguous 2-candidate case; gold is PO 104. Picking 103 is a substitution.
    assert sc.gold_mapping[5] == 104
    pred = dict(sc.gold_mapping)
    pred[5] = 103
    report = score_match(pred, sc.gold_mapping)
    assert report.f1 < 1.0
    assert report.false_auto_match_rate == 0.0   # not an unmatched-line error


def test_frozen_match_minimums_are_pinned() -> None:
    assert scorers.FROZEN_MIN_MATCH_F1 == 0.90
    assert scorers.FROZEN_MAX_FALSE_AUTO_MATCH_RATE == 0.02
