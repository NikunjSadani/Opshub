"""Canonical, ORM-free contracts shared across the challan pipeline.

These dataclasses are the seams between the pieces (parse/validate, orchestrate,
render) so each can be built and tested in isolation. The shape mirrors the real
`L/433` challan: a single fixed consignor, a Brand->State consignee, a 5-field
ship-to block, and a line table `Sl.No | Product | HSN | Qty | Rate | Amt (incl
Tax)` (no UOM/GST% columns).

Money is integer PAISE end to end. `rate` may be free text (e.g. "as per
contract"); `amount` is OPTIONAL — a line/challan with no amount prints
value-free. When an amount IS given it is tax-INCLUSIVE: amount = rate*qty*(1+gst).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# --- Excel template column keys (canonical, lower-snake). Header match is
# case-insensitive + whitespace-trimmed in the parser. The consignor is a single
# fixed entity (master data), so it is NOT an upload column. ---
CHALLAN_COLUMNS: tuple[str, ...] = (
    "group",             # challan grouping key: rows sharing it -> one challan
    "brand",             # resolves the consignee via (brand, ship_to_state)
    "ship_to_state",     # drives the consignee registry lookup
    "ship_to_name",      # "Detail of Shipment to" block (5 fields) ...
    "ship_to_address",
    "ship_to_enterprise",  # Enterprise Name
    "ship_to_number",      # contact Number
    "ship_to_contact",     # Contact Person Name (optional)
    "challan_date",      # DD-MM-YYYY or an Excel date
    "description",       # line item (Product Descriptions)
    "hsn",
    "quantity",
    "rate",              # optional; may be free text; pre-tax unit rate
    "amount",            # optional; tax-INCLUSIVE line amount in rupees -> paise
    "gst_rate",          # optional; if present must match the HSN's rate
    "po_number",         # optional (not printed on L/433)
    "invoice_number",    # optional (not printed on L/433)
)
REQUIRED_COLUMNS: tuple[str, ...] = (
    "group", "brand", "ship_to_state", "ship_to_name", "ship_to_address",
    "challan_date", "description", "hsn", "quantity",
)
# Fields that must be identical for every row within one group (challan-level).
GROUP_CONSISTENT_FIELDS: tuple[str, ...] = (
    "brand", "ship_to_state", "ship_to_name", "ship_to_address",
    "ship_to_enterprise", "ship_to_number", "ship_to_contact",
    "challan_date", "po_number", "invoice_number",
)


@dataclass
class RawRow:
    """One spreadsheet data row: 1-based `row_number` (incl. header) + raw cells."""

    row_number: int
    cells: dict[str, str]  # column key -> trimmed string cell value ("" if blank)


@dataclass
class RowError:
    """A single human-readable validation problem for the English error report."""

    row_number: int
    column: str  # "" for row/group-level problems
    message: str


@dataclass
class ParsedLine:
    description: str
    hsn: str
    quantity: Decimal
    rate_text: str            # verbatim (may be non-numeric)
    rate_paise: int | None    # parsed if numeric, else None
    amount_paise: int | None  # tax-inclusive; None = value-free line
    gst_rate: Decimal | None  # authoritative HSN rate (used for the tax-inclusive amount)


@dataclass
class ParsedChallan:
    group_key: str
    brand: str
    ship_to_state: str
    ship_to_name: str
    ship_to_address: str
    ship_to_enterprise: str
    ship_to_number: str
    ship_to_contact: str
    challan_date: date
    po_number: str
    invoice_number: str
    lines: list[ParsedLine] = field(default_factory=list)

    @property
    def total_paise(self) -> int | None:
        """Sum of line amounts; None if no line carries an amount (value-free)."""
        amounts = [line.amount_paise for line in self.lines if line.amount_paise is not None]
        return sum(amounts) if amounts else None

    @property
    def total_qty(self) -> Decimal:
        return sum((line.quantity for line in self.lines), Decimal(0))


@dataclass
class ValidationResult:
    """Either errors (no challans built) or the parsed challans (no errors)."""

    errors: list[RowError] = field(default_factory=list)
    challans: list[ParsedChallan] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- Render seam: an ORM-free view of a fully-resolved, numbered challan. ---

@dataclass
class ConsignorView:
    name: str
    warehouse_address: str
    gstin: str
    phone: str


@dataclass
class ConsigneeView:
    name: str
    address: str
    gstin: str
    state_label: str  # "Bihar (10)"
    phone: str


@dataclass
class ShipToView:
    name: str
    address: str
    enterprise: str
    number: str
    contact_person: str


@dataclass
class LineView:
    line_no: int
    description: str
    hsn: str
    quantity: str  # preformatted for display
    rate: str      # display text ("" if none)
    amount: str    # display rupees ("" if value-free)


@dataclass
class ChallanView:
    number: str        # GIF/DC/26-27/L/000189
    challan_date: str  # display "28th July 2026"
    consignor: ConsignorView
    consignee: ConsigneeView
    ship_to: ShipToView
    lines: list[LineView]
    total_qty: str
    total_amount: str  # "" when value-free
    show_amount: bool  # False -> Rate/Amt columns + total are blank
