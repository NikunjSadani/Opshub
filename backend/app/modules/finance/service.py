"""Finance (P&L) service — project + consolidated Profit & Loss, READ-ONLY.

Money-critical. Every figure is net-of-GST **taxable** value in integer PAISE, summed
only over CONFIRMED documents (an unconfirmed invoice / credit note is not yet a
committed financial fact). Nothing here writes.

The three money aggregates (revenue, revenue-reduction, cost) are each computed as a
SEPARATE single-grain aggregate so no join ever fan-outs and double-counts money:

  * Revenue      = Σ CONFIRMED ``billing_invoice.total_taxable_paise`` grouped by the
                   RESOLVED project COALESCE(``purchase_order.project_id`` [via a LEFT OUTER
                   join on ``invoice.po_id``], ``billing_invoice.project_id``) — a PO carries
                   the project, else the invoice's own direct attribution, else NULL (the
                   "unattributed" bucket for a PO-less invoice nobody attributed). Still a 1:1
                   join so money never multiplies. MINUS Σ CONFIRMED
                   ``billing_credit_note.total_taxable_paise`` reached through
                   ``credit_note.invoice_id → billing_invoice`` and resolved the SAME way, so a
                   CN reduces the exact bucket its invoice added to. The consolidated total sums
                   EVERY bucket (including the NULL/unattributed one), so it always reconciles
                   to Σ(all confirmed invoices) − Σ(all confirmed CNs) regardless of PO/project.
  * Cost         = Σ CONFIRMED ``expense_invoice.total_taxable_paise`` grouped by
                   ``expense_invoice.project_id``, SIGN-AWARE on ``doc_type``: an INVOICE
                   adds, a vendor CREDIT_NOTE subtracts (no join at all).
  * Margin       = revenue − cost;  margin % = margin / revenue * 100 (None if revenue ≤ 0).

The GEN / GEN-001 "General / Overhead" project (its client's code is ``GEN``) is the
general bucket for overhead + consolidated freight. It is NEVER split into client
projects: the per-project list excludes it, and it appears ONLY as a separate line in
the consolidated P&L. Consolidated totals = Σ over EVERY project (client projects +
the general bucket), so totals == Σ(project rows) + general bucket by construction.

Cross-module rows are reached by explicit ``select``/join (no ORM relationship), the
module-boundary pattern from ``expense.service.allocation_maps``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.modules.billing.models import (
    CreditNote,
    CreditNoteStatus,
    SalesInvoice,
    SalesInvoiceStatus,
)
from app.modules.expense.models import Invoice as ExpenseInvoice
from app.modules.expense.models import InvoiceStatus as ExpenseInvoiceStatus
from app.modules.projects.models import Project, ProjectClient
from app.modules.sales_orders.models import PurchaseOrder

# The client code whose project(s) form the general/overhead bucket. Mirrors
# app.seed.OVERHEAD_CLIENT_CODE (imported so the two can never drift).
from app.seed import OVERHEAD_CLIENT_CODE

_CONFIRMED_INVOICE = SalesInvoiceStatus.CONFIRMED.value
_CONFIRMED_CN = CreditNoteStatus.CONFIRMED.value
_CONFIRMED_EXPENSE = ExpenseInvoiceStatus.CONFIRMED.value


# --------------------------------------------------------------------- results

@dataclass(frozen=True)
class PnlLine:
    """A single P&L line (a project, the general bucket, or the consolidated total).
    All money is integer paise; ``margin_pct`` is None when revenue is 0 or negative."""

    revenue_paise: int
    cost_paise: int
    margin_paise: int
    margin_pct: float | None


@dataclass(frozen=True)
class ProjectPnl:
    """One project's P&L plus the identity needed to render/label it."""

    project_id: int
    project_code: str
    project_name: str
    client_id: int
    client_code: str
    client_name: str
    revenue_paise: int
    cost_paise: int
    margin_paise: int
    margin_pct: float | None


@dataclass(frozen=True)
class ConsolidatedPnl:
    """The whole-company P&L: per-project rows (GEN excluded), the general-bucket line
    (GEN only), the UNATTRIBUTED line (confirmed invoices/CNs with no PO and no direct
    project), and the totals over EVERY bucket (client projects + general + unattributed).

    ``unattributed`` is distinct from ``general_bucket``: GEN is the deliberate overhead
    project (a real client-coded project), whereas ``unattributed`` is the fallback for a
    PO-less invoice nobody attributed — it never has a project identity. Both are kept out of
    the per-project rows; both are folded into ``totals`` so the total always reconciles."""

    projects: list[ProjectPnl]
    general_bucket: PnlLine
    unattributed: PnlLine
    totals: PnlLine


