"""Canonicalization shared by master-data writes and challan-time lookups.

The consignee registry is keyed by (brand, ship-to state); if the two sides
normalize differently a typo could snapshot the wrong GSTIN onto a legal
challan. So writes STORE the collapsed value (case preserved for display) and
uniqueness/lookups compare case-insensitively (`match_key`).
"""
from __future__ import annotations

# Indian GST state codes: 01–38 plus 97 (Other Territory).
VALID_STATE_CODES: frozenset[str] = frozenset(
    {f"{i:02d}" for i in range(1, 39)} | {"97"}
)

# GSTIN check-digit alphabet (base-36): digits then A–Z.
_GSTIN_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# State name -> GST state code, incl. common spelling variants. Names are matched
# via match_key (collapsed + casefolded). Unknown names skip the state cross-check.
STATE_NAME_TO_CODE: dict[str, str] = {
    "jammu and kashmir": "01",
    "himachal pradesh": "02",
    "punjab": "03",
    "chandigarh": "04",
    "uttarakhand": "05", "uttaranchal": "05",
    "haryana": "06",
    "delhi": "07", "new delhi": "07", "nct of delhi": "07",
    "rajasthan": "08",
    "uttar pradesh": "09",
    "bihar": "10",
    "sikkim": "11",
    "arunachal pradesh": "12",
    "nagaland": "13",
    "manipur": "14",
    "mizoram": "15",
    "tripura": "16",
    "meghalaya": "17",
    "assam": "18",
    "west bengal": "19",
    "jharkhand": "20",
    "odisha": "21", "orissa": "21",
    "chhattisgarh": "22", "chattisgarh": "22",
    "madhya pradesh": "23",
    "gujarat": "24",
    "daman and diu": "25",
    "dadra and nagar haveli and daman and diu": "26", "dadra and nagar haveli": "26",
    "maharashtra": "27",
    "karnataka": "29",
    "goa": "30",
    "lakshadweep": "31",
    "kerala": "32",
    "tamil nadu": "33",
    "puducherry": "34", "pondicherry": "34",
    "andaman and nicobar islands": "35", "andaman and nicobar": "35",
    "telangana": "36",
    "andhra pradesh": "37",
    "ladakh": "38",
    "other territory": "97",
}


def collapse_ws(value: str) -> str:
    """Strip ends + collapse internal whitespace runs to single spaces."""
    return " ".join(value.split())


def match_key(value: str) -> str:
    """Case-insensitive comparison key (collapsed + casefolded)."""
    return collapse_ws(value).casefold()


def _gstin_check_char(first14: str) -> str:
    """The GSTN mod-36 check character for the first 14 GSTIN chars."""
    total = 0
    factor = 2
    for ch in reversed(first14):
        cp = _GSTIN_ALPHABET.index(ch)
        digit = factor * cp
        total += digit // 36 + digit % 36
        factor = 1 if factor == 2 else 2
    return _GSTIN_ALPHABET[(36 - (total % 36)) % 36]


def valid_gstin(gstin: str) -> bool:
    """Full structural + checksum validity: 15 chars, real state code, check digit."""
    if len(gstin) != 15 or any(c not in _GSTIN_ALPHABET for c in gstin):
        return False
    if gstin[:2] not in VALID_STATE_CODES:
        return False
    return _gstin_check_char(gstin[:14]) == gstin[14]


def valid_gstin_state(gstin: str) -> bool:
    """True if the GSTIN's leading state code is a real GST state code (range only)."""
    return len(gstin) >= 2 and gstin[:2] in VALID_STATE_CODES


def gstin_matches_state(gstin: str, state: str) -> bool:
    """True unless a RECOGNIZED state name disagrees with the GSTIN's state code.

    Lenient by design: an unrecognized/abbreviated state name (not in the map)
    passes, so real-but-unusually-named states aren't falsely rejected.
    """
    code = STATE_NAME_TO_CODE.get(match_key(state))
    return code is None or code == gstin[:2]
