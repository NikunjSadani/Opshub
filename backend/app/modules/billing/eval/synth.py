"""Synthetic SALES-invoice + PO-match gold generator for the billing eval set.

Two self-labelling generators, both known BY CONSTRUCTION (never re-parsed):

* :func:`build_sales_invoice_pdf` renders a byte-stable **client** invoice PDF and returns
  ``(pdf_bytes, gold)``. The roles are INVERTED vs the expense (inbound) side: the supplier
  is *us* (Tech Gifsy) and the buyer is the *client*. Rendering reuses the expense reportlab
  builder verbatim (``rl_config.invariant`` pins dates/ids, so committed fixtures are
  byte-stable across regenerations); only the identity framing differs, so the returned gold
  matches the shape :func:`app.modules.billing.eval.scorers.score` grades against.

* :func:`build_match_scenario` fabricates the PO-MATCH gold: a set of extracted invoice
  lines + a candidate purchase order (its ``POLineItem``-shaped lines) + the **expected**
  line→PO-line mapping. One scenario exercises every match archetype the matcher must get
  right on a money path:

    1. exact product-code match,
    2. HSN + description fuzzy match (no code on the invoice line),
    3. a genuinely-unmatched line (no PO line exists) → gold ``None``,
    4. a quantity/price-off line (same product, discrepant qty & rate — still that PO line),
    5. an ambiguous 2-candidate case (two PO lines share HSN + description prefix).

Money is integer paise; quantities are ``Decimal`` (house conventions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.modules.expense.eval.synth import (
    GSTIN_BUYER_KA,
    GSTIN_BUYER_MH,
    GSTIN_SUPPLIER_MH,
    InvoiceSpec,
    LineSpec,
    build_invoice_pdf,
)

# --- Fixed OUR-side (supplier) identity: a sales invoice is issued BY us. -------------------
SUPPLIER_US_NAME = "Tech Gifsy Solutions Limited"
SUPPLIER_US_GSTIN = GSTIN_SUPPLIER_MH  # Maharashtra (27...)
SUPPLIER_US_ADDRESS = "3rd Floor, Tech Park, Andheri East, Mumbai, Maharashtra 400069"

# Sample CLIENT (buyer) identities. Intra-state buyer shares our state (27); the inter-state
# buyer sits in Karnataka (29) so the IGST path is exercised.
CLIENT_INTRA_NAME = "Bajaj Consumer Care Limited"
CLIENT_INTRA_GSTIN = GSTIN_BUYER_MH
CLIENT_INTRA_ADDRESS = "Plot 12, MIDC, Waluj, Aurangabad, Maharashtra 431136"
CLIENT_INTER_NAME = "Britannia Industries Limited"
CLIENT_INTER_GSTIN = GSTIN_BUYER_KA
CLIENT_INTER_ADDRESS = "5/1A Hosur Road, Bengaluru, Karnataka 560095"


# ---------------------------------------------------------------------------
# Sales-invoice spec (thin sales framing over the expense reportlab renderer)
# ---------------------------------------------------------------------------


@dataclass
class SalesLineSpec:
    """One priced line on a sales invoice. Carries the product identity a matcher keys on
    (``product_code`` / ``hsn_sac`` / ``description``) alongside the money fields."""

    description: str
    hsn_sac: str
    quantity: Decimal
    unit: str
    unit_rate_paise: int
    gst_rate: Decimal            # percent, e.g. Decimal("18")
    product_code: str | None = None


@dataclass
class SalesInvoiceSpec:
    """A client (outbound) invoice: supplier is us, buyer is the client."""

    buyer_name: str
    buyer_gstin: str
    buyer_address: str
    invoice_number: str
    invoice_date: date
    place_of_supply: str
    lines: list[SalesLineSpec]
    intra_state: bool
    po_ref: str | None = None
    supplier_name: str = SUPPLIER_US_NAME
    supplier_gstin: str = SUPPLIER_US_GSTIN
    supplier_address: str = SUPPLIER_US_ADDRESS
    round_off_paise: int = 0
    two_page: bool = False
    page_break_after: int = 0
    scanned: bool = False


def _to_expense_spec(spec: SalesInvoiceSpec) -> InvoiceSpec:
    """Lower a :class:`SalesInvoiceSpec` onto the expense :class:`InvoiceSpec` the reportlab
    builder consumes. Roles are already sales-framed (supplier=us, buyer=client); the
    ``product_code`` knob is a match-only signal and is intentionally NOT rendered here."""
    return InvoiceSpec(
        supplier_name=spec.supplier_name,
        supplier_gstin=spec.supplier_gstin,
        supplier_address=spec.supplier_address,
        buyer_name=spec.buyer_name,
        buyer_gstin=spec.buyer_gstin,
        buyer_address=spec.buyer_address,
        invoice_number=spec.invoice_number,
        invoice_date=spec.invoice_date,
        place_of_supply=spec.place_of_supply,
        lines=[
            LineSpec(
                description=ls.description,
                hsn_sac=ls.hsn_sac,
                quantity=ls.quantity,
                unit=ls.unit,
                unit_rate_paise=ls.unit_rate_paise,
                gst_rate=ls.gst_rate,
            )
            for ls in spec.lines
        ],
        intra_state=spec.intra_state,
        po_ref=spec.po_ref,
        round_off_paise=spec.round_off_paise,
        two_page=spec.two_page,
        page_break_after=spec.page_break_after,
        scanned=spec.scanned,
        _title="Tax Invoice",
    )


def build_sales_invoice_pdf(spec: SalesInvoiceSpec) -> tuple[bytes, dict[str, object]]:
    """Render ``spec`` to a byte-stable client-invoice PDF and return ``(pdf_bytes, gold)``.

    ``gold`` matches the canonical shape :func:`app.modules.billing.eval.scorers.score`
    expects (identical to the expense gold shape — same GST-invoice canonical schema)."""
    return build_invoice_pdf(_to_expense_spec(spec))


# ---------------------------------------------------------------------------
# PO-match gold
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class POLine:
    """A candidate PO line, shaped after :class:`app.modules.sales_orders.models.POLineItem`
    (only the fields a matcher reads)."""

    id: int
    product_id: int
    product_code: str
    hsn_sac: str
    description: str
    ordered_qty: Decimal
    sell_price_paise: int


@dataclass(frozen=True)
class ExtractedMatchLine:
    """An extracted sales-invoice line offered to the matcher. ``product_code`` may be absent
    (the invoice printed only a description) — the fuzzy path must then earn the match."""

    line_no: int
    description: str
    hsn_sac: str
    quantity: Decimal
    unit_rate_paise: int
    product_code: str | None = None


@dataclass
class MatchScenario:
    """Extracted lines + a candidate PO + the gold line→PO-line mapping.

    ``gold_mapping`` maps each invoice ``line_no`` to the ``POLine.id`` it should resolve to,
    or ``None`` when the line is genuinely unmatched (no PO line exists for it)."""

    scenario_id: str
    invoice_lines: list[ExtractedMatchLine]
    po_lines: list[POLine]
    gold_mapping: dict[int, int | None]
    case_labels: dict[int, str] = field(default_factory=dict)


# Human-readable archetype label per invoice line_no in the canonical scenario.
MATCH_CASE_LABELS: dict[int, str] = {
    1: "exact_product_code",
    2: "hsn_description_fuzzy",
    3: "genuinely_unmatched",
    4: "quantity_price_off",
    5: "ambiguous_two_candidate",
}


def build_match_scenario() -> MatchScenario:
    """Build the canonical PO-match scenario covering all five archetypes.

    The candidate PO holds four priced lines; ``BRK-300`` and ``BRK-301`` deliberately share
    HSN ``848180`` and the "Brass Bracket" description prefix so line 5 is a genuine 2-way
    ambiguity that only the "Light Duty" token resolves to ``BRK-301``.
    """
    po_lines = [
        POLine(
            id=101, product_id=1, product_code="WID-100", hsn_sac="847130",
            description="Widget Assembly Type A", ordered_qty=Decimal("100"),
            sell_price_paise=15000,
        ),
        POLine(
            id=102, product_id=2, product_code="GSK-200", hsn_sac="401693",
            description="Rubber Gasket 20mm", ordered_qty=Decimal("50"),
            sell_price_paise=5000,
        ),
        POLine(
            id=103, product_id=3, product_code="BRK-300", hsn_sac="848180",
            description="Brass Bracket Heavy Duty", ordered_qty=Decimal("200"),
            sell_price_paise=8000,
        ),
        POLine(
            id=104, product_id=4, product_code="BRK-301", hsn_sac="848180",
            description="Brass Bracket Light Duty", ordered_qty=Decimal("200"),
            sell_price_paise=7000,
        ),
    ]
    invoice_lines = [
        # 1) exact product-code match → PO 101.
        ExtractedMatchLine(
            line_no=1, product_code="WID-100", hsn_sac="847130",
            description="Widget Assembly Type A", quantity=Decimal("100"),
            unit_rate_paise=15000,
        ),
        # 2) no code; HSN + fuzzy description ("20 mm" vs "20mm") → PO 102.
        ExtractedMatchLine(
            line_no=2, product_code=None, hsn_sac="401693",
            description="Rubber Gasket 20 mm", quantity=Decimal("50"),
            unit_rate_paise=5000,
        ),
        # 3) genuinely unmatched: no PO line for this product at all → gold None.
        ExtractedMatchLine(
            line_no=3, product_code="ZZZ-999", hsn_sac="999999",
            description="On-site Consulting Services", quantity=Decimal("1"),
            unit_rate_paise=250000,
        ),
        # 4) quantity/price-off: same product (code match) but qty 180≠200 and rate 8500≠8000.
        #    Still resolves to PO 103 — the discrepancy is surfaced downstream, not unmatched.
        ExtractedMatchLine(
            line_no=4, product_code="BRK-300", hsn_sac="848180",
            description="Brass Bracket Heavy Duty", quantity=Decimal("180"),
            unit_rate_paise=8500,
        ),
        # 5) ambiguous: no code; HSN 848180 + "Brass Bracket" matches BOTH 103 and 104.
        #    The "Light Duty" token is the only disambiguator → correct answer is PO 104.
        ExtractedMatchLine(
            line_no=5, product_code=None, hsn_sac="848180",
            description="Brass Bracket Light Duty", quantity=Decimal("200"),
            unit_rate_paise=7000,
        ),
    ]
    gold_mapping: dict[int, int | None] = {1: 101, 2: 102, 3: None, 4: 103, 5: 104}
    return MatchScenario(
        scenario_id="canonical_po_match",
        invoice_lines=invoice_lines,
        po_lines=po_lines,
        gold_mapping=gold_mapping,
        case_labels=dict(MATCH_CASE_LABELS),
    )
