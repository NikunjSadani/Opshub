"""PO-line matcher — ties each captured client-invoice line to a PO line item.

A confirmed client invoice drives the §6 per-line ``invoiced_qty``, so every line must
resolve to exactly one ``po_line_item`` before confirm. This module proposes those links
automatically (``match_invoice``) and records an operator's override (``apply_manual_match``).

**Deterministic scoring.** For an invoice line we score it against every OPEN line on the
invoice's PO and keep the best. The composite (0..1) is a fixed weighted sum:

    score = 0.35 * identity + 0.35 * description + 0.15 * price + 0.15 * quantity

* ``identity``    — 1.0 if the product CODE appears at a WORD BOUNDARY in the invoice line
                    text (its tokens form a contiguous run of whole tokens — so a short code
                    like ``"S1"`` never matches inside ``"Gasket GAS-100 rubber"``), OR the
                    HSN/SAC codes match exactly; else 0.0. The strongest signal.
* ``description`` — Jaccard token overlap between the invoice line description and the PO
                    line's description + product name/brand/model.
* ``price``       — 1 − min(1, |inv_rate − po_sell| / po_sell) when both unit prices are
                    present; 0.0 otherwise (a missing price earns nothing, never a false credit).
* ``quantity``    — the same proximity on invoice qty vs PO ordered qty; 0.0 when either is absent.

Because ``price + quantity`` cap at 0.30, the 0.60 auto-match threshold can NEVER be reached
on numeric coincidence alone — a match always requires real identity/description agreement.

**Greedy 1:1 assignment.** All (invoice-line, po-line) pairs at or above threshold are ranked
by score (ties broken by invoice ``line_no`` then po-line id, so the result is stable); each
invoice line and each PO line is consumed at most once. This stops two invoice lines
auto-claiming the same PO line — safer for the invoiced-qty rollup. An operator resolves the
remainder with ``apply_manual_match``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.models import LineMatchStatus, SalesInvoice, SalesInvoiceLine
from app.modules.sales_orders.models import LineStatus, POLineItem, Product

# At or above this composite score an invoice line auto-matches its best PO line.
MATCH_THRESHOLD = 0.60

_W_IDENTITY = 0.35
_W_DESCRIPTION = 0.35
_W_PRICE = 0.15
_W_QUANTITY = 0.15

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class _Candidate:
    """One OPEN PO line + its resolved Product identity, ready to score."""

    po_line_id: int
    code: str | None
    hsn: str | None
    text: str            # product name/brand/model + PO line description, lower-cased
    sell_price_paise: int | None
    ordered_qty: Decimal | None


def _tokens(*values: str | None) -> set[str]:
    """Lower-case alphanumeric tokens across the given strings."""
    out: set[str] = set()
    for value in values:
        if value:
            out.update(_TOKEN_RE.findall(value.lower()))
    return out


def _token_list(*values: str | None) -> list[str]:
    """Lower-case alphanumeric tokens in ORDER (for word-boundary identity matching)."""
    out: list[str] = []
    for value in values:
        if value:
            out.extend(_TOKEN_RE.findall(value.lower()))
    return out


def _token_run_present(needle: list[str], haystack: list[str]) -> bool:
    """True when ``needle`` occurs as a CONTIGUOUS run of whole tokens inside ``haystack``.

    This is the word-boundary identity test: a code's tokens must line up on token
    boundaries (so hyphenated ``'WID-1'`` -> ['wid','1'] still hits 'Widget WID-1', while a
    short ``'S1'`` -> ['s1'] no longer matches inside the fused text of an unrelated line)."""
    if not needle:
        return False
    span = len(needle)
    return any(
        haystack[start:start + span] == needle
        for start in range(len(haystack) - span + 1)
    )


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _proximity(observed: int | Decimal | None, expected: int | Decimal | None) -> float:
    """1.0 when equal, decaying to 0.0 as they diverge; 0.0 when either is missing or the
    reference is 0 (no scale to compare against). Deterministic, symmetric in magnitude."""
    if observed is None or expected is None:
        return 0.0
    exp = Decimal(expected)
    if exp == 0:
        return 1.0 if Decimal(observed) == 0 else 0.0
    ratio = abs(Decimal(observed) - exp) / abs(exp)
    return float(max(Decimal(0), Decimal(1) - ratio))


def _alnum(value: str | None) -> str:
    """Lower-cased, punctuation/space-stripped form (so 'WID-1' and 'wid 1' compare equal)."""
    return _NON_ALNUM_RE.sub("", (value or "").lower())


def _score(line: SalesInvoiceLine, cand: _Candidate) -> float:
    """The documented composite score for one (invoice line, PO candidate) pair."""
    # identity: the product CODE appears at a WORD BOUNDARY in the line text (its tokens
    # form a contiguous run of whole tokens), OR the HSN/SAC codes match exactly. A raw
    # substring test (the old rule) let a short code like 'S1' match inside 'gas100...' —
    # the token-run test requires real token-boundary agreement (H1).
    code_tokens = _TOKEN_RE.findall((cand.code or "").lower())
    line_tokens = _token_list(line.description, line.hsn_sac)
    hsn_match = bool(
        line.hsn_sac and cand.hsn and _alnum(line.hsn_sac) == _alnum(cand.hsn)
    )
    identity = 1.0 if _token_run_present(code_tokens, line_tokens) or hsn_match else 0.0

    description = _jaccard(_tokens(line.description), _tokens(cand.text))
    price = _proximity(line.unit_rate_paise, cand.sell_price_paise)
    quantity = _proximity(line.quantity, cand.ordered_qty)

    return (
        _W_IDENTITY * identity
        + _W_DESCRIPTION * description
        + _W_PRICE * price
        + _W_QUANTITY * quantity
    )


def _candidate_from(po_line: POLineItem, product: Product) -> _Candidate:
    """One scorable candidate from a (PO line, Product) pair."""
    return _Candidate(
        po_line_id=po_line.id,
        code=product.code,
        hsn=product.hsn,
        text=" ".join(filter(None, (
            product.name, product.brand, product.model_number, po_line.description,
        ))),
        sell_price_paise=po_line.sell_price_paise,
        ordered_qty=po_line.ordered_qty,
    )


def _load_candidates(db: Session, po_id: int) -> list[_Candidate]:
    """OPEN PO lines for ``po_id`` with their Product identity, ready to score."""
    rows = db.execute(
        select(POLineItem, Product)
        .join(Product, Product.id == POLineItem.product_id)
        .where(POLineItem.po_id == po_id, POLineItem.line_status == LineStatus.OPEN.value)
        .order_by(POLineItem.id)
    ).all()
    return [_candidate_from(po_line, product) for po_line, product in rows]


def load_candidates_by_ids(db: Session, po_line_ids: set[int]) -> list[_Candidate]:
    """PO lines by EXPLICIT id (IGNORING ``line_status``) with their Product identity, ready to
    score. Used by the credit-note matcher: a CN credits the specific lines its referenced
    invoice billed, which are frequently CLOSED / SHORT_CLOSED — ``line_status`` must NOT filter
    them out (that is exactly the set a credit note legitimately reverses)."""
    if not po_line_ids:
        return []
    rows = db.execute(
        select(POLineItem, Product)
        .join(Product, Product.id == POLineItem.product_id)
        .where(POLineItem.id.in_(po_line_ids))
        .order_by(POLineItem.id)
    ).all()
    return [_candidate_from(po_line, product) for po_line, product in rows]


def match_invoice(db: Session, invoice: SalesInvoice) -> None:
    """Auto-match every UNMATCHED line of ``invoice`` to a PO line on its ``po_id``.

    Deterministic + idempotent: lines already MANUAL/MATCHED are left untouched (a re-run
    never clobbers an operator's override), and re-running over the same data yields the
    same assignment. A line that finds no confident PO line stays UNMATCHED (``po_line_item_id``
    NULL). With no PO (``po_id`` NULL) nothing matches — every line stays UNMATCHED.
    """
    if invoice.po_id is None:
        return

    # Pending invoice lines needing a proposal (preserve operator overrides).
    pending = [
        ln for ln in invoice.lines
        if ln.match_status == LineMatchStatus.UNMATCHED.value
    ]
    if not pending:
        return

    candidates = _load_candidates(db, invoice.po_id)
    # PO lines already claimed (by a prior auto/manual match on THIS invoice) are off-limits.
    taken: set[int] = {
        ln.po_line_item_id for ln in invoice.lines
        if ln.po_line_item_id is not None
    }

    # Rank every eligible pair once, then assign greedily 1:1 (stable tie-break).
    scored: list[tuple[float, int, int, SalesInvoiceLine, _Candidate]] = []
    for ln in pending:
        for cand in candidates:
            s = _score(ln, cand)
            if s >= MATCH_THRESHOLD:
                scored.append((s, ln.line_no, cand.po_line_id, ln, cand))
    # Highest score first; deterministic tie-break by (line_no, po_line_id).
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))

    used_lines: set[int] = set()
    for _s, _ln_no, po_line_id, ln, _cand in scored:
        if ln.id in used_lines or po_line_id in taken:
            continue
        ln.po_line_item_id = po_line_id
        ln.match_status = LineMatchStatus.MATCHED.value
        used_lines.add(ln.id)
        taken.add(po_line_id)


def apply_manual_match(
    db: Session, line: SalesInvoiceLine, po_line_item_id: int
) -> None:
    """Record an operator's manual mapping of ``line`` to a PO line (status → MANUAL).

    Pure state mutation — the caller (service layer) validates that the PO line exists and
    belongs to the invoice's PO before calling. ``db`` is accepted for signature symmetry
    with ``match_invoice`` (and future lookups); the assignment itself needs no query.
    """
    line.po_line_item_id = po_line_item_id
    line.match_status = LineMatchStatus.MANUAL.value
