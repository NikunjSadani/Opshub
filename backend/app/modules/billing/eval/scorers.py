"""Scorers for the billing (sales) eval harness.

Two independent grading surfaces, both engine-neutral (they grade a *prediction* dict/mapping
— they never import the live extractor or matcher):

* **Extraction** — the sales invoice canonical is the SAME GST-invoice schema the expense
  side uses (``app.modules.expense.canonical.ExtractedInvoice``; only the identity roles are
  inverted, which is data, not shape). So the field-typed + order-independent line scorers are
  reused verbatim from :mod:`app.modules.expense.eval.scorers` and re-exported here, giving
  billing a complete scoring surface without forking a second copy that could drift.

* **Match** — a NEW scorer for the PO-match stage. Given a predicted line→PO-line mapping vs
  the gold mapping it computes match precision / recall / F1 PLUS the money-critical
  ``false_auto_match_rate``: of the genuinely-unmatched lines (gold ``None``), how often did
  the matcher wrongly auto-attach one to a PO line. A false auto-match silently books revenue
  against the wrong order, so it is the dangerous error and gets its own FROZEN ceiling.

``check_match_thresholds`` guards the match floors the way ``check_thresholds`` guards the
extraction floors: a regression trips the gate.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# Re-export the extraction scorers unchanged (identical canonical schema).
from app.modules.expense.eval.scorers import (
    AggregateReport,
    EvalReport,
    FieldScore,
    Kind,
    aggregate,
    check_thresholds,
    score,
)

__all__ = [
    "AggregateReport",
    "EvalReport",
    "FieldScore",
    "Kind",
    "MatchAggregate",
    "MatchReport",
    "aggregate",
    "aggregate_match",
    "check_match_thresholds",
    "check_thresholds",
    "score",
    "score_match",
    "FROZEN_MIN_MATCH_F1",
    "FROZEN_MAX_FALSE_AUTO_MATCH_RATE",
]


# ---------------------------------------------------------------------------
# FROZEN regression minimums for the match stage — the accuracy floor the matcher must clear
# once wired into the integration test. Loosening these should be a deliberate, reviewed act.
# ---------------------------------------------------------------------------
FROZEN_MIN_MATCH_F1 = 0.90
FROZEN_MAX_FALSE_AUTO_MATCH_RATE = 0.02  # ≤2% of genuinely-unmatched lines may be auto-matched


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MatchReport:
    """Grade of one predicted line→PO-line mapping against its gold mapping."""

    scenario_id: str
    precision: float
    recall: float
    f1: float
    false_auto_match_rate: float   # of gold-None lines, fraction wrongly auto-matched
    unmatched_correct_rate: float  # of gold-None lines, fraction correctly left unmatched
    # Raw tallies retained so aggregate_match() can micro-average correctly.
    _prf_counts: tuple[int, int, int] = field(default=(0, 0, 0), repr=False)  # tp, fp, fn
    _unmatched_counts: tuple[int, int] = field(default=(0, 0), repr=False)    # false_auto, total


@dataclass
class MatchAggregate:
    """Match grade pooled across a scenario set (micro-averaged)."""

    n_scenarios: int
    precision: float
    recall: float
    f1: float
    false_auto_match_rate: float
    unmatched_correct_rate: float


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------


def _safe_div(num: float, den: float, default: float) -> float:
    return num / den if den else default


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = _safe_div(tp, tp + fp, 1.0)
    recall = _safe_div(tp, tp + fn, 1.0)
    f1 = _safe_div(2 * precision * recall, precision + recall, 0.0)
    return precision, recall, f1


def score_match(
    predicted: Mapping[int, int | None],
    gold: Mapping[int, int | None],
    *,
    scenario_id: str = "",
) -> MatchReport:
    """Grade a predicted ``{line_no: po_line_id | None}`` mapping against gold.

    Scoring per invoice line (``line_no`` is the join key; a line absent from ``predicted`` is
    treated as left-unmatched, i.e. ``None``):

    * gold has a PO line and ``pred == gold`` → **TP** (correct match);
    * gold has a PO line and ``pred is None`` → **FN** (missed a real match);
    * gold has a PO line and ``pred`` names a *different* PO line → substitution → **FP + FN**;
    * gold is ``None`` and ``pred is None`` → correct-unmatched (a true negative — excluded
      from PRF, which is a match-detection metric);
    * gold is ``None`` and ``pred`` names a PO line → **false auto-match** → **FP** (and it
      lands in ``false_auto_match_rate`` — the dangerous money error).
    """
    tp = fp = fn = 0
    false_auto = 0
    gold_none_total = 0

    for line_no in set(gold) | set(predicted):
        g = gold.get(line_no)
        p = predicted.get(line_no)
        if g is None:
            gold_none_total += 1
            if p is not None:
                false_auto += 1
                fp += 1
            # p is None → correctly left unmatched (true negative; not in PRF).
            continue
        if p == g:
            tp += 1
        elif p is None:
            fn += 1
        else:
            fp += 1
            fn += 1

    precision, recall, f1 = _prf(tp, fp, fn)
    far = _safe_div(false_auto, gold_none_total, 0.0)
    ucr = _safe_div(gold_none_total - false_auto, gold_none_total, 1.0)
    return MatchReport(
        scenario_id=scenario_id,
        precision=precision,
        recall=recall,
        f1=f1,
        false_auto_match_rate=far,
        unmatched_correct_rate=ucr,
        _prf_counts=(tp, fp, fn),
        _unmatched_counts=(false_auto, gold_none_total),
    )


def aggregate_match(reports: Sequence[MatchReport]) -> MatchAggregate:
    """Pool per-scenario match reports into a micro-averaged grade."""
    n = len(reports)
    if n == 0:
        return MatchAggregate(0, 1.0, 1.0, 0.0, 0.0, 1.0)

    tp = sum(r._prf_counts[0] for r in reports)
    fp = sum(r._prf_counts[1] for r in reports)
    fn = sum(r._prf_counts[2] for r in reports)
    precision, recall, f1 = _prf(tp, fp, fn)

    false_auto = sum(r._unmatched_counts[0] for r in reports)
    gold_none_total = sum(r._unmatched_counts[1] for r in reports)
    far = _safe_div(false_auto, gold_none_total, 0.0)
    ucr = _safe_div(gold_none_total - false_auto, gold_none_total, 1.0)
    return MatchAggregate(
        n_scenarios=n,
        precision=precision,
        recall=recall,
        f1=f1,
        false_auto_match_rate=far,
        unmatched_correct_rate=ucr,
    )


def check_match_thresholds(agg: MatchAggregate) -> list[str]:
    """Return frozen-minimum violations for the match stage (empty == gate green)."""
    violations: list[str] = []
    if agg.f1 < FROZEN_MIN_MATCH_F1:
        violations.append(f"match_f1 {agg.f1:.3f} < {FROZEN_MIN_MATCH_F1}")
    if agg.false_auto_match_rate > FROZEN_MAX_FALSE_AUTO_MATCH_RATE:
        violations.append(
            f"false_auto_match_rate {agg.false_auto_match_rate:.3f} "
            f"> {FROZEN_MAX_FALSE_AUTO_MATCH_RATE}"
        )
    return violations
