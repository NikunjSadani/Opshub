"""Canonicalization shared by master-data writes and challan-time lookups.

The consignee registry is keyed by (brand, ship-to state); if the two sides
normalize differently a typo could snapshot the wrong GSTIN onto a legal
challan. So writes STORE the collapsed value (case preserved for display) and
uniqueness/lookups compare case-insensitively (`match_key`).
"""
from __future__ import annotations

# Indian GST state codes: 01–37 plus 97 (Other Territory).
VALID_STATE_CODES: frozenset[str] = frozenset(
    {f"{i:02d}" for i in range(1, 38)} | {"97"}
)


def collapse_ws(value: str) -> str:
    """Strip ends + collapse internal whitespace runs to single spaces."""
    return " ".join(value.split())


def match_key(value: str) -> str:
    """Case-insensitive comparison key (collapsed + casefolded)."""
    return collapse_ws(value).casefold()


def valid_gstin_state(gstin: str) -> bool:
    """True if the GSTIN's leading state code is a real GST state code."""
    return len(gstin) >= 2 and gstin[:2] in VALID_STATE_CODES
