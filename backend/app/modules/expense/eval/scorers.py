"""Field-typed scorers for the Expense/Invoice extractor.

Grades a canonical :class:`ExtractedInvoice` *prediction* against a plain-dict *gold*
record. Every canonical field is scored by a scorer chosen from its TYPE, matching the
house conventions the review gate already uses:

* **exact** — ids / GSTIN compared via ``collapse_ws`` equality; names / addresses via
  ``match_key`` equality, with a token-overlap partial-credit float so the aggregate
  reflects near-misses (a one-typo supplier name is not scored the same as garbage).
* **numeric-tolerance** — money fields pass when ``abs(pred-gold) <= tol_paise`` (default
  ``0``; per-field override, e.g. per-line tax gets ``LINE_TAX_TOL_PAISE`` = ₹1 to absorb
  vendor rounding); ``quantity`` / ``gst_rate`` compare as exact ``Decimal`` (value-equal,
  so ``18`` == ``18.00``).
* **date-normalized** — ``date`` objects compared directly.

The gold dict shape (also what ``synth.build_invoice_pdf`` returns) mirrors the canonical
tree but with plain, JSON-round-trippable values::

    {
      "schema_version": str, "doc_type": "gst_invoice", "needs_ocr": bool,
      "header": {<attr>: value | None, "invoice_date": "YYYY-MM-DD" | None},
      "totals": {<attr>: int_paise | None, "amount_in_words": str | None},
      "lines":  [{"line_no": int, "description": str, "hsn_sac": str,
                  "quantity": "3", "unit": "NOS", "unit_rate_paise": int,
                  "taxable_paise": int, "gst_rate": "18.00", "cgst_paise": int,
                  "sgst_paise": int, "igst_paise": int, "line_total_paise": int}, ...],
    }

``None`` means "correctly MISSING": a prediction that also reports the field MISSING scores
a pass on it (this is how the scanned/OCR fixture earns full marks).
"""
from __future__ import annotations

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.modules.expense.canonical import (
    LINE_TAX_TOL_PAISE,
    REQUIRED_FIELD_PATHS,
    TOTALS_TOL_PAISE,
    ExtractedInvoice,
    Field,
    FieldStatus,
    InvoiceLine,
)
from app.modules.masterdata.normalize import collapse_ws, match_key

# ---------------------------------------------------------------------------
# Field-type registry: which scorer + tolerance applies to each canonical field.
# ---------------------------------------------------------------------------


class Kind(str, enum.Enum):
    ID = "id"        # exact via collapse_ws (invoice no, GSTIN, HSN, PO ref, place-of-supply)
    NAME = "name"    # exact via match_key + token-overlap partial credit (names/addresses)
    MONEY = "money"  # integer paise, abs-delta tolerance
    NUM = "num"      # Decimal value-equality (quantity, gst_rate)
    DATE = "date"    # date-object equality


@dataclass(frozen=True)
class _Spec:
    attr: str
    kind: Kind
    tol_paise: int = 0  # only meaningful for MONEY


# Header scalars (dotted path == "header.<attr>").
_HEADER_SPECS: tuple[_Spec, ...] = (
    _Spec("supplier_name", Kind.NAME),
    _Spec("supplier_gstin", Kind.ID),
    _Spec("supplier_address", Kind.NAME),
    _Spec("buyer_name", Kind.NAME),
    _Spec("buyer_gstin", Kind.ID),
    _Spec("buyer_address", Kind.NAME),
    _Spec("invoice_number", Kind.ID),
    _Spec("invoice_date", Kind.DATE),
    _Spec("place_of_supply", Kind.ID),
    _Spec("po_ref", Kind.ID),
)

