"""Canonical, ORM-free contracts shared across the challan pipeline.

These dataclasses are the seams between the three pieces (parse/validate,
orchestrate, render) so each can be built and tested in isolation:

  parse_workbook(bytes) -> (list[RawRow], list[RowError])   # structural
  validate(db, rows)    -> ValidationResult                 # semantic (master data)
  ...orchestrate: reserve -> render(ChallanView) -> issue...

Money is integer PAISE end to end (BigInt in the DB). `rate` may legitimately be
free text (e.g. "as per contract"), so it is carried as text plus an optional
parsed paise value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# --- Excel template column keys (canonical, lower-snake). Header match is
# case-insensitive + whitespace-trimmed in the parser. ---
CHALLAN_COLUMNS: tuple[str, ...] = (
    "group",          # challan grouping key: rows sharing it -> one challan
    "consignor",      # consignor NAME (resolves to an active Consignor)
    "brand",          # resolves the consignee via (brand, ship_to_state)
    "ship_to_name",
    "ship_to_address",
    "ship_to_state",  # drives the consignee registry lookup
    "challan_date",   # DD-MM-YYYY or an Excel date
    "description",    # line item
    "hsn",
    "quantity",
    "uom",            # optional (default NOS)
    "rate",           # optional; may be free text
    "amount",         # line amount in rupees -> stored as paise
    "gst_rate",       # optional; if present, validated against the HSN's rate
    "po_number",      # optional
    "invoice_number",  # optional
)
REQUIRED_COLUMNS: tuple[str, ...] = (
    "group", "consignor", "brand", "ship_to_name", "ship_to_address",
    "ship_to_state", "challan_date", "description", "hsn", "quantity", "amount",
)
# Fields that must be identical for every row within one group (challan-level).
GROUP_CONSISTENT_FIELDS: tuple[str, ...] = (
    "consignor", "brand", "ship_to_name", "ship_to_address", "ship_to_state",
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
    uom: str
    rate_text: str          # verbatim (may be non-numeric)
    rate_paise: int | None  # parsed if numeric, else None
    amount_paise: int
    gst_rate: Decimal | None


@dataclass
class ParsedChallan:
    group_key: str
    consignor_name: str
    brand: str
    ship_to_name: str
    ship_to_address: str
    ship_to_state: str
    challan_date: date
    po_number: str
    invoice_number: str
    lines: list[ParsedLine] = field(default_factory=list)

    @property
    def total_paise(self) -> int:
        return sum(line.amount_paise for line in self.lines)


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
class PartyView:
    name: str
    gstin: str
    state: str
    address: str


@dataclass
class LineView:
    line_no: int
    description: str
    hsn: str
    quantity: str      # preformatted for display
    uom: str
    rate: str          # display text
    amount: str        # display rupees, e.g. "1,234.00"
    gst_rate: str


@dataclass
class ChallanView:
    number: str        # GIF/DC/26-27/L/000189
    challan_date: str  # display DD-MM-YYYY
    consignor: PartyView
    consignee: PartyView
    ship_to_name: str
    ship_to_address: str
    ship_to_state: str
    po_number: str
    invoice_number: str
    eway_required: bool
    lines: list[LineView]
    total_amount: str  # display rupees
