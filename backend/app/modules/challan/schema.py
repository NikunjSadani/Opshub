"""Canonical, ORM-free contracts shared across the challan pipeline.

These dataclasses are the seams between the pieces (parse/validate, orchestrate,
render) so each can be built and tested in isolation. The shape mirrors the real
`L/433` challan: a single fixed consignor, an INLINE consignee resolved by GSTIN
(the golden-record master, `masterdata.consignee_master`), a structured ship-to
block, a referenced Project (`<CLIENT_CODE>-<seq>`, must pre-exist + be Active),
and a line table `Sl.No | Product | HSN | Qty | Rate | Amt (incl Tax)`.

Money is integer PAISE end to end. `rate` may be free text (e.g. "as per
contract"); `amount` is OPTIONAL — a line/challan with no amount prints
value-free. When an amount IS given it is tax-INCLUSIVE: amount = rate*qty*(1+gst).

--- Increment 15 changes vs the original Brand->State model ---
* `brand` is DROPPED — the inline consignee GSTIN fully identifies the bill-to.
* The consignee is TYPED INLINE (name / address / GSTIN / ...) and resolved
  against the GSTIN-keyed golden record (auto-create on first sight; a known
  GSTIN with differing details yields a non-blocking DEVIATION warning and the
  STORED record is snapshotted, never the typed value).
* `Project ID` references an existing ACTIVE project and is printed on the doc.
* The ship-to address is captured SPLIT (line1/line2/city/pincode) and PRINTED
  joined into one block.
* Grouping is an explicit `Challan Group` column (rows sharing it -> one
  challan). Two different groups sharing the same destination + date are a
  non-blocking WARNING surfaced to the operator (a possible split/duplicate).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# --- Excel template columns (canonical, lower-snake keys). The parser matches
# each uploaded header case-insensitively + whitespace-collapsed against the
# canonical key, its friendly display header, and a small alias set (see
# `COLUMN_ALIASES` / `header_to_key`). The consignor is a single fixed entity
# (master data), so it is NOT an upload column. Order = template column order. ---
CHALLAN_COLUMNS: tuple[str, ...] = (
    "challan_group",           # grouping key: rows sharing it -> one challan
    "project_id",              # references an ACTIVE Project (<CODE>-<seq>); NOT printed
    "ship_to_enterprise",      # ship-to block ("Detail of Shipment to") ...
    "ship_to_name",
    "ship_to_address_line1",
    "ship_to_address_line2",
    "ship_to_city",
    "ship_to_state",
    "ship_to_pincode",
    "ship_to_phone",
    "consignee_name",          # inline consignee (resolved/snapshotted by GSTIN) ...
    "consignee_address_line1",
    "consignee_address_line2",
    "consignee_pincode",
    "consignee_state",
    "consignee_phone",
    "consignee_gstin",         # the golden-record key
    "challan_date",            # DD-MM-YYYY or an Excel date
    "description",             # line item (Product Descriptions)
    "hsn",
    "quantity",
    "rate",                    # optional; may be free text; pre-tax unit rate
    "amount",                  # optional; tax-INCLUSIVE line amount in rupees -> paise
    "gst_rate",                # optional; if present must match the HSN's rate
    "po_number",               # optional; printed as "PO No." when present
    "invoice_number",          # optional (printed above the challan number)
)

# Friendly, human-readable header each column is written with in the downloaded
# template. The parser accepts these (and the raw keys, and `COLUMN_ALIASES`).
COLUMN_HEADERS: dict[str, str] = {
    "challan_group": "Challan Group",
    "project_id": "Project ID",
    "ship_to_enterprise": "Enterprise Name",
    "ship_to_name": "Ship-to Name",
    "ship_to_address_line1": "Ship-to Address Line 1",
    "ship_to_address_line2": "Ship-to Address Line 2",
    "ship_to_city": "City",
    "ship_to_state": "Ship-to State",
    "ship_to_pincode": "Ship-to Pincode",
    "ship_to_phone": "Ship-to Phone",
    "consignee_name": "Consignee Name",
    "consignee_address_line1": "Consignee Address Line 1",
    "consignee_address_line2": "Consignee Address Line 2",
    "consignee_pincode": "Consignee Pincode",
    "consignee_state": "Consignee State",
    "consignee_phone": "Consignee Phone",
    "consignee_gstin": "Consignee GSTIN",
    "challan_date": "Challan Date",
    "description": "Description",
    "hsn": "HSN",
    "quantity": "Quantity",
    "rate": "Rate",
    "amount": "Amount",
    "gst_rate": "GST Rate",
    "po_number": "PO Number",
    "invoice_number": "Invoice Number",
}

# Extra accepted header spellings (normalized: whitespace-collapsed + lower-cased)
# -> canonical key. Covers common variants so a hand-adjusted sheet still parses.
# NOTE: a bare "pin code" is deliberately NOT aliased — it is ambiguous between
# ship-to and consignee, so the template uses the two disambiguated headers.
_EXTRA_ALIASES: dict[str, str] = {
    "ship_to_address line 1": "ship_to_address_line1",
    "ship_to_address line 2": "ship_to_address_line2",
    "ship-to address line 1": "ship_to_address_line1",
    "ship-to address line 2": "ship_to_address_line2",
    "address line 1": "ship_to_address_line1",
    "address line 2": "ship_to_address_line2",
    "city": "ship_to_city",
    "ship_to_phone number": "ship_to_phone",
    "ship-to phone number": "ship_to_phone",
    "phone number": "ship_to_phone",
    "consignee address line 1": "consignee_address_line1",
    "consignee address line 2": "consignee_address_line2",
    "state": "consignee_state",
    "phone no.": "consignee_phone",
    "phone no": "consignee_phone",
    "gst no": "consignee_gstin",
    "gst no.": "consignee_gstin",
    "gstin": "consignee_gstin",
}


def _norm_header(raw: str) -> str:
    """Whitespace-collapse + lower-case a header cell for tolerant matching."""
    return " ".join(raw.split()).lower()


# Full lookup: canonical key, friendly header, and extra aliases -> canonical key.
COLUMN_ALIASES: dict[str, str] = {}
for _key in CHALLAN_COLUMNS:
    COLUMN_ALIASES[_norm_header(_key)] = _key
    COLUMN_ALIASES[_norm_header(COLUMN_HEADERS[_key])] = _key
for _alias, _key in _EXTRA_ALIASES.items():
    COLUMN_ALIASES.setdefault(_norm_header(_alias), _key)


def header_to_key(raw: str) -> str | None:
    """Map an uploaded header cell to its canonical column key, or None if unknown."""
    return COLUMN_ALIASES.get(_norm_header(raw))


REQUIRED_COLUMNS: tuple[str, ...] = (
    "challan_group", "project_id",
    "ship_to_name", "ship_to_address_line1", "ship_to_state",
    "consignee_name", "consignee_gstin",
    "challan_date", "description", "hsn", "quantity",
)
# Fields that must be identical for every row within one group (challan-level):
# everything except the per-line item fields (description/hsn/qty/rate/amount/gst).
GROUP_CONSISTENT_FIELDS: tuple[str, ...] = (
    "project_id",
    "ship_to_enterprise", "ship_to_name", "ship_to_address_line1",
    "ship_to_address_line2", "ship_to_city", "ship_to_state", "ship_to_pincode",
    "ship_to_phone",
    "consignee_name", "consignee_address_line1", "consignee_address_line2",
    "consignee_pincode", "consignee_state", "consignee_phone", "consignee_gstin",
    "challan_date", "po_number", "invoice_number",
)
# The subset of challan-level fields that together define a shipment's identity;
# two DIFFERENT groups sharing the same tuple are flagged as a possible split.
COLLISION_FIELDS: tuple[str, ...] = (
    "consignee_gstin", "ship_to_name", "ship_to_address_line1",
    "ship_to_city", "ship_to_state", "challan_date",
)


@dataclass
class RawRow:
    """One spreadsheet data row: 1-based `row_number` (incl. header) + raw cells."""

    row_number: int
    cells: dict[str, str]  # column key -> trimmed string cell value ("" if blank)


@dataclass
class RowError:
    """A single human-readable validation problem for the English report.

    `severity` is "ERROR" (blocks generation) or "WARNING" (highlighted to the
    operator but non-blocking — e.g. a consignee deviation or a possible split).
    """

    row_number: int
    column: str  # "" for row/group-level problems
    message: str
    severity: str = "ERROR"


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
    project_id: str
    # ship-to (captured split, printed joined)
    ship_to_enterprise: str
    ship_to_name: str
    ship_to_address_line1: str
    ship_to_address_line2: str
    ship_to_city: str
    ship_to_state: str
    ship_to_pincode: str
    ship_to_phone: str
    # consignee as TYPED in the upload (resolution/snapshot uses the golden record)
    consignee_name: str
    consignee_address_line1: str
    consignee_address_line2: str
    consignee_pincode: str
    consignee_state: str
    consignee_phone: str
    consignee_gstin: str
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
class Contradiction:
    """One consignee field whose uploaded value differs from the stored golden
    record (a KNOWN GSTIN). The operator must resolve each of these before a batch
    can generate — see `models.ChallanBatchDecision` / `DecisionChoice`."""

    gstin: str
    consignee_name: str  # the uploaded consignee name, for a human-readable report
    field: str           # name / address_line1 / address_line2 / pincode / state / phone
    stored: str
    uploaded: str


@dataclass
class ValidationResult:
    """Errors (block generation) + non-blocking warnings + consignee contradictions
    + the parsed challans.

    `ok` is driven by ERRORS only. A batch with no errors but with `contradictions`
    is valid-but-needs-review (blocked from generation until each is decided); a
    batch with neither is ready to generate. `challans` is populated only when there
    are no errors.
    """

    errors: list[RowError] = field(default_factory=list)
    warnings: list[RowError] = field(default_factory=list)
    contradictions: list[Contradiction] = field(default_factory=list)
    challans: list[ParsedChallan] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def issues(self) -> list[RowError]:
        """Errors + warnings, for a single combined operator report."""
        return [*self.errors, *self.warnings]


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
    address: str       # joined line1 / line2 / pincode
    gstin: str
    state_label: str   # "Bihar (10)"
    phone: str


@dataclass
class ShipToView:
    name: str
    address: str       # joined line1 / line2 / city / pincode / state
    enterprise: str
    phone: str


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
    number: str          # GIF/DC/26-27/L/000189
    project_id: str      # NOT printed on the document (internal reference only)
    po_number: str       # printed as "PO No." just below the challan number ("" -> omitted)
    invoice_number: str  # printed just above the challan number ("" -> omitted)
    challan_date: str    # display "28th July 2026"
    consignor: ConsignorView
    consignee: ConsigneeView
    ship_to: ShipToView
    lines: list[LineView]
    total_qty: str
    total_amount: str  # "" when value-free
    show_amount: bool  # False -> Rate/Amt columns + total are blank
    access_token: str = ""  # opaque QR token; "" -> no QR (feature dormant)