# Totals scalars ("totals.<attr>"). Round-off has its own explicit line, so totals are exact.
_TOTALS_SPECS: tuple[_Spec, ...] = (
    _Spec("total_taxable_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("total_cgst_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("total_sgst_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("total_igst_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("round_off_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("grand_total_paise", Kind.MONEY, TOTALS_TOL_PAISE),
    _Spec("amount_in_words", Kind.NAME),
)

# Per-line scalars ("lines[<i>].<attr>"). taxable is pre-tax (exact); the tax components
# absorb ±LINE_TAX_TOL_PAISE of vendor rounding, and the line total (which carries that
# rounding) inherits the same tolerance.
_LINE_SPECS: tuple[_Spec, ...] = (
    _Spec("description", Kind.NAME),
    _Spec("hsn_sac", Kind.ID),
    _Spec("quantity", Kind.NUM),
    _Spec("unit", Kind.ID),
    _Spec("unit_rate_paise", Kind.MONEY, 0),
    _Spec("taxable_paise", Kind.MONEY, 0),
    _Spec("gst_rate", Kind.NUM),
    _Spec("cgst_paise", Kind.MONEY, LINE_TAX_TOL_PAISE),
    _Spec("sgst_paise", Kind.MONEY, LINE_TAX_TOL_PAISE),
    _Spec("igst_paise", Kind.MONEY, LINE_TAX_TOL_PAISE),
    _Spec("line_total_paise", Kind.MONEY, LINE_TAX_TOL_PAISE),
)

# Line fields whose agreement identifies "the same row" across a reorder / page break. Kept
# to structural, rounding-stable fields so a ±₹1 tax wobble never breaks row identity.
_LINE_IDENTITY_ATTRS: tuple[str, ...] = ("hsn_sac", "quantity", "taxable_paise")

_CALIBRATION_BINS = 10  # deciles: [0,0.1), [0.1,0.2), ... [0.9,1.0]

# ---------------------------------------------------------------------------
# FROZEN regression minimums — the accuracy floor a real extractor must clear once it is
# wired into the integration test. Bumping these DOWN should be a deliberate, reviewed act.
# ---------------------------------------------------------------------------
FROZEN_MIN_DOC_EXACT_RATE = 0.95
FROZEN_MIN_FIELD_ACCURACY = 0.98
FROZEN_MIN_LINE_F1 = 0.95
FROZEN_MIN_REVIEW_RECALL = 0.90


# ---------------------------------------------------------------------------
# Report dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FieldScore:
    """One graded field: the normalized pair, the pass/partial verdict, and the envelope
    provenance (confidence + status) needed to calibrate the confidence ladder."""

    field_path: str
    kind: Kind
    predicted: object          # normalized predicted value (or None if MISSING)
    gold: object               # normalized gold value (or None if MISSING)
    passed: bool               # exact / within-tolerance match
    partial: float             # 0..1; name-field token overlap, else 1.0/0.0 == passed
    confidence: float          # from the predicted Field envelope
    status: FieldStatus        # predicted field status
    tol_paise: int | None = None  # applied tolerance for MONEY fields, else None


@dataclass
class EvalReport:
    """Per-document grade."""

    fixture_id: str
    per_field: list[FieldScore]
    field_accuracy: dict[str, float]
    line_item_prf: tuple[float, float, float]     # (precision, recall, f1) on rows
    doc_exact_rate: float                          # 1.0 if the whole doc is exact, else 0.0
    calibration: list[tuple[float, float, int]]    # (bin_center, actual_accuracy, n)
    review_precision_recall: tuple[float, float]   # did review_needed fire on wrong docs?
    # Raw tallies retained so aggregate() can pool correctly (micro-averaging).
    _line_counts: tuple[int, int, int] = field(default=(0, 0, 0), repr=False)  # tp, fp, fn
    _review_counts: tuple[int, int, int, int] = field(
        default=(0, 0, 0, 0), repr=False)  # tp, fp, fn, tn


@dataclass
class AggregateReport:
    """Grade pooled across a gold set (micro-averaged where it matters)."""

    n_docs: int
    field_accuracy: dict[str, float]               # per canonical field (line index collapsed)
    macro_field_accuracy: float                    # unweighted mean of field_accuracy values
    line_item_prf: tuple[float, float, float]
    doc_exact_rate: float
    calibration: list[tuple[float, float, int]]
    review_precision_recall: tuple[float, float]


# ---------------------------------------------------------------------------
# Comparison primitives
# ---------------------------------------------------------------------------


def _both_missing(pred: object, gold: object) -> tuple[bool, float] | None:
    """Handle the MISSING axis: returns a verdict when either side is None, else None."""
    if pred is None and gold is None:
        return True, 1.0          # correctly reported absent
    if pred is None or gold is None:
        return False, 0.0         # one side has a value the other lacks
    return None


def _token_jaccard(a: str, b: str) -> float:
    ta = set(match_key(a).split())
    tb = set(match_key(b).split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _to_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return None
    return None


def _to_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _to_int(value: object) -> int | None:
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _compare(kind: Kind, pred: object, gold: object, tol_paise: int) -> tuple[bool, float]:
    """Typed comparison → (passed, partial). ``partial`` gives names token-overlap credit;
    for every other kind it is simply ``float(passed)``."""
    early = _both_missing(pred, gold)
    if early is not None:
        return early

    if kind is Kind.ID:
        passed = collapse_ws(str(pred)) == collapse_ws(str(gold))
        return passed, float(passed)
    if kind is Kind.NAME:
        passed = match_key(str(pred)) == match_key(str(gold))
        return passed, 1.0 if passed else _token_jaccard(str(pred), str(gold))
    if kind is Kind.MONEY:
        pi, gi = _to_int(pred), _to_int(gold)
        if pi is None or gi is None:
            return False, 0.0
        passed = abs(pi - gi) <= tol_paise
        return passed, float(passed)
    if kind is Kind.NUM:
        pd, gd = _to_decimal(pred), _to_decimal(gold)
        if pd is None or gd is None:
            return False, 0.0
        passed = pd == gd
        return passed, float(passed)
    # Kind.DATE
    pdt, gdt = _to_date(pred), _to_date(gold)
    if pdt is None or gdt is None:
        return False, 0.0
    passed = pdt == gdt
    return passed, float(passed)


def _score_field(path: str, spec: _Spec, envelope: Field[object], gold: object) -> FieldScore:
    pred = envelope.value_normalized if envelope.status is not FieldStatus.MISSING else None
    passed, partial = _compare(spec.kind, pred, gold, spec.tol_paise)
    return FieldScore(
        field_path=path,
        kind=spec.kind,
        predicted=pred,
        gold=gold,
        passed=passed,
        partial=partial,
        confidence=float(envelope.confidence),
        status=envelope.status,
        tol_paise=spec.tol_paise if spec.kind is Kind.MONEY else None,
    )


# ---------------------------------------------------------------------------
# Line-item matching (order-independent; robust to page-break reorder)
# ---------------------------------------------------------------------------


def _line_field(line: InvoiceLine, attr: str) -> Field[object]:
    envelope: Field[object] = getattr(line, attr)
    return envelope


def _line_identity(values: dict[str, object]) -> tuple[object, ...]:
    key: list[object] = []
    for attr in _LINE_IDENTITY_ATTRS:
        v = values[attr]
        if attr == "quantity":
            key.append(_to_decimal(v))
        elif attr == "hsn_sac":
            key.append(collapse_ws(str(v)) if v is not None else None)
        else:
            key.append(_to_int(v))
    return tuple(key)


def _pred_line_values(line: InvoiceLine) -> dict[str, object]:
    out: dict[str, object] = {}
    for spec in _LINE_SPECS:
        env = _line_field(line, spec.attr)
        out[spec.attr] = env.value_normalized if env.status is not FieldStatus.MISSING else None
    return out


def _match_lines(
    pred_lines: Sequence[InvoiceLine], gold_lines: Sequence[dict[str, object]]
) -> tuple[list[tuple[InvoiceLine, dict[str, object]]], int, int]:
    """Greedy identity match. Returns (matched pairs, unmatched_pred, unmatched_gold)."""
    used_pred: set[int] = set()
    pairs: list[tuple[InvoiceLine, dict[str, object]]] = []
    pred_ids = [_line_identity(_pred_line_values(pl)) for pl in pred_lines]
    for gl in gold_lines:
        gid = _line_identity(gl)
        for i, pl in enumerate(pred_lines):
            if i in used_pred:
                continue
            if pred_ids[i] == gid:
                used_pred.add(i)
                pairs.append((pl, gl))
                break
    unmatched_pred = len(pred_lines) - len(used_pred)
    unmatched_gold = len(gold_lines) - len(pairs)
    return pairs, unmatched_pred, unmatched_gold


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _safe_div(num: float, den: float, default: float = 1.0) -> float:
    return num / den if den else default


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall, 0.0)
    return precision, recall, f1


def _calibration(scores: Sequence[FieldScore]) -> list[tuple[float, float, int]]:
    """Confidence-bin → actual accuracy → n, over fields the engine positively claimed
    (status != MISSING). MISSING carries no meaningful confidence, so it is excluded."""
    hits = [0.0] * _CALIBRATION_BINS
    totals = [0] * _CALIBRATION_BINS
    for fs in scores:
        if fs.status is FieldStatus.MISSING:
            continue
        idx = min(int(fs.confidence * _CALIBRATION_BINS), _CALIBRATION_BINS - 1)
        idx = max(idx, 0)
        totals[idx] += 1
        hits[idx] += 1.0 if fs.passed else 0.0
    out: list[tuple[float, float, int]] = []
    for i in range(_CALIBRATION_BINS):
        if totals[i] == 0:
            continue
        center = (i + 0.5) / _CALIBRATION_BINS
        out.append((center, hits[i] / totals[i], totals[i]))
    return out


_LINE_INDEX_RE = re.compile(r"^lines\[\d+\]\.")


def _normalize_path(path: str) -> str:
    """Collapse ``lines[3].cgst_paise`` → ``line.cgst_paise`` so per-line accuracy pools
    across documents with different line counts."""
    return _LINE_INDEX_RE.sub("line.", path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score(
    predicted: ExtractedInvoice, gold: dict[str, object], *, fixture_id: str = ""
) -> EvalReport:
    """Grade one canonical prediction against its gold record."""
    gold_header = _as_dict(gold.get("header"))
    gold_totals = _as_dict(gold.get("totals"))
    gold_lines = _as_line_list(gold.get("lines"))

    per_field: list[FieldScore] = []

    for spec in _HEADER_SPECS:
        env: Field[object] = getattr(predicted.header, spec.attr)
        per_field.append(
            _score_field(f"header.{spec.attr}", spec, env, gold_header.get(spec.attr)))
    for spec in _TOTALS_SPECS:
        env = getattr(predicted.totals, spec.attr)
        per_field.append(
            _score_field(f"totals.{spec.attr}", spec, env, gold_totals.get(spec.attr)))

    pairs, unmatched_pred, unmatched_gold = _match_lines(predicted.lines, gold_lines)
    line_tp = 0
    for pred_line, gold_line in pairs:
        row_scores: list[FieldScore] = []
        for spec in _LINE_SPECS:
            env = _line_field(pred_line, spec.attr)
            path = f"lines[{pred_line.line_no}].{spec.attr}"
            row_scores.append(_score_field(path, spec, env, gold_line.get(spec.attr)))
        per_field.extend(row_scores)
        if all(rs.passed for rs in row_scores):
            line_tp += 1

    # A matched-but-imperfect row is a substitution: it counts as neither a clean TP nor a
    # clean detection miss, so fold its imperfection into both FP and FN.
    imperfect = len(pairs) - line_tp
    line_fp = unmatched_pred + imperfect
    line_fn = unmatched_gold + imperfect
    line_prf = _prf(line_tp, line_fp, line_fn)

    # Doc is exact iff every header/totals field passes AND every line row is a clean TP
    # with no extra / missing rows.
    scalar_pass = all(fs.passed for fs in per_field if not fs.field_path.startswith("lines["))
    lines_perfect = (
        unmatched_pred == 0 and unmatched_gold == 0 and line_tp == len(gold_lines))
    doc_exact = 1.0 if (scalar_pass and lines_perfect) else 0.0

    field_accuracy = {fs.field_path: fs.partial for fs in per_field}
    calibration = _calibration(per_field)

    # review-flag reality check: a doc SHOULD be flagged when it truly needs a human —
    # either no text layer (needs_ocr), or any REQUIRED field is actually wrong/missing.
    required_failed = any(
        fs.field_path in REQUIRED_FIELD_PATHS and not fs.passed for fs in per_field)
    should_review = bool(gold.get("needs_ocr")) or required_failed
    predicted_review = bool(predicted.review_needed)
    tp = int(should_review and predicted_review)
    fp = int((not should_review) and predicted_review)
    fn = int(should_review and (not predicted_review))
    tn = int((not should_review) and (not predicted_review))
    review_p = _safe_div(tp, tp + fp)
    review_r = _safe_div(tp, tp + fn)

    return EvalReport(
        fixture_id=fixture_id,
        per_field=per_field,
        field_accuracy=field_accuracy,
        line_item_prf=line_prf,
        doc_exact_rate=doc_exact,
        calibration=calibration,
        review_precision_recall=(review_p, review_r),
        _line_counts=(line_tp, line_fp, line_fn),
        _review_counts=(tp, fp, fn, tn),
    )


def aggregate(reports: Sequence[EvalReport]) -> AggregateReport:
    """Pool per-document reports into a gold-set grade (micro-averaged PRF + calibration)."""
    n = len(reports)
    if n == 0:
        return AggregateReport(
            n_docs=0,
            field_accuracy={},
            macro_field_accuracy=0.0,
            line_item_prf=(0.0, 0.0, 0.0),
            doc_exact_rate=0.0,
            calibration=[],
            review_precision_recall=(1.0, 1.0),
        )

    # Field accuracy: mean partial per canonical field (line index collapsed).
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    all_scores: list[FieldScore] = []
    for r in reports:
        for fs in r.per_field:
            key = _normalize_path(fs.field_path)
            sums[key] = sums.get(key, 0.0) + fs.partial
            counts[key] = counts.get(key, 0) + 1
            all_scores.append(fs)
    field_accuracy = {k: sums[k] / counts[k] for k in sums}
    macro = sum(field_accuracy.values()) / len(field_accuracy) if field_accuracy else 0.0

    tp = sum(r._line_counts[0] for r in reports)
    fp = sum(r._line_counts[1] for r in reports)
    fn = sum(r._line_counts[2] for r in reports)
    line_prf = _prf(tp, fp, fn)

    doc_exact_rate = sum(r.doc_exact_rate for r in reports) / n

    rtp = sum(r._review_counts[0] for r in reports)
    rfp = sum(r._review_counts[1] for r in reports)
    rfn = sum(r._review_counts[2] for r in reports)
    review_pr = (_safe_div(rtp, rtp + rfp), _safe_div(rtp, rtp + rfn))

    return AggregateReport(
        n_docs=n,
        field_accuracy=field_accuracy,
        macro_field_accuracy=macro,
        line_item_prf=line_prf,
        doc_exact_rate=doc_exact_rate,
        calibration=_calibration(all_scores),
        review_precision_recall=review_pr,
    )


def check_thresholds(agg: AggregateReport) -> list[str]:
    """Return a list of frozen-minimum violations (empty == the regression gate is green)."""
    violations: list[str] = []
    if agg.doc_exact_rate < FROZEN_MIN_DOC_EXACT_RATE:
        violations.append(
            f"doc_exact_rate {agg.doc_exact_rate:.3f} < {FROZEN_MIN_DOC_EXACT_RATE}")
    if agg.macro_field_accuracy < FROZEN_MIN_FIELD_ACCURACY:
        violations.append(
            f"macro_field_accuracy {agg.macro_field_accuracy:.3f} < {FROZEN_MIN_FIELD_ACCURACY}")
    if agg.line_item_prf[2] < FROZEN_MIN_LINE_F1:
        violations.append(f"line_f1 {agg.line_item_prf[2]:.3f} < {FROZEN_MIN_LINE_F1}")
    if agg.review_precision_recall[1] < FROZEN_MIN_REVIEW_RECALL:
        violations.append(
            f"review_recall {agg.review_precision_recall[1]:.3f} < {FROZEN_MIN_REVIEW_RECALL}")
    return violations


# ---------------------------------------------------------------------------
# Gold-dict coercion (defensive: JSON gives us plain dicts/lists)
# ---------------------------------------------------------------------------


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_line_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]
