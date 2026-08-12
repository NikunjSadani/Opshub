"""Self-documenting Excel upload template for the challan generator.

Builds a two-sheet `.xlsx`:

* **Challans** — the data sheet the operator fills in (this MUST be the first
  worksheet, because the parser reads `worksheets[0]`). Row 1 is the canonical
  header (`schema.CHALLAN_COLUMNS`), styled + frozen, with a hover-comment on
  every header cell explaining that column. Two worked example rows follow.
* **Instructions** — a full reference: every column (Required?/Applies to/what to
  enter, all driven off the schema so they can't drift) plus Dos & Don'ts.

The headers are the exact `schema.CHALLAN_COLUMNS` keys (the parser matches them
case-insensitively), so a downloaded template uploads back cleanly.
"""
from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

from app.modules.challan.schema import (
    CHALLAN_COLUMNS,
    GROUP_CONSISTENT_FIELDS,
    REQUIRED_COLUMNS,
)

# Per-column prose (Required?/Applies-to are DERIVED from the schema below).
_HELP: dict[str, str] = {
    "group": "A label that groups rows into one challan. Rows sharing the SAME "
             "value become a single (multi-line) challan. Use one label per "
             "challan, e.g. C1, C2.",
    "brand": "Brand of the goods. Together with Ship-to State it looks up the "
             "consignee (bill-to party) from Master Data — so it must match a "
             "brand configured there.",
    "ship_to_state": "Destination state. Together with Brand it resolves the "
                     "consignee registry entry (name / GSTIN / address).",
    "ship_to_name": "Name of the party the goods are shipped to "
                    "(the 'Detail of Shipment to' block).",
    "ship_to_address": "Full shipping address of the ship-to party.",
    "ship_to_enterprise": "Enterprise / business name of the ship-to party.",
    "ship_to_number": "Contact phone number at the ship-to party.",
    "ship_to_contact": "Name of the contact person at the ship-to party.",
    "challan_date": "Challan date. Formats: DD-MM-YYYY (e.g. 15-05-2026), "
                    "YYYY-MM-DD, or a real Excel date. This sets the financial "
                    "year (Apr-Mar) in the challan number.",
    "description": "Description of the line item (product).",
    "hsn": "HSN code of the line item. Must already exist as an active HSN in "
           "Master Data (it supplies the GST rate).",
    "quantity": "Quantity for the line. Must be a number greater than 0 "
                "(decimals allowed).",
    "rate": "Pre-tax unit rate. Optional. May be free text like 'as per "
            "contract'. If a number, it must not be negative.",
    "amount": "Tax-INCLUSIVE line amount in rupees = Rate x Qty x (1 + GST%). "
              "Optional — leave blank for a value-free challan. Within one "
              "challan, either ALL lines have an amount or NONE do.",
    "gst_rate": "GST % for the line. Optional. If given, it must equal the HSN's "
                "configured rate (a safety cross-check).",
    "po_number": "Purchase-order number. Optional. Stored but NOT printed on the "
                 "challan.",
    "invoice_number": "Invoice number. Optional. Printed just above the challan "
                      "number.",
}

# Two worked examples: one single-line priced challan (HSN 1509 @ 5% ->
# 100 x 10 x 1.05 = 1050.00) and one two-line value-free challan.
_EXAMPLES: tuple[dict[str, str], ...] = (
    {
        "group": "C1", "brand": "Deoleo", "ship_to_state": "Maharashtra",
        "ship_to_name": "Acme Retail Pvt Ltd", "ship_to_address": "12 MG Road, Mumbai 400001",
        "ship_to_enterprise": "Acme Enterprises", "ship_to_number": "9900000000",
        "ship_to_contact": "Ravi Kumar", "challan_date": "15-05-2026",
        "description": "Olive Oil 1L", "hsn": "1509", "quantity": "10",
        "rate": "100.00", "amount": "1050.00", "gst_rate": "5",
        "po_number": "PO-2026-100", "invoice_number": "INV-2026-100",
    },
    {
        "group": "C2", "brand": "Deoleo", "ship_to_state": "Maharashtra",
        "ship_to_name": "Best Foods", "ship_to_address": "9 Link Road, Pune 411001",
        "ship_to_enterprise": "Best Foods LLP", "ship_to_number": "9800000000",
        "ship_to_contact": "Sunita Rao", "challan_date": "16-05-2026",
        "description": "Sample Pack A", "hsn": "1509", "quantity": "2",
        "rate": "", "amount": "", "gst_rate": "", "po_number": "", "invoice_number": "",
    },
    {
        "group": "C2", "brand": "Deoleo", "ship_to_state": "Maharashtra",
        "ship_to_name": "Best Foods", "ship_to_address": "9 Link Road, Pune 411001",
        "ship_to_enterprise": "Best Foods LLP", "ship_to_number": "9800000000",
        "ship_to_contact": "Sunita Rao", "challan_date": "16-05-2026",
        "description": "Sample Pack B", "hsn": "1509", "quantity": "3",
        "rate": "", "amount": "", "gst_rate": "", "po_number": "", "invoice_number": "",
    },
)

