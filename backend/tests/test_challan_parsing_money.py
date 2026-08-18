"""Regression tests for the numeric-parsing audit findings in `challan.parsing`.

Each test pins a specific defect the audit surfaced so it can't silently return:
  M2  European decimal-comma / ambiguous grouping is rejected, never mis-parsed
  L1  non-ASCII digits and "1_000" underscores are row errors, not silent values
  L2  a quantity past the Numeric(14,3) column is a validation row error
  L3  the parser preserves every existing passing money/qty behaviour
"""
from __future__ import annotations

from decimal import Decimal

from app.modules.challan import parsing


def _min_cells(**over: str) -> dict[str, str]:
    """A minimal valid-shape row, overridable per test (mirrors the audit suite)."""
    base = {
        "challan_group": "G1", "project_id": "BRI-001", "ship_to_name": "S",
        "ship_to_address_line1": "A", "ship_to_state": "MH", "consignee_name": "C",
        "consignee_gstin": "27AAPFU0939F1ZV", "challan_date": "15-05-2026",
        "description": "Item", "hsn": "1509", "quantity": "1",
    }
    base.update(over)
    return base


def _qty_errors(qty: str) -> list[parsing.RowError]:
    row = parsing.RawRow(row_number=2, cells=_min_cells(quantity=qty))
    return [e for e in parsing.structural_row_errors([row]) if e.column == "quantity"]


# --------------------------------------------------- M2: European decimal comma

def test_european_decimal_comma_is_rejected_not_mis_parsed() -> None:
    # "1.234,50" must NOT become 123 paise (the old unconditional comma-strip bug).
    assert parsing.parse_paise("1.234,50") is None
    assert parsing.parse_qty("1.234,50") is None


def test_ambiguous_grouping_is_rejected() -> None:
    # "1,0,0" must NOT become 10000 paise; ambiguous grouping -> row error.
    assert parsing.parse_paise("1,0,0") is None
    assert parsing.parse_qty("1,0,0") is None


def test_indian_grouping_parses_correctly() -> None:
    assert parsing.parse_paise("1,23,456.78") == 12345678
    assert parsing.parse_qty("1,23,456.78") == Decimal("123456.78")


def test_western_grouping_parses_correctly() -> None:
    assert parsing.parse_paise("1,234,567.89") == 123456789


def test_plain_and_single_grouped_values_still_parse() -> None:
    assert parsing.parse_paise("1234.50") == 123450
    assert parsing.parse_paise("1234") == 123400
    assert parsing.parse_paise("1,234.50") == 123450  # single Western group


def test_mixed_interior_group_widths_are_rejected() -> None:
    # inconsistent interior widths (2 then 3) are malformed grouping.
    assert parsing.parse_qty("1,23,456,789") is None


# ------------------------------------- L1: non-ASCII digits / underscore groups

def test_non_ascii_and_underscore_numerics_are_row_errors() -> None:
    for junk in ("१००", "٢٣", "１００", "1_000"):
        assert parsing.parse_paise(junk) is None, junk
        assert parsing.parse_qty(junk) is None, junk


def test_ascii_digits_still_parse() -> None:
    assert parsing.parse_paise("100") == 10000
    assert parsing.parse_qty("100") == Decimal(100)


def test_non_ascii_quantity_is_structural_row_error() -> None:
    errs = _qty_errors("१००")
    assert any("positive number" in e.message for e in errs)


# --------------------------------------------- L2: quantity column upper bound

def test_quantity_over_column_integer_digits_is_row_error() -> None:
    errs = _qty_errors("100000000000")  # 1e11 -> 12 integer digits, overflows N(14,3)
    assert any("too large or too precise" in e.message for e in errs)


def test_quantity_at_column_limit_is_ok() -> None:
    assert _qty_errors("99999999999.999") == []  # 11 int + 3 dec == the column max


def test_quantity_too_precise_is_row_error() -> None:
    errs = _qty_errors("1.2345")  # 4 decimal places, past Numeric(14,3)
    assert any("too large or too precise" in e.message for e in errs)


def test_ordinary_quantity_has_no_error() -> None:
    assert _qty_errors("2.5") == []
    assert _qty_errors("10") == []


# ------------------------- L3 / preservation: existing behaviour is unchanged

def test_preserves_money_ceiling_and_free_text() -> None:
    assert parsing.parse_paise("1E30") is None            # money ceiling / non-numeric
    assert parsing.parse_paise("as per contract") is None  # rate free text
    assert parsing.parse_paise("") is None                 # blank
    assert parsing.parse_paise("1234.505") == 123451       # ROUND_HALF_UP


def test_preserves_negative_and_finite_semantics() -> None:
    assert parsing.parse_qty("-5") == Decimal(-5)  # sign preserved (validation bounds >0 elsewhere)
    assert parsing.parse_qty("NaN") is None
    assert parsing.parse_qty("Infinity") is None
