"""Action Center service — a READ-ONLY, computed "what needs attention" view.

Nothing here writes and nothing is stored: the three categories are derived ON READ
from the existing Project-Spine data (POs, PO lines, the §6 invoiced-qty rollup, and
the AR register). No tables, no scheduler — the push/scheduled-email version is the
deferred owner-gated cloud wave.

The three categories (all dates relative to ``date.today()`` — never a hardcoded date):

1. procurement    — POs whose ``expected_procurement_date`` is set and falls within the
                    horizon (``<= today + horizon_days``) and are still live (status not
                    CLOSED / CANCELLED). ``days_until`` is negative when already past.
2. invoicing_due  — CONFIRMED / IN_PROGRESS POs that still carry unbilled units. Per OPEN
                    line ``open_qty = ordered_qty − invoiced_qty_for_po_line`` clamped at 0
                    (reusing billing's §6 net-of-CN rollup); retired (SHORT_CLOSED / CLOSED)
                    lines are skipped. A PO is included when Σ open_qty > 0. Integer paise.
3. ar_overdue     — CONFIRMED client invoices past ``due_date`` with outstanding > 0,
                    straight off ``ar_service.ar_register(overdue=True)`` plus days-overdue.

Cross-module identity (client name / project code) is resolved by explicit join — the
module-boundary pattern from ``finance.service`` / ``expense.service.allocation_maps`` —
never via a cross-module ORM relationship.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.ar_service import ar_register
from app.modules.billing.invoice_service import invoiced_qty_for_po_line
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import LineStatus, POStatus, PurchaseOrder

_ZERO = Decimal("0")


# --------------------------------------------------------------------- results

@dataclass(frozen=True)
class ProcurementItem:
    """A PO whose expected procurement date is within the horizon (or already past)."""

    po_id: int
    po_number: str
    client_name: str | None
    project_code: str | None
    expected_procurement_date: date
    days_until: int  # (expected_procurement_date − today).days; negative once past


@dataclass(frozen=True)
class InvoicingDueItem:
    """A PO still carrying unbilled units (Σ open-to-invoice > 0)."""

    po_id: int
    po_number: str
    client_name: str | None
    project_code: str | None
    uninvoiced_qty: Decimal          # Σ per-line clamped open_qty
    uninvoiced_value_paise: int      # Σ (open_qty × sell_price_paise), ROUND_HALF_UP


@dataclass(frozen=True)
class ArOverdueItem:
    """A CONFIRMED client invoice past its due date with a positive outstanding balance."""

    invoice_id: int
    invoice_number: str
    client_name: str | None
    outstanding_paise: int
    due_date: date
    days_overdue: int                # (today − due_date).days, > 0 for an overdue row
    aging_bucket: str


@dataclass(frozen=True)
class ActionCenter:
    """The combined "what needs attention" payload (three most-urgent-first lists)."""

    procurement: list[ProcurementItem] = field(default_factory=list)
    invoicing_due: list[InvoicingDueItem] = field(default_factory=list)
    ar_overdue: list[ArOverdueItem] = field(default_factory=list)


# ----------------------------------------------------------------- identity joins

def _client_names(db: Session, client_ids: set[int]) -> dict[int, str]:
    """{client_id -> name} for the given ids in one query (mirrors the finance join)."""
    if not client_ids:
        return {}
    rows = db.execute(
        select(ProjectClient.id, ProjectClient.name).where(ProjectClient.id.in_(client_ids))
    )
    return {cid: name for cid, name in rows}


def _project_codes(db: Session, project_ids: set[int]) -> dict[int, str]:
    """{project_id -> code} for the given ids in one query."""
    if not project_ids:
        return {}
    rows = db.execute(
        select(Project.id, Project.code).where(Project.id.in_(project_ids))
    )
    return {pid: code for pid, code in rows}


# --------------------------------------------------------------------- categories

def procurement_followups(db: Session, horizon_days: int) -> list[ProcurementItem]:
    """POs due for procurement within ``horizon_days`` (or already past), still live.

    Included when ``expected_procurement_date`` is set and ``<= today + horizon_days`` AND
    status is neither CLOSED nor CANCELLED. Ordered most-urgent first (``days_until`` asc,
    so the most overdue lead the list; ``po_id`` breaks ties deterministically)."""
    today = date.today()
    cutoff = today + timedelta(days=horizon_days)
    pos = db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.expected_procurement_date.is_not(None),
            PurchaseOrder.expected_procurement_date <= cutoff,
            PurchaseOrder.status.not_in(
                [POStatus.CLOSED.value, POStatus.CANCELLED.value]
            ),
        )
    ).scalars().all()

    names = _client_names(db, {po.client_id for po in pos})
    codes = _project_codes(db, {po.project_id for po in pos})

    items = [
        ProcurementItem(
            po_id=po.id,
            po_number=po.po_number,
            client_name=names.get(po.client_id),
            project_code=codes.get(po.project_id),
            # expected_procurement_date is non-null by the query filter above.
            expected_procurement_date=po.expected_procurement_date,  # type: ignore[arg-type]
            days_until=(po.expected_procurement_date - today).days,  # type: ignore[operator]
        )
        for po in pos
    ]
    items.sort(key=lambda it: (it.days_until, it.po_id))
    return items


def invoicing_due(db: Session) -> list[InvoicingDueItem]:
    """Live POs (status CONFIRMED or IN_PROGRESS) that still have units to invoice.

    Only OPEN lines carry units to invoice; SHORT_CLOSED / CLOSED lines are retired and
    skipped. Per OPEN line ``open_qty = ordered_qty − invoiced_qty_for_po_line(line.id)``,
    clamped at 0 (the §6 rollup already nets confirmed credit notes and floors at 0); a PO is
    included when Σ open_qty > 0. Money is integer paise: ``uninvoiced_value_paise`` sums
    ``open_qty × sell_price_paise`` (Decimal) then rounds HALF_UP to paise — no float. Ordered
    most-urgent first (largest uninvoiced value; ``po_id`` breaks ties).

    Only a CONFIRMED / IN_PROGRESS PO is "invoicing due": a DRAFT PO has not been confirmed
    into the live pipeline yet (now that the confirm lifecycle exists, it is no longer the
    working state), and CLOSED / CANCELLED are terminal."""
    pos = db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.status.in_([POStatus.CONFIRMED.value, POStatus.IN_PROGRESS.value])
        )
    ).scalars().all()

    items: list[InvoicingDueItem] = []
    pending: list[tuple[PurchaseOrder, Decimal, int]] = []
    for po in pos:
        total_open = _ZERO
        total_value = _ZERO
        for line in po.lines:
            # Only an OPEN line has units left to invoice. A SHORT_CLOSED / CLOSED line is
            # retired — never invoicing-due, even if a later credit note nets its invoiced
            # qty back down (which would otherwise make ordered − invoiced go positive again).
            if line.line_status != LineStatus.OPEN.value:
                continue
            open_qty = line.ordered_qty - invoiced_qty_for_po_line(db, line.id)
            if open_qty <= _ZERO:
                continue
            total_open += open_qty
            total_value += open_qty * line.sell_price_paise
        if total_open > _ZERO:
            value_paise = int(total_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            pending.append((po, total_open, value_paise))

    names = _client_names(db, {po.client_id for po, _, _ in pending})
    codes = _project_codes(db, {po.project_id for po, _, _ in pending})
    for po, total_open, value_paise in pending:
        items.append(InvoicingDueItem(
            po_id=po.id,
            po_number=po.po_number,
            client_name=names.get(po.client_id),
            project_code=codes.get(po.project_id),
            uninvoiced_qty=total_open,
            uninvoiced_value_paise=value_paise,
        ))
    items.sort(key=lambda it: (-it.uninvoiced_value_paise, it.po_id))
    return items


def ar_overdue(db: Session, today: date) -> list[ArOverdueItem]:
    """CONFIRMED client invoices past ``due_date`` with outstanding > 0.

    Reuses ``ar_service.ar_register(overdue=True)`` (the single money identity for
    outstanding / aging) and adds ``days_overdue``. Ordered most-urgent first (most days
    overdue; ``invoice_id`` breaks ties)."""
    rows = ar_register(db, overdue=True, today=today)
    names = _client_names(db, {r.client_id for r in rows})
    items: list[ArOverdueItem] = []
    for r in rows:
        if r.due_date is None:  # an overdue row always has a due date — guard for typing
            continue
        items.append(ArOverdueItem(
            invoice_id=r.invoice_id,
            invoice_number=r.invoice_number,
            client_name=names.get(r.client_id),
            outstanding_paise=r.outstanding_paise,
            due_date=r.due_date,
            days_overdue=(today - r.due_date).days,
            aging_bucket=r.aging_bucket or "",
        ))
    items.sort(key=lambda it: (-it.days_overdue, it.invoice_id))
    return items


def action_center(db: Session, horizon_days: int) -> ActionCenter:
    """The combined read: procurement follow-ups + invoicing-due + AR-overdue.

    ``today`` is computed once and threaded into the AR read so every category reflects the
    same day."""
    today = date.today()
    return ActionCenter(
        procurement=procurement_followups(db, horizon_days),
        invoicing_due=invoicing_due(db),
        ar_overdue=ar_overdue(db, today),
    )
