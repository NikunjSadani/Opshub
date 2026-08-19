"""Evaluation harness for the Expense/Invoice extractor.

Two halves that stay decoupled from the engine under test:

* ``scorers`` — field-typed scorers that grade a canonical ``ExtractedInvoice``
  prediction against a plain-dict gold record, producing an ``EvalReport`` (and an
  ``AggregateReport`` across a gold set). It imports only ``canonical`` + the shared
  masterdata normalizers, never the extractor, so it can score ANY engine's output.
* ``synth`` — a reportlab GST-invoice PDF generator whose expected canonical values are
  known BY CONSTRUCTION, so the gold set is self-labelling and reproducible.

The real extractor-vs-gold accuracy test is wired at integration time (the extractor is
built in parallel); here we grade CONSTRUCTED predictions to prove the scorers.
"""
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
    "aggregate",
    "check_thresholds",
    "score",
]