_DOS: tuple[str, ...] = (
    "Repeat the same 'group' value to add more lines to one challan; keep every "
    "challan-level field identical across those rows.",
    "Fill every Required column (see the table above).",
    "Make sure Brand + Ship-to State match a consignee, and HSN matches an HSN, "
    "already set up in Master Data.",
    "Enter a Quantity greater than 0.",
    "If you enter an Amount, use the tax-inclusive figure = Rate x Qty x (1 + GST%).",
    "Replace the two example rows on the 'Challans' sheet with your own data.",
    "Save the file as .xlsx before uploading.",
)

_DONTS: tuple[str, ...] = (
    "Don't rename, remove, reorder, or add header columns — keep row 1 exactly as "
    "provided.",
    "Don't type consignor or consignee details — the consignor is fixed, and the "
    "consignee is looked up from Brand + Ship-to State.",
    "Don't mix priced and value-free lines in the same challan (all priced, or all "
    "blank).",
    "Don't enter a GST rate different from the HSN's configured rate.",
    "Don't leave any Required column blank.",
    "Don't upload a .csv — the uploader accepts .xlsx only.",
)

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")  # slate-800
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_REQ_FILL = PatternFill("solid", fgColor="FEF3C7")      # amber-100 (required cols)
_TITLE_FONT = Font(bold=True, size=14)
_SECTION_FONT = Font(bold=True, size=11, color="1F2937")
_WRAP_TOP = Alignment(wrap_text=True, vertical="top")
_THIN = Side(style="thin", color="D1D5DB")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _applies_to(col: str) -> str:
    if col == "group":
        return "Grouping key"
    if col in GROUP_CONSISTENT_FIELDS:
        return "Whole challan"
    return "Per line"


def _build_data_sheet(ws: Worksheet) -> None:
    ws.title = "Challans"
    ws.freeze_panes = "A2"
    for idx, col in enumerate(CHALLAN_COLUMNS, start=1):
        cell = ws.cell(row=1, column=idx, value=col)
        cell.fill = _HEADER_FILL if col not in REQUIRED_COLUMNS else _REQ_FILL
        cell.font = _HEADER_FONT if col not in REQUIRED_COLUMNS else Font(bold=True)
        req = "REQUIRED" if col in REQUIRED_COLUMNS else "Optional"
        cell.comment = Comment(f"{req} · {_applies_to(col)}\n\n{_HELP[col]}", "OpsHub")
        ws.column_dimensions[cell.column_letter].width = max(14, len(col) + 4)
    for r, example in enumerate(_EXAMPLES, start=2):
        for idx, col in enumerate(CHALLAN_COLUMNS, start=1):
            ws.cell(row=r, column=idx, value=example.get(col, ""))


def _build_instructions_sheet(ws: Worksheet) -> None:
    ws.title = "Instructions"
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 16
    ws.column_dimensions["D"].width = 80

    ws["A1"] = "Delivery Challan — upload template"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = (
        "Fill the 'Challans' sheet (one row per line item) and upload it as .xlsx. "
        "The consignor is fixed; the consignee is resolved from Brand + Ship-to State."
    )
    ws["A2"].alignment = _WRAP_TOP
    ws.merge_cells("A2:D2")

    head = 4
    for i, label in enumerate(("Column", "Required?", "Applies to", "What to enter")):
        c = ws.cell(row=head, column=1 + i, value=label)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
        c.border = _BORDER
    row = head + 1
    for col in CHALLAN_COLUMNS:
        req = "Required" if col in REQUIRED_COLUMNS else "Optional"
        values = (col, req, _applies_to(col), _HELP[col])
        for i, val in enumerate(values):
            c = ws.cell(row=row, column=1 + i, value=val)
            c.alignment = _WRAP_TOP
            c.border = _BORDER
            if i == 0:
                c.font = Font(bold=True)
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="DOs").font = _SECTION_FONT
    row += 1
    for item in _DOS:
        cell = ws.cell(row=row, column=1, value=f"✓  {item}")
        cell.alignment = _WRAP_TOP
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 1

    row += 1
    ws.cell(row=row, column=1, value="DON'Ts").font = _SECTION_FONT
    row += 1
    for item in _DONTS:
        cell = ws.cell(row=row, column=1, value=f"✗  {item}")
        cell.alignment = _WRAP_TOP
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 1


def build_template_xlsx() -> bytes:
    """Return the self-documenting upload template as `.xlsx` bytes."""
    wb = Workbook()
    _build_data_sheet(wb.active)  # the default sheet becomes 'Challans' (worksheets[0])
    _build_instructions_sheet(wb.create_sheet())
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
