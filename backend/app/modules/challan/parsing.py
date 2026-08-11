"""Challan Excel parsing + structural validation — the no-database front door.

This layer turns an uploaded `.xlsx` into `RawRow`s and reports every problem it
can find WITHOUT touching master data, so a bad spreadsheet is rejected with a
complete English report before the service ever reserves a number:

  parse_workbook(bytes)          -> (list[RawRow], list[RowError])  # shape/headers
  structural_row_errors(rows)    -> list[RowError]                  # per-row + group

The semantic layer (consignor/consignee/HSN lookups, building `ParsedChallan`s)
lives in the service — this module deliberately stops at structural truth.

Money is integer PAISE via `Decimal`, never float: `parse_paise` multiplies rupees
by 100 and rounds HALF-UP to the paise. `rate` may legitimately be free text
(e.g. "as per contract"), so `parse_rate` returns the verbatim text plus an
optional parsed paise value.
"""
from __future__ import annotations

import io
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from openpyxl import load_workbook

from app.modules.challan.schema import (
    CHALLAN_COLUMNS,
    GROUP_CONSISTENT_FIELDS,
    REQUIRED_COLUMNS,
    RawRow,
    RowError,
)

# Excel's day-zero for the 1900 date system, offset by its fictional 1900-02-29.
_EXCEL_EPOCH = date(1899, 12, 30)
_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


# ------------------------------------------------------------------- helpers

