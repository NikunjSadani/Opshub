"""Engine-neutral canonical schema for extracted GST invoices (Phase 2).

ORM-free dataclasses — the SEAM every extractor engine maps onto (`TextLayerExtractor`
now; a paid engine later, behind the same shapes). House conventions from the challan
module: **money is ALWAYS integer paise** (never float); dates are Python `date`; a
per-field ENVELOPE carries the verbatim source + provenance + a CALIBRATED confidence +
a review status, so both the review UI and the eval harness can reason about every value.

`CANONICAL_SCHEMA_VERSION` travels inside every `ExtractedInvoice` so a stored extraction
can be re-scored against the gold set it was produced under, and a schema bump is
detectable at read time.
"""
import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Generic, TypeVar

CANONICAL_SCHEMA_VERSION = "gst_invoice/1.0.0"  # bump on any field add/rename/semantics change

# Tolerances (F5, locked) — load-bearing for both the review gate and the eval scorer.
TOTALS_TOL_PAISE = 0        # round-off is its own explicit line, so totals must match exactly
LINE_TAX_TOL_PAISE = 100    # ₹1 — absorbs per-line vendor rounding

# A document is flagged for review if any of these is MISSING/LOW_CONFIDENCE (plus the
# arithmetic / GSTIN-checksum / needs-ocr triggers the extractor adds). `po_ref`,
# `amount_in_words`, buyer/address fields are optional — MISSING there is not a trigger.
REQUIRED_FIELD_PATHS = (
    "header.supplier_gstin",
    "header.invoice_number",
    "header.invoice_date",
    "totals.total_taxable_paise",
    "totals.grand_total_paise",
)


class FieldStatus(str, enum.Enum):
    OK = "OK"                          # extracted + passed its checks
    LOW_CONFIDENCE = "LOW_CONFIDENCE"  # extracted but weakly-sourced / arithmetic-uncorroborated
    MISSING = "MISSING"                # not found in the document (value_normalized is None)
    CORRECTED = "CORRECTED"            # a human overwrote the machine value (source_engine="human")


T = TypeVar("T")  # int (paise) | Decimal (qty / gst%) | date | str


@dataclass
class Field(Generic[T]):
    """One canonical field: the typed value + the verbatim substring + provenance + status.

    `value_normalized is None` iff `status is MISSING`. `confidence` is 0..1 and CALIBRATED
    (see the extractor's ladder + the eval reliability table) — not a raw heuristic score.
    """

    value_normalized: "T | None"
    value_raw: str
    confidence: float
    source_engine: str                                  # "text_layer/1.0" | "docai/1.0" | "human"
    page: int | None = None                             # 1-based
    bbox: tuple[float, float, float, float] | None = None  # (x0, y0, x1, y1) PDF points
    status: FieldStatus = FieldStatus.MISSING


# Typed aliases so call sites read straight.
MoneyField = Field[int]      # ALWAYS integer paise
DateField = Field[date]
NumField = Field[Decimal]    # qty, gst_rate (percent)
TextField = Field[str]


@dataclass
class InvoiceHeader:
    supplier_name: TextField
    supplier_gstin: TextField      # validated via masterdata.normalize.valid_gstin
    supplier_address: TextField
    buyer_name: TextField
    buyer_gstin: TextField         # extracted + stored; not hard-validated in v1 (F3 deferred)
    buyer_address: TextField
    invoice_number: TextField      # the VENDOR's own number (exact-match id)
    invoice_date: DateField
    place_of_supply: TextField     # "State (NN)" — drives intra/inter-state sanity
    po_ref: TextField              # optional; MISSING is legal, never a review trigger


@dataclass
class InvoiceLine:
    line_no: int                   # 1-based, OUR sequence (structural, not a Field)
    description: TextField
    hsn_sac: TextField
    quantity: NumField
    unit: TextField
    unit_rate_paise: MoneyField
    taxable_paise: MoneyField      # pre-tax line value
    gst_rate: NumField             # percent, e.g. Decimal("18.00")
    cgst_paise: MoneyField
    sgst_paise: MoneyField
    igst_paise: MoneyField
    line_total_paise: MoneyField   # taxable + cgst + sgst + igst


@dataclass
class InvoiceTotals:
    total_taxable_paise: MoneyField
    total_cgst_paise: MoneyField
    total_sgst_paise: MoneyField
    total_igst_paise: MoneyField
    round_off_paise: MoneyField    # SIGNED (may be negative) — the explicit rounding line
    grand_total_paise: MoneyField
    amount_in_words: TextField


@dataclass
class ArithmeticChecks:
    lines_sum_matches_taxable: bool   # Σ line taxable == total taxable (±TOTALS_TOL_PAISE)
    per_line_tax_consistent: bool     # taxable×rate/100 ≈ cgst+sgst+igst per line (±LINE_TAX_TOL)
    totals_add_to_grand: bool         # taxable + taxes + round_off == grand_total
    supply_type_consistent: bool      # intra→CGST&SGST/IGST=0 ; inter→IGST/CGST&SGST=0
    max_abs_delta_paise: int          # worst residual across all checks (drives review)


@dataclass
class ExtractedInvoice:
    schema_version: str               # == CANONICAL_SCHEMA_VERSION at extraction time
    doc_type: str                     # "gst_invoice"
    source_engine: str
    page_count: int
    needs_ocr: bool                   # true → no text layer; fields MISSING, NOT a failure
    review_needed: bool               # any required field weak/missing OR arithmetic fails
    review_reasons: list[str]         # human-readable, e.g. "supplier_gstin failed checksum"
    header: InvoiceHeader
    lines: list[InvoiceLine]          # FIRST-CLASS
    totals: InvoiceTotals
    arithmetic: ArithmeticChecks
    raw_text: str                     # full text layer, retained for audit / re-extraction
    content_hash: str                 # sha256 of normalized raw_text
    dedup_key: str                    # sha256(norm_gstin | inv_no | date | grand_total_paise)
