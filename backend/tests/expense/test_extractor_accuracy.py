"""Integration: the REAL text-layer extractor scored against the eval GOLD set.

The "eval harness before the engine is trusted" payoff (DESIGN §4). It runs the actual
`TextLayerExtractor` over the independently-generated, labelled gold invoices and asserts
the frozen accuracy thresholds. Because the extractor and the gold fixtures were built by
separate streams, this is a genuine generalization test — and it becomes a regression gate
as the extractor evolves.
"""
import json
import pathlib

from app.modules.expense.eval.scorers import aggregate, check_thresholds, score
from app.modules.expense.extractor import get_extractor

_GOLD = pathlib.Path(__file__).parent / "gold"

# Tally-ENGINE-ONLY fixtures: their glued-token layout (adjacent tax cells that pdfplumber
# merges into one word) is resolved by the dedicated TallyInvoiceExtractor's item-row
# token-splitter, NOT the generic text-layer engine this frozen-threshold harness scores. They
# have their own exact-paise assertions in tests/expense/test_tally.py
# (test_gold_tally_glued_igst_line_exact), so they are excluded here to keep this a text-layer
# accuracy gate rather than dragging the aggregate down on a document the text-layer engine was
# never meant to parse.
_TALLY_ONLY_FIXTURES = frozenset({"tally_glued_igst"})


def _fixture_dirs() -> list[pathlib.Path]:
    return sorted(d for d in _GOLD.iterdir()
                  if d.is_dir() and (d / "expected.json").is_file()
                  and d.name not in _TALLY_ONLY_FIXTURES)


def test_extractor_meets_frozen_thresholds_on_gold_set() -> None:
    extractor = get_extractor("text_layer")
    reports = []
    for d in _fixture_dirs():
        pdf = (d / "source.pdf").read_bytes()
        gold = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        reports.append(score(extractor.extract(pdf), gold, fixture_id=d.name))
    violations = check_thresholds(aggregate(reports))
    assert not violations, f"extractor accuracy regressed vs the gold set: {violations}"


def test_scanned_fixture_flags_needs_ocr() -> None:
    extractor = get_extractor("text_layer")
    pdf = (_GOLD / "scanned_no_text" / "source.pdf").read_bytes()
    result = extractor.extract(pdf)
    assert result.needs_ocr is True
    assert result.review_needed is True