def _to_decimal(text: str) -> Decimal | None:
    """Parse a (comma-grouped) numeric string to a finite Decimal, else None.

    Rejects blanks, junk, and non-finite tokens ("NaN"/"Infinity" are valid
    `Decimal` literals but must never masquerade as a quantity or amount).
    """
    cleaned = text.strip().replace(",", "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_paise(text: str) -> int | None:
    """Rupees string ("1,234.50") -> integer paise (123450), or None if not numeric.

    Half-up rounding to the paise keeps money exact and float-free.
    """
    rupees = _to_decimal(text)
    if rupees is None:
        return None
    paise = (rupees * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(paise)


def parse_rate(text: str) -> tuple[str, int | None]:
    """Return (verbatim text, paise) — rate may be free text, so paise is optional."""
    return text.strip(), parse_paise(text)


def parse_qty(text: str) -> Decimal | None:
    """Parse a quantity/rate figure to a finite Decimal, or None if not numeric."""
    return _to_decimal(text)


def parse_date(text: str) -> date | None:
    """Parse DD-MM-YYYY, YYYY-MM-DD, DD/MM/YYYY, an ISO datetime, or an Excel serial.

    openpyxl hands date cells back as `datetime`s that we stringify to ISO, so the
    ISO/`datetime`-string branch covers cells authored as real Excel dates; the
    serial branch covers a bare numeric date. Returns None if nothing matches.
    """
    s = text.strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(s[:10])  # "2026-08-10" or "2026-08-10 00:00:00"
    except ValueError:
        pass
    serial = _to_decimal(s)
    if serial is None or serial <= 0:
        return None
    return _EXCEL_EPOCH + timedelta(days=int(serial))


def _to_str(value: object) -> str:
    """Coerce a raw cell value to a trimmed string ("" for blank)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        # Avoid "10.0" for whole-number Excel cells; keep exact decimals otherwise.
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


# ------------------------------------------------------------------- parse

def parse_workbook(data: bytes) -> tuple[list[RawRow], list[RowError]]:
    """Load the first worksheet from `data` and read its rows against the template.

    Row 1 is the header; headers match `CHALLAN_COLUMNS` case-insensitively and
    whitespace-trimmed. Returns ([], [errors]) — never raises — if the file can't
    be opened or any `REQUIRED_COLUMNS` header is missing (one error per missing
    key, all at row 1). Otherwise returns (rows, []): each `RawRow` carries the
    real 1-based sheet row number (first data row = 2), every mapped cell coerced
    to a trimmed string; fully-blank rows and unknown/extra columns are dropped.
    """
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - openpyxl raises many types on a bad file
        return [], [RowError(1, "", "could not read the file as an .xlsx workbook")]
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)

        col_index: dict[str, int] = {}
        for idx, raw in enumerate(header or ()):
            if raw is None:
                continue
            key = str(raw).strip().lower()
            if key in CHALLAN_COLUMNS and key not in col_index:
                col_index[key] = idx

        missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
        if missing:
            return [], [
                RowError(1, c, f"required column '{c}' is missing") for c in missing
            ]

        rows: list[RawRow] = []
        for sheet_row, values in enumerate(rows_iter, start=2):
            cells = {
                key: _to_str(values[idx] if idx < len(values) else None)
                for key, idx in col_index.items()
            }
            if all(v == "" for v in cells.values()):
                continue  # skip a fully-blank row
            rows.append(RawRow(row_number=sheet_row, cells=cells))
        return rows, []
    finally:
        wb.close()


# --------------------------------------------------------------- validation

def structural_row_errors(rows: list[RawRow]) -> list[RowError]:
    """Every no-database problem across `rows`, sorted by (row_number, column).

    Covers required-non-empty, numeric/date well-formedness, and per-group field
    consistency. Collects ALL errors (never stops at the first) so the operator
    gets one complete report.
    """
    errors: list[RowError] = []
    for row in rows:
        _required_errors(row, errors)
        _numeric_errors(row, errors)
        _date_errors(row, errors)
    _group_consistency_errors(rows, errors)
    errors.sort(key=lambda e: (e.row_number, e.column))
    return errors


def _required_errors(row: RawRow, errors: list[RowError]) -> None:
    for col in REQUIRED_COLUMNS:
        if not row.cells.get(col, "").strip():
            errors.append(RowError(row.row_number, col, f"{col} is required"))


def _numeric_errors(row: RawRow, errors: list[RowError]) -> None:
    """Validate quantity (> 0), amount (>= 0), and gst_rate (0..100) when present.

    Emptiness is left to `_required_errors`, so these fire only on a present-but-
    malformed value (no duplicate error for a blank required cell).
    """
    c = row.cells
    if c.get("quantity", "").strip():
        qty = parse_qty(c["quantity"])
        if qty is None or qty <= 0:
            errors.append(RowError(row.row_number, "quantity",
                                   "quantity must be a positive number"))
    if c.get("amount", "").strip():
        amount = parse_paise(c["amount"])
        if amount is None or amount < 0:
            errors.append(RowError(row.row_number, "amount",
                                   "amount must be a number >= 0"))
    rate = c.get("rate", "").strip()
    if rate:  # rate may be free text; only a PARSED-numeric-and-negative rate is wrong
        rate_paise = parse_paise(rate)
        if rate_paise is not None and rate_paise < 0:
            errors.append(RowError(row.row_number, "rate", "rate must not be negative"))
    if c.get("gst_rate", "").strip():
        gst = parse_qty(c["gst_rate"])
        if gst is None or gst < 0 or gst > 100:
            errors.append(RowError(row.row_number, "gst_rate",
                                   "gst_rate must be a number between 0 and 100"))


def _date_errors(row: RawRow, errors: list[RowError]) -> None:
    text = row.cells.get("challan_date", "").strip()
    if text and parse_date(text) is None:
        errors.append(RowError(row.row_number, "challan_date",
                               "challan_date is not a recognisable date"))


def _group_consistency_errors(rows: list[RawRow], errors: list[RowError]) -> None:
    """Flag every row whose GROUP_CONSISTENT_FIELDS diverge from its group's first row.

    Rows are grouped by `group`; a blank group is left to `_required_errors`.
    """
    groups: dict[str, list[RawRow]] = {}
    for row in rows:
        key = row.cells.get("group", "").strip()
        if key:
            groups.setdefault(key, []).append(row)

    for key, members in groups.items():
        if len(members) < 2:
            continue
        first = members[0]
        for field_name in GROUP_CONSISTENT_FIELDS:
            baseline = first.cells.get(field_name, "")
            for row in members[1:]:
                if row.cells.get(field_name, "") != baseline:
                    errors.append(RowError(
                        row.row_number, field_name,
                        f"{field_name} must match the rest of group '{key}'"))