@dataclass
class PnlFilters:
    """Optional filters for the per-project P&L list. ``date_from``/``date_to`` are
    INCLUSIVE and match a document by its own date (billing invoice_date, credit-note
    cn_date, expense invoice_date); ``client_id`` restricts to one client's projects."""

    client_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None


# ----------------------------------------------------------------- math helpers

def _margin_pct(revenue_paise: int, margin_paise: int) -> float | None:
    """margin / revenue * 100, or None when revenue is 0 OR NEGATIVE (undefined).

    A negative revenue (an over-credited project) would make ``margin / revenue`` flip sign and
    report a misleadingly POSITIVE margin %, so a non-positive revenue yields None (rendered as
    "—"), never a spurious percentage."""
    if revenue_paise <= 0:
        return None
    return round(margin_paise / revenue_paise * 100, 2)


def _line(revenue_paise: int, cost_paise: int) -> PnlLine:
    margin = revenue_paise - cost_paise
    return PnlLine(
        revenue_paise=revenue_paise,
        cost_paise=cost_paise,
        margin_paise=margin,
        margin_pct=_margin_pct(revenue_paise, margin),
    )


# --------------------------------------------------------------- money aggregates

# The resolved project for a billing invoice: the PO's project (reached by a LEFT OUTER join
# on ``SalesInvoice.po_id``) when the invoice has a PO, ELSE the invoice's own direct
# ``project_id`` attribution. NULL only when the invoice has neither — the "unattributed"
# bucket. COALESCE preserves the money invariant: every confirmed invoice lands in exactly one
# bucket (some real project, or the NULL bucket), so Σ over all buckets == Σ all invoices.
_RESOLVED_INVOICE_PROJECT = func.coalesce(PurchaseOrder.project_id, SalesInvoice.project_id)


