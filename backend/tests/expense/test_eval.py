"""Prove the expense eval scorers on CONSTRUCTED predictions.

We do NOT import the extractor (built in parallel). Instead we synthesize canonical
``ExtractedInvoice`` predictions FROM the committed gold records — perfect, and deliberately
perturbed — and assert the scorers grade them as expected. The real extractor-vs-gold
accuracy test is wired at integration; the frozen-minimum guard here is the seam that later
turns extraction accuracy into a regression gate.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.modules.expense.canonical import (
    ArithmeticChecks,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceHeader,
    InvoiceLine,
    InvoiceTotals,
)
from app.modules.expense.eval import scorers
from app.modules.expense.eval.scorers import aggregate, check_thresholds, score

GOLD_DIR = Path(__file__).resolve().parent / "gold"
FIXTURE_IDS = ("intra_single_line", "inter_multi_line", "edge_twopage_reorder", "scanned_no_text")

# Which canonical kind each gold key deserializes to, so the builder types the envelope.
_MONEY_KEYS = {
    "unit_rate_paise", "taxable_paise", "cgst_paise", "sgst_paise", "igst_paise",
    "line_total_paise", "total_taxable_paise", "total_cgst_paise", "total_sgst_paise",
    "total_igst_paise", "round_off_paise", "grand_total_paise",
}
_NUM_KEYS = {"quantity", "gst_rate"}
_DATE_KEYS = {"invoice_date"}

_MISSING = object()  # sentinel for a value_edit that forces a MISSING field
_LINE_RE = re.compile(r"^lines\[(\d+)\]\.(.+)$")


# ---------------------------------------------------------------------------
# Gold loading
# ---------------------------------------------------------------------------


def _load_gold(fixture_id: str) -> dict[str, Any]:
    return json.loads((GOLD_DIR / fixture_id / "expected.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Prediction builder (gold dict -> canonical ExtractedInvoice)
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


def _mk_field(key: str, value: Any, confidence: float) -> Field[Any]:
    if value is None:
        return Field(None, "", 0.0, "text_layer/1.0", status=FieldStatus.MISSING)
    return Field(_norm(key, value), str(value), confidence, "text_layer/1.0",
                 status=FieldStatus.OK)


def _set_path(gold: dict[str, Any], path: str, value: Any) -> None:
    m = _LINE_RE.match(path)
    if m:
        idx, attr = int(m.group(1)), m.group(2)
        # gold line_no is 1-based; find the matching row.
        for row in gold["lines"]:
            if row["line_no"] == idx:
                row[attr] = None if value is _MISSING else value
                return
        raise KeyError(f"no gold line_no={idx}")
    section, attr = path.split(".", 1)
    gold[section][attr] = None if value is _MISSING else value


def build_prediction(
    gold: dict[str, Any],
    *,
    value_edits: dict[str, Any] | None = None,
    conf_edits: dict[str, float] | None = None,
    default_conf: float = 0.95,
    review_needed: bool | None = None,
) -> ExtractedInvoice:
    """Fabricate a canonical prediction from a gold record.

    ``value_edits`` maps a dotted field path to a replacement value (or ``_MISSING``) — this
    is how we inject wrong / out-of-tolerance / dropped values. ``conf_edits`` overrides the
    per-field confidence so calibration can be exercised.
    """
    g = copy.deepcopy(gold)
    for path, val in (value_edits or {}).items():
        _set_path(g, path, val)
    conf_edits = conf_edits or {}

    def conf(path: str) -> float:
        return conf_edits.get(path, default_conf)

    h = g["header"]
    header = InvoiceHeader(
        **{a: _mk_field(a, h[a], conf(f"header.{a}")) for a in h}
    )
    t = g["totals"]
    totals = InvoiceTotals(
        **{a: _mk_field(a, t[a], conf(f"totals.{a}")) for a in t}
    )
    lines: list[InvoiceLine] = []
    for row in g["lines"]:
        ln = int(row["line_no"])
        fields = {
            a: _mk_field(a, row[a], conf(f"lines[{ln}].{a}"))
            for a in row if a != "line_no"
        }
        lines.append(InvoiceLine(line_no=ln, **fields))

    arithmetic = ArithmeticChecks(
        lines_sum_matches_taxable=True,
        per_line_tax_consistent=True,
        totals_add_to_grand=True,
        supply_type_consistent=True,
        max_abs_delta_paise=0,
    )
    rn = g["review_needed"] if review_needed is None else review_needed
    return ExtractedInvoice(
        schema_version=g["schema_version"],
        doc_type=g["doc_type"],
        source_engine="text_layer/1.0",
        page_count=int(g.get("page_count", 1)),
        needs_ocr=bool(g["needs_ocr"]),
        review_needed=bool(rn),
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
# Fixtures on disk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_fixture_files_present_and_consistent(fixture_id: str) -> None:
    d = GOLD_DIR / fixture_id
    pdf = d / "source.pdf"
    assert pdf.exists() and pdf.stat().st_size > 0, "source.pdf missing/empty"
    assert pdf.read_bytes().startswith(b"%PDF"), "source.pdf is not a PDF"
    gold = _load_gold(fixture_id)
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert gold["schema_version"] == "gst_invoice/1.0.0"
    assert meta["needs_ocr"] == gold["needs_ocr"]
    assert meta["n_lines"] == len(gold["lines"])


def test_scanned_fixture_is_all_missing() -> None:
    gold = _load_gold("scanned_no_text")
    assert gold["needs_ocr"] is True
    assert gold["lines"] == []
    assert all(v is None for v in gold["header"].values())
    assert gold["totals"]["grand_total_paise"] is None


# ---------------------------------------------------------------------------
# Perfect prediction == gold
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", FIXTURE_IDS)
def test_perfect_prediction_scores_all_pass(fixture_id: str) -> None:
    gold = _load_gold(fixture_id)
    pred = build_prediction(gold)
    report = score(pred, gold, fixture_id=fixture_id)

    assert report.doc_exact_rate == 1.0
    assert all(fs.passed for fs in report.per_field), \
        [fs.field_path for fs in report.per_field if not fs.passed]
    p, r, f1 = report.line_item_prf
    assert (p, r, f1) == (1.0, 1.0, 1.0)
    # Correctly-not-flagged / correctly-flagged both yield perfect review P/R on one doc.
    assert report.review_precision_recall == (1.0, 1.0)


def test_perfect_aggregate_over_gold_set_is_exact() -> None:
    reports = [score(build_prediction(_load_gold(fid)), _load_gold(fid), fixture_id=fid)
               for fid in FIXTURE_IDS]
    agg = aggregate(reports)
    assert agg.n_docs == 4
    assert agg.doc_exact_rate == 1.0
    assert agg.macro_field_accuracy == 1.0
    assert agg.line_item_prf == (1.0, 1.0, 1.0)
    assert agg.review_precision_recall[1] == 1.0  # recall: every should-review doc flagged
    assert check_thresholds(agg) == []


# ---------------------------------------------------------------------------
# Money tolerance
# ---------------------------------------------------------------------------


def test_wrong_money_beyond_tolerance_fails_that_field() -> None:
    gold = _load_gold("intra_single_line")
    # grand_total has zero tolerance; +₹5 (500 paise) must fail exactly that field.
    pred = build_prediction(
        gold, value_edits={"totals.grand_total_paise": gold["totals"]["grand_total_paise"] + 500})
    report = score(pred, gold)

    bad = {fs.field_path for fs in report.per_field if not fs.passed}
    assert bad == {"totals.grand_total_paise"}
    assert report.doc_exact_rate == 0.0
    assert report.field_accuracy["totals.grand_total_paise"] == 0.0


def test_within_tolerance_money_delta_passes() -> None:
    gold = _load_gold("intra_single_line")
    ln = gold["lines"][0]["line_no"]
    base = gold["lines"][0]["sgst_paise"]
    # Line tax tolerance is ₹1 (100 paise); +50 must still pass and keep the doc exact.
    pred = build_prediction(gold, value_edits={f"lines[{ln}].sgst_paise": base + 50})
    report = score(pred, gold)
    assert all(fs.passed for fs in report.per_field)
    assert report.doc_exact_rate == 1.0

    # +150 exceeds the ₹1 line-tax tolerance → that line field fails, doc no longer exact.
    pred_bad = build_prediction(gold, value_edits={f"lines[{ln}].sgst_paise": base + 150})
    report_bad = score(pred_bad, gold)
    failed = {fs.field_path for fs in report_bad.per_field if not fs.passed}
    assert f"lines[{ln}].sgst_paise" in failed
    assert report_bad.doc_exact_rate == 0.0
    # The imperfect row is no longer a clean TP → line recall drops below 1.
    assert report_bad.line_item_prf[1] < 1.0


def test_name_field_gets_partial_credit() -> None:
    gold = _load_gold("intra_single_line")
    # One-token typo in a multi-token name: fails exact match but earns token-overlap credit.
    pred = build_prediction(gold, value_edits={"header.supplier_name": "Umang Traderss"})
    report = score(pred, gold)
    sn = next(fs for fs in report.per_field if fs.field_path == "header.supplier_name")
    assert sn.passed is False
    assert 0.0 < sn.partial < 1.0


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def test_calibration_returns_sane_bins() -> None:
    gold = _load_gold("inter_multi_line")
    # A confidently-right field and a low-confidence wrong field → two distinct bins.
    pred = build_prediction(
        gold,
        value_edits={"header.invoice_number": "WRONG-999"},
        conf_edits={"header.invoice_number": 0.15, "header.supplier_gstin": 0.95},
    )
    report = score(pred, gold)
    assert report.calibration, "calibration should not be empty"
    for center, acc, n in report.calibration:
        assert 0.0 < center < 1.0
        assert 0.0 <= acc <= 1.0
        assert n >= 1
    # The low-confidence bin should reflect the wrong field (accuracy < 1 there).
    low = [acc for center, acc, n in report.calibration if center < 0.5]
    assert low and min(low) < 1.0


# ---------------------------------------------------------------------------
# review_needed reality check
# ---------------------------------------------------------------------------


def test_review_flag_reality_check() -> None:
    gold = _load_gold("intra_single_line")

    # (a) clean + not-flagged → true negative → vacuously perfect P/R on the single doc.
    clean = score(build_prediction(gold, review_needed=False), gold)
    assert clean.review_precision_recall == (1.0, 1.0)

    # (b) required field actually wrong, but review DID NOT fire → a miss (recall 0).
    wrong_unflagged = score(
        build_prediction(
            gold,
            value_edits={"header.invoice_number": _MISSING},  # required field dropped
            review_needed=False,
        ),
        gold,
    )
    assert wrong_unflagged.review_precision_recall[1] == 0.0  # recall

    # (c) required field wrong AND review fired → true positive.
    wrong_flagged = score(
        build_prediction(
            gold,
            value_edits={"header.invoice_number": _MISSING},
            review_needed=True,
        ),
        gold,
    )
    assert wrong_flagged.review_precision_recall == (1.0, 1.0)

    # Pooled across (b)+(c): one caught, one missed → recall 0.5, precision 1.0.
    agg = aggregate([wrong_unflagged, wrong_flagged])
    assert agg.review_precision_recall == (1.0, 0.5)


# ---------------------------------------------------------------------------
# Frozen-minimum regression guard
# ---------------------------------------------------------------------------


def test_threshold_guard_trips_on_degraded_extraction() -> None:
    gold = _load_gold("inter_multi_line")
    # Break a whole line (drop it) + corrupt totals → aggregate must violate the floors.
    pred = build_prediction(
        gold,
        value_edits={
            "totals.grand_total_paise": 0,
            "totals.total_taxable_paise": 0,
            "header.invoice_number": _MISSING,
        },
    )
    report = score(pred, gold)
    agg = aggregate([report])
    violations = check_thresholds(agg)
    assert violations, "degraded extraction should trip the frozen minimums"
    assert any("doc_exact_rate" in v for v in violations)


def test_frozen_minimums_are_pinned() -> None:
    # Pin the floor values so a silent downgrade shows up as a failing diff.
    assert scorers.FROZEN_MIN_DOC_EXACT_RATE == 0.95
    assert scorers.FROZEN_MIN_FIELD_ACCURACY == 0.98
    assert scorers.FROZEN_MIN_LINE_F1 == 0.95
    assert scorers.FROZEN_MIN_REVIEW_RECALL == 0.90
