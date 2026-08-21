"""Evaluation harness for the billing (sales) capture + PO-match flow.

Two engine-decoupled halves (the "harness before the engine" discipline):

* ``scorers`` — grades predictions, never the live engine. The EXTRACTION scorers are the
  expense field/line scorers reused unchanged (same GST-invoice canonical, inverted roles);
  the MATCH scorer is new: it grades a predicted line→PO-line mapping and guards the
  money-critical ``false_auto_match_rate`` against a frozen ceiling.
* ``synth`` — a reportlab SALES-invoice PDF generator (supplier=us, buyer=client) whose
  canonical gold is known BY CONSTRUCTION, plus a ``MatchScenario`` generator whose gold
  line→PO mapping is likewise self-labelling.

The real extractor/matcher-vs-gold accuracy test is wired at integration time (both engines
are built in parallel); here we grade CONSTRUCTED predictions to prove the scorers.
"""
from app.modules.billing.eval.scorers import (
    AggregateReport,
    EvalReport,
    FieldScore,
    Kind,
    MatchAggregate,
    MatchReport,
    aggregate,
    aggregate_match,
    check_match_thresholds,
    check_thresholds,
    score,
    score_match,
)
from app.modules.billing.eval.synth import (
    ExtractedMatchLine,
    MatchScenario,
    POLine,
    SalesInvoiceSpec,
    SalesLineSpec,
    build_match_scenario,
    build_sales_invoice_pdf,
)

__all__ = [
    "AggregateReport",
    "EvalReport",
    "ExtractedMatchLine",
    "FieldScore",
    "Kind",
    "MatchAggregate",
    "MatchReport",
    "MatchScenario",
    "POLine",
    "SalesInvoiceSpec",
    "SalesLineSpec",
    "aggregate",
    "aggregate_match",
    "build_match_scenario",
    "build_sales_invoice_pdf",
    "check_match_thresholds",
    "check_thresholds",
    "score",
    "score_match",
]
