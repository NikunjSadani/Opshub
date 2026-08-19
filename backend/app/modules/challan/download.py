"""Challan range/list download — resolve a (series, FY, range and/or list) selection to
ISSUED challans for a ZIP of individual PDFs or a 2-up merged PDF.

Numbers repeat per (series, FY), so a selection is always scoped to both. VOID numbers in
the range are SKIPPED and reported back so the operator is told exactly what was left out
(never silently); a number with no challan at all is reported as MISSING.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.challan.models import Challan, ChallanStatus

# A single request can't pull an unbounded slice of the never-reused statutory sequence.
MAX_DOWNLOAD_CHALLANS = 2000


@dataclass
class ResolvedChallan:
    number_int: int
    number: str
    id: int
    pdf_file_id: int | None


@dataclass
class ResolveResult:
    resolved: list[ResolvedChallan] = field(default_factory=list)  # ISSUED + has a stored PDF
    skipped_void: list[int] = field(default_factory=list)          # VOID → skipped (+ notified)
    no_pdf: list[int] = field(default_factory=list)                # ISSUED but no stored PDF yet
    missing: list[int] = field(default_factory=list)               # no such challan in series/FY
    errors: list[str] = field(default_factory=list)                # spec-parse / empty errors


def parse_number_spec(spec: str) -> tuple[list[int], list[str]]:
    """Parse a "10-50, 55, 60" range-and/or-list spec → (sorted unique positive ints, errors).

    Accepts comma / newline / semicolon separators; a token is either `N` or `LO-HI`. Leading
    zeros are tolerated (`000012` == `12`). Bad tokens, reversed/zero ranges, and oversized
    selections become English errors rather than a silent wrong result.
    """
    numbers: set[int] = set()
    errors: list[str] = []
    for token in re.split(r"[,\n;]", spec or ""):
        tok = token.strip()
        if not tok:
            continue
        rng = re.fullmatch(r"0*(\d+)\s*-\s*0*(\d+)", tok)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo == 0 or hi == 0:
                errors.append(f"'{tok}': challan numbers start at 1")
            elif lo > hi:
                errors.append(f"range '{tok}': start is after end")
            elif hi - lo + 1 > MAX_DOWNLOAD_CHALLANS:
                errors.append(f"range '{tok}' spans more than {MAX_DOWNLOAD_CHALLANS} numbers")
            else:
                numbers.update(range(lo, hi + 1))
            continue
        single = re.fullmatch(r"0*(\d+)", tok)
        if single:
            n = int(single.group(1))
            if n == 0:
                errors.append(f"'{tok}': challan numbers start at 1")
            else:
                numbers.add(n)
            continue
        errors.append(f"'{tok}' is not a challan number or range")
    if len(numbers) > MAX_DOWNLOAD_CHALLANS:
        errors.append(f"more than {MAX_DOWNLOAD_CHALLANS} challans requested at once")
    return sorted(numbers), errors


def resolve(db: Session, series: str, fy: str, spec: str) -> ResolveResult:
    """Parse the spec and classify each requested number (within this series/FY) as an ISSUED
    challan to download, a VOID to SKIP (reported), or a MISSING number."""
    numbers, errors = parse_number_spec(spec)
    result = ResolveResult(errors=list(errors))
    if not numbers:
        if not result.errors:
            result.errors.append("enter at least one challan number or range")
        return result

    rows = db.execute(
        select(Challan).where(
            Challan.series == series,
            Challan.fy == fy,
            Challan.number_int.in_(numbers),
        )
    ).scalars().all()
    by_num = {c.number_int: c for c in rows}
    for n in numbers:
        c = by_num.get(n)
        if c is None:
            result.missing.append(n)
        elif c.status == ChallanStatus.VOID.value:
            result.skipped_void.append(n)
        elif c.pdf_file_id is None:
            # ISSUED but never rendered (e.g. a partially-generated/retried batch). It is a
            # real challan, so NOT "missing" — but it has no PDF to hand out, so it must be
            # reported, never counted as downloadable and never silently dropped.
            result.no_pdf.append(n)
        else:
            result.resolved.append(ResolvedChallan(n, c.number, c.id, c.pdf_file_id))
    return result
