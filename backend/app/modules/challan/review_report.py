"""Downloadable Excel review report for a NEEDS_REVIEW challan batch.

When an upload contradicts the stored consignee golden record, the operator gets a
two-sheet `.xlsx` so they can understand everything offline before deciding:

* **Uploaded rows** — every row exactly as uploaded (all columns, friendly headers),
  with a trailing status column flagging rows whose consignee has a contradiction.
* **Contradictions to review** — one line per (GSTIN, field): our stored value vs
  the uploaded value + a plain-English note on the choice to make.

Pure function, no I/O beyond building the workbook bytes. Every value comes from an
untrusted upload, so each is passed through `_safe_cell` before writing: openpyxl
stores a string beginning with '=' as a LIVE formula (data_type 'f'), and Excel also
auto-evaluates a leading + - @ on open, so those are neutralized (leading quote) to
plain text. Do NOT remove that guard believing openpyxl is inherently safe — it isn't.
"""
from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.modules.challan.schema import CHALLAN_COLUMNS, COLUMN_HEADERS, Contradiction, RawRow

_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_FLAG_FILL = PatternFill("solid", fgColor="FEF3C7")  # amber-100 for contradiction rows
_WRAP = Alignment(wrap_text=True, vertical="top")

# Consignee golden-record field key -> human label for the report.
_FIELD_LABEL: dict[str, str] = {
    "name": "Consignee name",
    "address_line1": "Address line 1",
    "address_line2": "Address line 2",
    "pincode": "Pincode",
    "state": "State",
    "phone": "Phone",
}


def _norm_gstin(value: str) -> str:
    return value.strip().upper()


def _safe_cell(value: str) -> str:
    """Neutralize spreadsheet formula/DDE injection from an untrusted cell.

    openpyxl stores a string beginning with '=' as a live FORMULA (data_type 'f'),
    and Excel/Sheets also auto-evaluate a leading + - @ (or control char) on open. A
    value from the untrusted upload is prefixed with a single quote so the cell is
    forced to plain text — the same guard the CSV report applies (`service._csv_field`)."""
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _header_row(ws: Worksheet, labels: list[str]) -> None:
    for idx, label in enumerate(labels, start=1):
        cell = ws.cell(row=1, column=idx, value=label)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    ws.freeze_panes = "A2"


def _build_uploaded_sheet(ws: Worksheet, rows: list[RawRow], flagged: set[str]) -> None:
    ws.title = "Uploaded rows"
    headers = ["Row", *(COLUMN_HEADERS[k] for k in CHALLAN_COLUMNS), "Review status"]
    _header_row(ws, headers)
    for r, row in enumerate(rows, start=2):
        ws.cell(row=r, column=1, value=row.row_number)
        for idx, key in enumerate(CHALLAN_COLUMNS, start=2):
            ws.cell(row=r, column=idx, value=_safe_cell(row.cells.get(key, "")))
        gstin = _norm_gstin(row.cells.get("consignee_gstin", ""))
        flag = gstin in flagged
        status = ws.cell(row=r, column=len(headers),
                         value="Contradiction — see next sheet" if flag else "OK")
        if flag:
            status.fill = _FLAG_FILL
    ws.column_dimensions["A"].width = 6
    ws.column_dimensions[get_column_letter(len(headers))].width = 28


def _build_contradiction_sheet(ws: Worksheet, contradictions: list[Contradiction]) -> None:
    ws.title = "Contradictions to review"
    headers = ["GSTIN", "Consignee (as uploaded)", "Field", "Our records (stored)",
               "Your upload", "What to decide"]
    _header_row(ws, headers)
    for r, c in enumerate(contradictions, start=2):
        note = (f"Your upload says '{c.uploaded}' but our records have '{c.stored}'. "
                "Decide: update our records to your value, use your value for this "
                "upload only, or keep our records.")
        values = (c.gstin, c.consignee_name, _FIELD_LABEL.get(c.field, c.field),
                  c.stored, c.uploaded, note)
        for idx, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=idx, value=_safe_cell(value))
            cell.alignment = _WRAP
    for col, width in zip("ABCDEF", (18, 26, 16, 30, 30, 60), strict=True):
        ws.column_dimensions[col].width = width


def build_review_xlsx(rows: list[RawRow], contradictions: list[Contradiction]) -> bytes:
    """Return the two-sheet review workbook as `.xlsx` bytes."""
    flagged = {_norm_gstin(c.gstin) for c in contradictions}
    wb = Workbook()
    _build_uploaded_sheet(wb.active, rows, flagged)
    _build_contradiction_sheet(wb.create_sheet(), contradictions)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
