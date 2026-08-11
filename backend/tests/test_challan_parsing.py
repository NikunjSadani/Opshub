"""Challan parsing + structural validation — exercised against real .xlsx bytes.

Builds in-memory workbooks with openpyxl (never a fixture file) so the header
matching, row-number arithmetic, blank-row skipping, numeric/date/group checks,
and the money/date helpers are all under test end to end.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

from openpyxl import Workbook

from app.modules.challan import parsing
from app.modules.challan.schema import CHALLAN_COLUMNS

_DEFAULTS: dict[str, object] = {
    "group": "G1",
    "brand": "Bertolli",
    "ship_to_state": "Karnataka",
    "ship_to_name": "Retail Mart",
    "ship_to_address": "12 MG Road",
    "ship_to_enterprise": "Mart Enterprises",
    "ship_to_number": "9000000000",
    "ship_to_contact": "Ravi",
    "challan_date": "10-08-2026",
    "description": "Olive Oil 1L",
    "hsn": "1509",
    "quantity": "10",
    "rate": "500.00",
    "amount": "5000.00",
    "gst_rate": "5",
    "po_number": "PO-1",
    "invoice_number": "INV-1",
}


def _row(**overrides: object) -> list[object]:
    """A valid data row (in CHALLAN_COLUMNS order) with per-cell overrides."""
    merged = {**_DEFAULTS, **overrides}
    return [merged[col] for col in CHALLAN_COLUMNS]


def _xlsx(headers: list[object], data_rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in data_rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------- parse_workbook

def test_clean_two_line_one_group_parses() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [
        _row(description="Olive Oil 1L", quantity="10", amount="5000.00"),
        _row(description="Olive Oil 2L", quantity="5", amount="4000.00"),
    ])
    rows, errors = parsing.parse_workbook(data)
    assert errors == []
    assert len(rows) == 2
    assert [r.row_number for r in rows] == [2, 3]  # first data row = sheet row 2
    assert rows[0].cells["group"] == "G1"
    assert rows[1].cells["description"] == "Olive Oil 2L"
    assert parsing.structural_row_errors(rows) == []


def test_headers_match_case_insensitively_and_trimmed() -> None:
    headers = [str(c).upper().center(len(c) + 4) for c in CHALLAN_COLUMNS]
    data = _xlsx(headers, [_row()])
    rows, errors = parsing.parse_workbook(data)
    assert errors == []
    assert len(rows) == 1
    assert rows[0].cells["brand"] == "Bertolli"


def test_missing_required_column_is_reported() -> None:
    headers = [c for c in CHALLAN_COLUMNS if c != "brand"]
    data = _xlsx(list(headers), [])
    rows, errors = parsing.parse_workbook(data)
    assert rows == []
    assert len(errors) == 1
    assert errors[0].row_number == 1
    assert errors[0].column == "brand"


def test_unopenable_file_is_reported_not_raised() -> None:
    rows, errors = parsing.parse_workbook(b"this is not a zip / xlsx")
    assert rows == []
    assert len(errors) == 1
    assert errors[0].row_number == 1


def test_fully_blank_row_is_skipped() -> None:
    blank: list[object] = [None] * len(CHALLAN_COLUMNS)
    data = _xlsx(list(CHALLAN_COLUMNS), [
        _row(),
        blank,
        _row(description="Second real line"),
    ])
    rows, errors = parsing.parse_workbook(data)
    assert errors == []
    assert [r.row_number for r in rows] == [2, 4]  # blank sheet row 3 dropped


def test_unknown_extra_columns_are_ignored() -> None:
    headers = [*list(CHALLAN_COLUMNS), "notes", "internal_ref"]
    data = _xlsx(headers, [[*_row(), "ignore me", "X-99"]])
    rows, errors = parsing.parse_workbook(data)
    assert errors == []
    assert set(rows[0].cells) == set(CHALLAN_COLUMNS)


# ---------------------------------------------------------- structural errors

def test_bad_quantity_amount_and_gst_are_reported() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [
        _row(quantity="abc", amount="1000.00", gst_rate="5"),
        _row(quantity="5", amount="not-money", gst_rate="150"),
    ])
    rows, errors = parsing.parse_workbook(data)
    assert errors == []
    problems = {(e.row_number, e.column) for e in parsing.structural_row_errors(rows)}
    assert (2, "quantity") in problems  # non-numeric quantity
    assert (3, "amount") in problems    # non-numeric amount
    assert (3, "gst_rate") in problems  # out-of-range gst_rate


def test_zero_and_negative_quantity_rejected() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [_row(quantity="0"), _row(quantity="-3")])
    problems = {
        (e.row_number, e.column)
        for e in parsing.structural_row_errors(parsing.parse_workbook(data)[0])
    }
    assert (2, "quantity") in problems
    assert (3, "quantity") in problems


def test_negative_rate_rejected() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [_row(rate="-100.00")])
    problems = {
        (e.row_number, e.column)
        for e in parsing.structural_row_errors(parsing.parse_workbook(data)[0])
    }
    assert (2, "rate") in problems


def test_free_text_rate_still_allowed() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [_row(rate="as per contract")])
    problems = [e for e in parsing.structural_row_errors(parsing.parse_workbook(data)[0])
                if e.column == "rate"]
    assert problems == []


def test_missing_required_value_reported_per_row() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [_row(ship_to_name="")])
    errors = parsing.structural_row_errors(parsing.parse_workbook(data)[0])
    assert any(e.row_number == 2 and e.column == "ship_to_name" for e in errors)


def test_group_inconsistent_ship_to_state_is_reported() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [
        _row(ship_to_state="Karnataka"),
        _row(ship_to_state="Kerala"),
    ])
    rows, _ = parsing.parse_workbook(data)
    errors = parsing.structural_row_errors(rows)
    offenders = [e for e in errors if e.column == "ship_to_state"]
    assert len(offenders) == 1
    assert offenders[0].row_number == 3  # the divergent row, not the first


def test_errors_are_sorted_by_row_then_column() -> None:
    data = _xlsx(list(CHALLAN_COLUMNS), [
        _row(quantity="bad", amount="bad"),
        _row(quantity="bad"),
    ])
    errors = parsing.structural_row_errors(parsing.parse_workbook(data)[0])
    keys = [(e.row_number, e.column) for e in errors]
    assert keys == sorted(keys)


# ------------------------------------------------------------------- helpers

def test_parse_paise() -> None:
    assert parsing.parse_paise("1,234.50") == 123450
    assert parsing.parse_paise("1000") == 100000
    assert parsing.parse_paise("as per contract") is None
    assert parsing.parse_paise("") is None


def test_parse_paise_rounds_half_up() -> None:
    assert parsing.parse_paise("1234.505") == 123451  # 123450.5 paise -> half-up


def test_parse_rate_keeps_free_text() -> None:
    text, paise = parsing.parse_rate("as per contract")
    assert text == "as per contract"
    assert paise is None
    numeric_text, numeric_paise = parsing.parse_rate("1,234.50")
    assert numeric_text == "1,234.50"
    assert numeric_paise == 123450


def test_parse_qty() -> None:
    assert parsing.parse_qty("10") == Decimal(10)
    assert parsing.parse_qty("2.5") == Decimal("2.5")
    assert parsing.parse_qty("abc") is None


def test_parse_date_accepts_supported_formats() -> None:
    assert parsing.parse_date("10-08-2026") == date(2026, 8, 10)  # DD-MM-YYYY
    assert parsing.parse_date("2026-08-10") == date(2026, 8, 10)  # YYYY-MM-DD
    assert parsing.parse_date("10/08/2026") == date(2026, 8, 10)  # DD/MM/YYYY
    assert parsing.parse_date("2026-08-10 00:00:00") == date(2026, 8, 10)  # ISO string
    assert parsing.parse_date("nonsense") is None
    assert parsing.parse_date("") is None