def _billing_invoice_revenue(
    db: Session, *, date_from: date | None, date_to: date | None
) -> dict[int | None, int]:
    """{resolved_project_id -> Σ CONFIRMED billing-invoice taxable paise}, attributed to
    COALESCE(PO.project_id, invoice.project_id). The PO join is a LEFT OUTER PK join (invoice →
    at most one PO), so money is never multiplied and a PO-less invoice is retained. The key is
    ``None`` for a fully-unattributed invoice (no PO and no direct project) — the caller sums
    that bucket into the consolidated total but excludes it from per-project rows."""
    amt = func.coalesce(SalesInvoice.total_taxable_paise, 0)
    stmt = (
        select(_RESOLVED_INVOICE_PROJECT, func.coalesce(func.sum(amt), 0))
        .select_from(SalesInvoice)
        .join(PurchaseOrder, PurchaseOrder.id == SalesInvoice.po_id, isouter=True)
        .where(SalesInvoice.status == _CONFIRMED_INVOICE)
    )
    if date_from is not None:
        stmt = stmt.where(SalesInvoice.invoice_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(SalesInvoice.invoice_date <= date_to)
    stmt = stmt.group_by(_RESOLVED_INVOICE_PROJECT)
    return {pid: int(total) for pid, total in db.execute(stmt)}


def _billing_credit_note_reduction(
    db: Session, *, date_from: date | None, date_to: date | None
) -> dict[int | None, int]:
    """{resolved_project_id -> Σ CONFIRMED client credit-note taxable paise}, attributed to the
    credited invoice's resolved project COALESCE(PO.project_id, invoice.project_id) — the SAME
    resolution as the revenue side, so a CN always reduces the exact bucket its invoice added
    to. 1:1 PK joins (CN → one invoice; invoice → at most one PO via LEFT OUTER). The credited
    invoice must itself be CONFIRMED — symmetric with ``_billing_invoice_revenue`` — so a CN can
    never reduce revenue an invoice never contributed. Key is ``None`` for the unattributed
    bucket (an unattributed invoice's CN)."""
    amt = func.coalesce(CreditNote.total_taxable_paise, 0)
    stmt = (
        select(_RESOLVED_INVOICE_PROJECT, func.coalesce(func.sum(amt), 0))
        .select_from(CreditNote)
        .join(SalesInvoice, SalesInvoice.id == CreditNote.invoice_id)
        .join(PurchaseOrder, PurchaseOrder.id == SalesInvoice.po_id, isouter=True)
        .where(
            CreditNote.status == _CONFIRMED_CN,
            SalesInvoice.status == _CONFIRMED_INVOICE,
        )
    )
    if date_from is not None:
        stmt = stmt.where(CreditNote.cn_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(CreditNote.cn_date <= date_to)
    stmt = stmt.group_by(_RESOLVED_INVOICE_PROJECT)
    return {pid: int(total) for pid, total in db.execute(stmt)}


def _expense_cost(
    db: Session, *, date_from: date | None, date_to: date | None
) -> dict[int, int]:
    """{project_id -> Σ CONFIRMED expense taxable paise}, SIGN-AWARE on doc_type: an
    INVOICE adds, a vendor CREDIT_NOTE subtracts, anything else contributes 0. No join
    (project_id is on the expense row), so no fan-out."""
    amt = func.coalesce(ExpenseInvoice.total_taxable_paise, 0)
    signed = case(
        (ExpenseInvoice.doc_type == "INVOICE", amt),
        (ExpenseInvoice.doc_type == "CREDIT_NOTE", -amt),
        else_=0,
    )
    stmt = (
        select(ExpenseInvoice.project_id, func.coalesce(func.sum(signed), 0))
        .where(ExpenseInvoice.status == _CONFIRMED_EXPENSE)
    )
    if date_from is not None:
        stmt = stmt.where(ExpenseInvoice.invoice_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(ExpenseInvoice.invoice_date <= date_to)
    stmt = stmt.group_by(ExpenseInvoice.project_id)
    return {pid: int(total) for pid, total in db.execute(stmt) if pid is not None}


def _revenue_map(
    db: Session, *, date_from: date | None, date_to: date | None
) -> dict[int | None, int]:
    """Net revenue per resolved project = confirmed billing invoices − confirmed credit notes.
    The ``None`` key is the unattributed bucket (invoices with no PO and no direct project);
    the caller sums it into the consolidated total but excludes it from per-project rows."""
    invoices = _billing_invoice_revenue(db, date_from=date_from, date_to=date_to)
    credits = _billing_credit_note_reduction(db, date_from=date_from, date_to=date_to)
    out: dict[int | None, int] = defaultdict(int)
    for pid, amt in invoices.items():
        out[pid] += amt
    for pid, amt in credits.items():
        out[pid] -= amt
    return dict(out)


def _general_bucket_project_ids(db: Session) -> set[int]:
    """The project ids that form the general/overhead bucket (client code == GEN)."""
    return set(
        db.execute(
            select(Project.id)
            .join(ProjectClient, ProjectClient.id == Project.client_id)
            .where(ProjectClient.code == OVERHEAD_CLIENT_CODE)
        ).scalars()
    )


def _project_identity(
    db: Session, project_ids: set[int]
) -> dict[int, tuple[Project, ProjectClient]]:
    """{project_id -> (project, client)} for the given ids, one query (no N+1)."""
    if not project_ids:
        return {}
    rows = db.execute(
        select(Project, ProjectClient)
        .join(ProjectClient, ProjectClient.id == Project.client_id)
        .where(Project.id.in_(project_ids))
    )
    return {proj.id: (proj, client) for proj, client in rows}


def _project_row(
    project: Project, client: ProjectClient, revenue_paise: int, cost_paise: int
) -> ProjectPnl:
    margin = revenue_paise - cost_paise
    return ProjectPnl(
        project_id=project.id,
        project_code=project.code,
        project_name=project.name,
        client_id=client.id,
        client_code=client.code,
        client_name=client.name,
        revenue_paise=revenue_paise,
        cost_paise=cost_paise,
        margin_paise=margin,
        margin_pct=_margin_pct(revenue_paise, margin),
    )


# --------------------------------------------------------------------- public API

def project_pnl(db: Session, project_id: int) -> PnlLine:
    """One project's P&L breakdown (revenue, cost, margin, margin_pct) in paise.

    Works for ANY project id (a client project or the general bucket); the caller is
    responsible for confirming the project exists. A project with no confirmed activity
    yields an all-zero line (margin_pct None)."""
    revenue = _revenue_map(db, date_from=None, date_to=None).get(project_id, 0)
    cost = _expense_cost(db, date_from=None, date_to=None).get(project_id, 0)
    return _line(revenue, cost)


def pnl_by_project(db: Session, filters: PnlFilters | None = None) -> list[ProjectPnl]:
    """Per-project P&L rows for every project with confirmed activity, EXCLUDING the
    general bucket (its overhead never splits into a client project). Ordered by project
    code. ``filters`` narrows by client and/or an inclusive document-date range."""
    filters = filters or PnlFilters()
    revenue = _revenue_map(db, date_from=filters.date_from, date_to=filters.date_to)
    cost = _expense_cost(db, date_from=filters.date_from, date_to=filters.date_to)
    general = _general_bucket_project_ids(db)

    # Drop the general bucket AND the unattributed (None) bucket — neither is a client row.
    active_ids = {pid for pid in (set(revenue) | set(cost)) if pid is not None} - general
    identity = _project_identity(db, active_ids)
    rows: list[ProjectPnl] = []
    for pid in active_ids:
        pair = identity.get(pid)
        if pair is None:  # a project row vanished under us — skip rather than 500
            continue
        project, client = pair
        if filters.client_id is not None and client.id != filters.client_id:
            continue
        rows.append(_project_row(project, client, revenue.get(pid, 0), cost.get(pid, 0)))
    rows.sort(key=lambda r: r.project_code)
    return rows


def consolidated_pnl(db: Session) -> ConsolidatedPnl:
    """The whole-company P&L: per-project rows (GEN + unattributed excluded), the
    general-bucket line (GEN only), the unattributed line (no-PO-no-project confirmed
    invoices/CNs), and totals over EVERY bucket so
    totals == Σ(rows) + general bucket + unattributed."""
    revenue = _revenue_map(db, date_from=None, date_to=None)
    cost = _expense_cost(db, date_from=None, date_to=None)
    general = _general_bucket_project_ids(db)

    # Per-project rows (client projects with activity) — exclude GEN and the None bucket.
    project_ids = {pid for pid in (set(revenue) | set(cost)) if pid is not None} - general
    identity = _project_identity(db, project_ids)
    projects = [
        _project_row(project, client, revenue.get(pid, 0), cost.get(pid, 0))
        for pid, (project, client) in identity.items()
    ]
    projects.sort(key=lambda r: r.project_code)

    # General bucket = the GEN project(s) aggregated into one line.
    gen_revenue = sum(revenue.get(pid, 0) for pid in general)
    gen_cost = sum(cost.get(pid, 0) for pid in general)
    general_bucket = _line(gen_revenue, gen_cost)

    # Unattributed = the None bucket: confirmed invoices/CNs with no PO and no direct project.
    # No expense ever lands here (cost keys are always real project ids), so its cost is 0.
    unattributed = _line(revenue.get(None, 0), 0)

    # Totals over EVERY bucket (client projects + general + unattributed) — reconciles to
    # Σ(rows) + general bucket + unattributed. Summing the full maps includes the None bucket
    # on the revenue side, so the company total can never silently drop a PO-less invoice.
    total_revenue = sum(revenue.values())
    total_cost = sum(cost.values())
    totals = _line(total_revenue, total_cost)

    return ConsolidatedPnl(
        projects=projects, general_bucket=general_bucket,
        unattributed=unattributed, totals=totals,
    )


# --------------------------------------------------------------------- CSV export

def pnl_csv(rows: list[ProjectPnl]) -> bytes:
    """Render the per-project P&L to CSV. Text-derived fields (code/name/client) are
    CSV-injection-guarded; money is a plain 2dp rupee decimal a spreadsheet can sum."""
    header = (
        "Project Code,Project Name,Client Code,Client,"
        "Revenue (INR),Cost (INR),Margin (INR),Margin %"
    )
    lines = [header]
    for r in rows:
        pct = "" if r.margin_pct is None else f"{r.margin_pct:.2f}"
        lines.append(",".join((
            _csv_field(r.project_code),
            _csv_field(r.project_name),
            _csv_field(r.client_code),
            _csv_field(r.client_name),
            f'"{_rupees(r.revenue_paise)}"',
            f'"{_rupees(r.cost_paise)}"',
            f'"{_rupees(r.margin_paise)}"',
            f'"{pct}"',
        )))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _rupees(paise: int) -> str:
    """Paise -> a signed 2dp rupee decimal (money is SIGNED — a margin can be negative)."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}{whole}.{frac:02d}"


def _csv_field(value: str) -> str:
    """Quote a CSV field and neutralize spreadsheet formula/DDE injection (RFC 4180
    quote-doubling; a leading = + - @ / control char is prefixed with `'`)."""
    value = value.replace('"', '""')
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        value = "'" + value
    return f'"{value}"'
