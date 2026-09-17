"""Purchase Orders service — the client-PO backbone of the Project Spine.

A ``PurchaseOrder`` is a client's own reference document (``po_number`` is supplied
BY the client, unique within that client) that lives under one project and carries
priced ``POLineItem`` rows. Every structural change is preserved: an amendment
SNAPSHOTS the whole PO+lines into an immutable ``POAmendment`` version BEFORE the
mutation is applied, a short-close retires open-to-invoice quantity (never deletes),
and a void flips the PO to CANCELLED — history is an asset, so nothing is ever
hard-deleted.

Integrity discipline (mirrors the projects/products services):

  * The ``(client_id, po_number)`` UNIQUE is the DB backstop: a create/amend runs
    inside a ``with db.begin_nested():`` SAVEPOINT and a collision at flush is
    surfaced as ``DuplicatePO`` (route -> 409). Cross-module references (project,
    client, client GSTIN, stored file) are validated up-front by explicit SELECT so
    a bad FK is a clean 400/404 rather than a misread IntegrityError.
  * Client / project / product must all be ACTIVE at create time.
  * Money is integer PAISE; quantities + tax rate are ``Decimal``. Every mutation is
    audited inside the caller's transaction. The caller (route) owns ``commit``.

⚠️ ``db.begin_nested()`` MUST be used as ``with db.begin_nested():`` — a bare call
leaks a SAVEPOINT per insert and a large batch overflows commit recursion.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from openpyxl import Workbook, load_workbook
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.modules.challan.parsing import parse_paise, parse_qty
from app.modules.files.models import StoredFile
from app.modules.projects.models import ClientGstin, Project, ProjectClient, ProjectStatus
from app.modules.sales_orders.models import (
    LineStatus,
    POAmendment,
    POLineItem,
    POStatus,
    Product,
    PurchaseOrder,
)
from app.platform import audit

# --------------------------------------------------------------------- bounds
# Mirror challan.parsing._MAX_MONEY_PAISE (= Rs 10,000,000,000,000): a single money
# cell above this is a clean validation error, never an int8-overflow DB 500.
_MAX_MONEY_PAISE = 10**15
# ordered_qty / short_closed_qty map to Numeric(18,3): 15 integer digits, 3 decimals.
_MAX_QTY = Decimal(10) ** 15  # exclusive ceiling
_QTY_MAX_DECIMALS = 3
_MAX_PO_NUMBER_LEN = 64
_MAX_DESCRIPTION_LEN = 500
_MAX_UOM_LEN = 20
_MAX_NOTES_LEN = 1000
_MAX_REASON_LEN = 500
_MONEY_FIELDS = (
    "cost_price_paise", "original_cost_price_paise",
    "client_sell_price_paise", "vendor_sell_price_paise", "sell_price_paise",
    "client_freight_paise", "vendor_freight_paise", "freight_paise",
    "packaging_paise", "handling_paise", "other_paise",
)
_AGENCY_FEE_TYPES = ("NONE", "PERCENT", "FIXED")
_AGENCY_KEYS = frozenset({
    "agency_fee_type", "agency_fee_percent", "agency_fee_amount_paise",
})


# --------------------------------------------------------------------- errors

class POError(Exception):
    """Base for purchase-order errors."""


class PONotFound(POError):
    """A referenced PO / line does not exist (route -> 404)."""


class DuplicatePO(POError):
    """``(client_id, po_number)`` already exists (route -> 409)."""


class POValidationError(POError):
    """A malformed / illegal PO operation (route -> 400, or 422 for a blocked amend)."""


# ------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class LineInput:
    """One line to create on a PO. ``description``/``uom`` fall back to the product's
    name/uom when omitted. Money is per-UNIT (cost/sell tiers) or per-LINE (the rest).

    Pricing tiers:
      * ``cost_price_paise``           — Our CP (billed to us), visible, required.
      * ``original_cost_price_paise``  — Original CP, visible, optional.
      * ``client_sell_price_paise``    — client-quoted sell, visible, REQUIRED.
      * ``vendor_sell_price_paise``    — vendor sell, visible, optional.
      * ``sell_price_paise``           — ACTUAL sell, ADMIN-ONLY. ``None`` => default to the
                                         client sell (a non-admin can never diverge it).
      * ``client_freight_paise``       — client freight, visible (default 0).
      * ``vendor_freight_paise``       — vendor freight, visible, optional.
      * ``freight_paise``              — ACTUAL freight, ADMIN-ONLY. ``None`` => default to
                                         the client freight.
    """

    product_id: int
    ordered_qty: Decimal
    cost_price_paise: int
    client_sell_price_paise: int
    description: str | None = None
    uom: str | None = None
    original_cost_price_paise: int | None = None
    vendor_sell_price_paise: int | None = None
    sell_price_paise: int | None = None
    client_freight_paise: int | None = 0
    vendor_freight_paise: int | None = None
    freight_paise: int | None = None
    packaging_paise: int = 0
    handling_paise: int = 0
    other_paise: int = 0
    tax_rate: Decimal = Decimal("0")


@dataclass
class BulkResult:
    """Outcome of an Excel bulk create: PO numbers created, PO numbers skipped (with
    reason), and per-row errors (1-based sheet row + reason)."""

    created: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)


# ------------------------------------------------------------- fk validation

def _ensure_client_active(db: Session, client_id: int) -> ProjectClient:
    client = db.get(ProjectClient, client_id)
    if client is None:
        raise PONotFound(f"client {client_id} not found")
    if not client.active:
        raise POValidationError(f"client {client_id} is not active")
    return client


def _ensure_project_active(db: Session, project_id: int, client_id: int) -> Project:
    """A referenced project must exist, be ACTIVE, AND belong to this PO's client.

    The client↔project linkage check is the spine invariant: a PO's client and its
    project's client must agree, or downstream P&L / quote joins surface a mismatched
    pair. (Mirrors the ownership check the GSTIN reference already enforces.)"""
    project = db.execute(
        select(Project).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise PONotFound(f"project {project_id} not found")
    if project.client_id != client_id:
        raise POValidationError("project does not belong to this client")
    if project.status != ProjectStatus.ACTIVE.value:
        raise POValidationError(f"project {project_id} is not Active")
    return project


def _ensure_gstin(db: Session, client_id: int, gstin_id: int) -> None:
    """A referenced client GSTIN must exist, belong to this client, AND be active.

    Rejecting an inactive (soft-deleted) GSTIN on a NEW/amended reference mirrors the
    inactive-product rule; a historical PO keeps its old reference via ``label_maps``."""
    row = db.get(ClientGstin, gstin_id)
    if row is None:
        raise PONotFound(f"client gstin {gstin_id} not found")
    if row.client_id != client_id:
        raise POValidationError("client_gstin_id does not belong to this client")
    if not row.active:
        raise POValidationError("client_gstin_id is inactive")


def _ensure_file(db: Session, file_id: int) -> None:
    if db.get(StoredFile, file_id) is None:
        raise PONotFound(f"file {file_id} not found")


# --------------------------------------------------------------- validation

def _qty_ok(qty: Decimal) -> bool:
    """A quantity is valid when it is > 0 and fits Numeric(18,3)."""
    if qty <= 0 or qty >= _MAX_QTY:
        return False
    exponent = qty.as_tuple().exponent
    return not (isinstance(exponent, int) and -exponent > _QTY_MAX_DECIMALS)


def _validate_line_numbers(li: LineInput) -> None:
    """Enforce qty > 0 + column-fit, money >= 0 + ceiling, tax 0..100 for a direct
    service caller (the route's pydantic guards most of this at the edge). The
    client-quoted sell is REQUIRED; the optional tiers only validate when present."""
    if not _qty_ok(li.ordered_qty):
        raise POValidationError(
            "ordered_qty must be a positive number that fits 15 digits and 3 decimals")
    if li.client_sell_price_paise is None:
        raise POValidationError("client_sell_price_paise is required")
    for attr in _MONEY_FIELDS:
        value = getattr(li, attr)
        if value is None:  # optional tier omitted — nothing to bound
            continue
        if value < 0:
            raise POValidationError(f"{attr} must not be negative")
        if value > _MAX_MONEY_PAISE:
            raise POValidationError(f"{attr} is too large")
    if li.tax_rate < 0 or li.tax_rate > 100:
        raise POValidationError("tax_rate must be between 0 and 100")
    # tax_rate is Numeric(5,2): reject an over-scale value rather than let Postgres silently
    # round it (this guard also covers the bulk-.xlsx path, which routes through create_po).
    if li.tax_rate != li.tax_rate.quantize(Decimal("0.01")):
        raise POValidationError("tax_rate supports at most 2 decimal places")


def _validate_agency_fee(
    fee_type: str, percent: Decimal | None, amount: int | None
) -> None:
    """A PO-level agency fee is one of NONE / PERCENT / FIXED, with EXACTLY the matching
    figure set and the other null: PERCENT ⇒ percent in [0,100] and amount null;
    FIXED ⇒ amount in [0, ceiling] and percent null; NONE ⇒ both null."""
    if fee_type not in _AGENCY_FEE_TYPES:
        raise POValidationError(
            f"agency_fee_type must be one of {_AGENCY_FEE_TYPES}")
    if fee_type == "PERCENT":
        if percent is None:
            raise POValidationError("agency_fee_percent is required for a PERCENT agency fee")
        if percent < 0 or percent > 100:
            raise POValidationError("agency_fee_percent must be between 0 and 100")
        if amount is not None:
            raise POValidationError(
                "agency_fee_amount_paise must be null for a PERCENT agency fee")
    elif fee_type == "FIXED":
        if amount is None:
            raise POValidationError("agency_fee_amount_paise is required for a FIXED agency fee")
        if amount < 0 or amount > _MAX_MONEY_PAISE:
            raise POValidationError(
                "agency_fee_amount_paise must be between 0 and the ceiling")
        if percent is not None:
            raise POValidationError("agency_fee_percent must be null for a FIXED agency fee")
    else:  # NONE
        if percent is not None or amount is not None:
            raise POValidationError(
                "a NONE agency fee must not set agency_fee_percent or agency_fee_amount_paise")


def _resolve_line(
    db: Session, li: LineInput, *, can_set_actuals: bool,
    carry_actual_from: POLineItem | None = None,
) -> POLineItem:
    """Validate a line's numbers + product (must exist and be active), snapshotting
    the description (product name when omitted) and uom (product uom when omitted).

    ACTUAL-FIELD RULE (critical): the actual sell/freight (``sell_price_paise`` /
    ``freight_paise``, both ADMIN-ONLY and NOT NULL) may only diverge from the client
    figure when ``can_set_actuals`` is True. For a non-admin caller — or an admin who
    omits them — the actual DEFAULTS to the client value, so a non-admin can never set an
    actual that differs from the client-quoted one.

    ``carry_actual_from`` (amend only): the admin-set actuals cannot be seen or re-sent by a
    non-admin, and even an admin editing (say) a quantity typo need not re-type them — so
    when the amended line OMITS an actual we PRESERVE the matching existing line's actual
    instead of silently RESETTING it to the client figure (which would destroy an admin's
    recorded margin). The caller matches the old line by PRODUCT (not list position), so a
    reorder/insert no longer loses the margin; a genuinely new product still defaults to
    client. An admin who EXPLICITLY sends an actual always overrides the carry."""
    _validate_line_numbers(li)
    product = db.get(Product, li.product_id)
    if product is None:
        raise PONotFound(f"product {li.product_id} not found")
    if not product.active:
        raise POValidationError(f"product {li.product_id} is not active")
    description = (li.description or product.name).strip()[:_MAX_DESCRIPTION_LEN]
    uom = (li.uom or product.uom).strip()[:_MAX_UOM_LEN] or product.uom
    client_sell = li.client_sell_price_paise
    client_freight = li.client_freight_paise if li.client_freight_paise is not None else 0
    # Actual resolution order: (1) an admin's EXPLICIT value wins; (2) else carry the prior
    # line's actual forward (admins who omit + non-admins alike — preserves recorded margin
    # across a reorder/qty edit); (3) else default to the client figure (new line / no prior).
    if can_set_actuals and li.sell_price_paise is not None:
        actual_sell = li.sell_price_paise
    elif carry_actual_from is not None:
        actual_sell = carry_actual_from.sell_price_paise
    else:
        actual_sell = client_sell
    if can_set_actuals and li.freight_paise is not None:
        actual_freight = li.freight_paise
    elif carry_actual_from is not None:
        actual_freight = carry_actual_from.freight_paise
    else:
        actual_freight = client_freight
    return POLineItem(
        product_id=product.id,
        description=description,
        uom=uom,
        ordered_qty=li.ordered_qty,
        cost_price_paise=li.cost_price_paise,
        original_cost_price_paise=li.original_cost_price_paise,
        client_sell_price_paise=client_sell,
        vendor_sell_price_paise=li.vendor_sell_price_paise,
        sell_price_paise=actual_sell,
        client_freight_paise=client_freight,
        vendor_freight_paise=li.vendor_freight_paise,
        freight_paise=actual_freight,
        packaging_paise=li.packaging_paise,
        handling_paise=li.handling_paise,
        other_paise=li.other_paise,
        tax_rate=li.tax_rate,
        line_status=LineStatus.OPEN.value,
        short_closed_qty=Decimal("0"),
    )


def _clean_po_number(po_number: str | None) -> str | None:
    """Normalize an OPTIONAL client PO number: blank / None ⇒ ``None`` (stored NULL, not an
    error), otherwise whitespace-collapsed and length-bounded."""
    if po_number is None:
        return None
    cleaned = " ".join(po_number.split())
    if not cleaned:
        return None
    if len(cleaned) > _MAX_PO_NUMBER_LEN:
        raise POValidationError(f"po_number must be at most {_MAX_PO_NUMBER_LEN} characters")
    return cleaned


# ------------------------------------------------------------------- create

def create_po(
    db: Session,
    *,
    po_number: str | None,
    client_id: int,
    project_id: int,
    po_date: date,
    lines: list[LineInput],
    client_gstin_id: int | None = None,
    expected_procurement_date: date | None = None,
    notes: str | None = None,
    soft_copy_file_id: int | None = None,
    agency_fee_type: str = "NONE",
    agency_fee_percent: Decimal | None = None,
    agency_fee_amount_paise: int | None = None,
    can_set_actuals: bool = False,
    actor_uid: str | None = None,
) -> PurchaseOrder:
    """Create a PO with its lines. Validates client + project + every product are
    ACTIVE and the optional GSTIN/file references resolve, then inserts under a
    SAVEPOINT so a ``(client_id, po_number)`` collision surfaces as ``DuplicatePO``.
    ``po_number`` is OPTIONAL (NULL when absent); the actual sell/freight per line only
    diverge from the client figure when ``can_set_actuals`` (admin). Audited ``po.created``.
    Caller commits."""
    po_number = _clean_po_number(po_number)
    if not lines:
        raise POValidationError("at least one line item is required")
    _validate_agency_fee(agency_fee_type, agency_fee_percent, agency_fee_amount_paise)
    _ensure_client_active(db, client_id)
    _ensure_project_active(db, project_id, client_id)
    if client_gstin_id is not None:
        _ensure_gstin(db, client_id, client_gstin_id)
    if soft_copy_file_id is not None:
        _ensure_file(db, soft_copy_file_id)

    resolved = [_resolve_line(db, li, can_set_actuals=can_set_actuals) for li in lines]

    po = PurchaseOrder(
        po_number=po_number,
        client_id=client_id,
        client_gstin_id=client_gstin_id,
        project_id=project_id,
        soft_copy_file_id=soft_copy_file_id,
        po_date=po_date,
        expected_procurement_date=expected_procurement_date,
        status=POStatus.DRAFT.value,
        notes=(notes.strip()[:_MAX_NOTES_LEN] if notes else None),
        agency_fee_type=agency_fee_type,
        agency_fee_percent=agency_fee_percent,
        agency_fee_amount_paise=agency_fee_amount_paise,
        created_by=actor_uid,
    )
    po.lines.extend(resolved)
    try:
        with db.begin_nested():
            db.add(po)
            db.flush()
    except IntegrityError as exc:
        raise DuplicatePO(
            f"a PO '{po_number}' already exists for this client") from exc

    audit.log(
        db,
        action="po.created",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={
            "po_number": po_number,
            "client_id": client_id,
            "project_id": project_id,
            "lines": len(resolved),
        },
    )
    return po


# --------------------------------------------------------------------- read

def list_pos(
    db: Session,
    *,
    client_id: int | None = None,
    project_id: int | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PurchaseOrder]:
    """The filtered, newest-first PO register (lines eager-loaded for count + total)."""
    stmt: Select[tuple[PurchaseOrder]] = (
        select(PurchaseOrder)
        .options(selectinload(PurchaseOrder.lines))
        .order_by(PurchaseOrder.id.desc())
    )
    if client_id is not None:
        stmt = stmt.where(PurchaseOrder.client_id == client_id)
    if project_id is not None:
        stmt = stmt.where(PurchaseOrder.project_id == project_id)
    if status is not None:
        stmt = stmt.where(PurchaseOrder.status == status)
    if q and q.strip():
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(PurchaseOrder.po_number.ilike(like, escape="\\"))
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def get_po_detail(db: Session, po_id: int) -> PurchaseOrder:
    """One PO with its lines (+ product) and amendments, or raise ``PONotFound``."""
    po = db.execute(
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.lines).selectinload(POLineItem.product),
            selectinload(PurchaseOrder.amendments),
        )
        .where(PurchaseOrder.id == po_id)
    ).scalar_one_or_none()
    if po is None:
        raise PONotFound(f"purchase order {po_id} not found")
    return po


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed ``%``/``_`` matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------- totals

def _net_qty(line: POLineItem) -> Decimal:
    """A line's INVOICEABLE quantity = ordered_qty minus any short-closed (retired) qty,
    floored at 0. A short-close retires the un-invoiced remainder, so only the remaining
    (ordered - short_closed) quantity is billable and counts toward every revenue total."""
    net = Decimal(line.ordered_qty) - Decimal(line.short_closed_qty or 0)
    return net if net > 0 else Decimal(0)


def _is_voided(po: PurchaseOrder) -> bool:
    """A CANCELLED (voided) PO carries NO revenue — every money total collapses to 0."""
    return po.status == POStatus.CANCELLED.value


def line_sell_paise(line: POLineItem) -> int:
    """A line's ACTUAL sell value in paise = INVOICEABLE qty * per-unit actual sell (HALF-UP).
    ADMIN-ONLY figure (the margin basis); the route masks it for non-admins. Nets out any
    short-closed quantity — retired units are never billed."""
    total = _net_qty(line) * Decimal(line.sell_price_paise)
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def po_total_sell_paise(po: PurchaseOrder) -> int:
    """Sum of every line's ACTUAL sell value in paise (net of short-close; 0 for a voided PO).
    ADMIN-ONLY aggregate — masked for non-admins in the serializer."""
    if _is_voided(po):
        return 0
    return sum((line_sell_paise(line) for line in po.lines), 0)


def line_client_sell_paise(line: POLineItem) -> int:
    """A line's CLIENT-quoted sell value in paise = INVOICEABLE qty * per-unit client sell
    (HALF-UP). Visible to everyone. Nets out any short-closed quantity. A NULL client sell
    contributes 0 (giveaway line)."""
    total = _net_qty(line) * Decimal(line.client_sell_price_paise or 0)
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def po_total_client_sell_paise(po: PurchaseOrder) -> int:
    """Sum of every line's CLIENT-quoted sell value in paise (net of short-close; 0 for a
    voided PO). Visible to all."""
    if _is_voided(po):
        return 0
    return sum((line_client_sell_paise(line) for line in po.lines), 0)


def line_client_freight_paise(line: POLineItem) -> int:
    """A line's CLIENT-quoted freight in paise — a FLAT per-line charge (not per-unit), and
    REVENUE billed to the client. Counted only while the line still delivers: a FULLY
    short-closed line (net qty 0 — nothing ships) carries no freight; a partial short-close
    keeps the flat freight (the shipment still happens). Visible to everyone."""
    return line.client_freight_paise or 0 if _net_qty(line) > 0 else 0


def po_total_client_freight_paise(po: PurchaseOrder) -> int:
    """Sum of every live line's CLIENT-quoted freight in paise (0 for a voided PO). Client
    freight is revenue we bill the client. Visible to all."""
    if _is_voided(po):
        return 0
    return sum((line_client_freight_paise(line) for line in po.lines), 0)


def line_client_extras_paise(line: POLineItem) -> int:
    """A line's packaging + handling + other FLAT per-line charges in paise — all billed to
    the client (revenue). Like freight, counted only while the line still delivers: a FULLY
    short-closed line (nothing ships) carries none; a partial short-close keeps them flat."""
    if _net_qty(line) <= 0:
        return 0
    return line.packaging_paise + line.handling_paise + line.other_paise


def po_total_client_extras_paise(po: PurchaseOrder) -> int:
    """Sum of every live line's packaging/handling/other client charges (0 for a voided PO).
    These are revenue billed to the client. Visible to all."""
    if _is_voided(po):
        return 0
    return sum((line_client_extras_paise(line) for line in po.lines), 0)


def po_total_client_billing_paise(po: PurchaseOrder) -> int:
    """The ENTIRE billing to the client (excl. the agency fee itself) = goods client-sell +
    client freight + packaging/handling/other — all net of short-close, 0 for a voided PO.
    This is the base the PERCENT agency fee is charged on. Visible to all."""
    return (po_total_client_sell_paise(po) + po_total_client_freight_paise(po)
            + po_total_client_extras_paise(po))


def agency_fee_paise(po: PurchaseOrder) -> int:
    """The agency fee CHARGED TO THE CLIENT, in paise — ADDITIONAL revenue for us. PERCENT is
    applied to the ENTIRE client billing (goods client-sell + client freight + packaging/
    handling/other), net of short-close, HALF-UP; FIXED is the flat amount; NONE is 0. A
    voided PO charges no agency fee. Visible to everyone (not a sensitive/actual figure)."""
    if _is_voided(po):
        return 0
    if po.agency_fee_type == "PERCENT" and po.agency_fee_percent is not None:
        base = Decimal(po_total_client_billing_paise(po))
        fee = base * (Decimal(po.agency_fee_percent) / Decimal(100))
        return int(fee.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if po.agency_fee_type == "FIXED" and po.agency_fee_amount_paise is not None:
        return po.agency_fee_amount_paise
    return 0


def po_total_with_agency_paise(po: PurchaseOrder) -> int:
    """Full client-facing REVENUE = the entire client billing (goods + client freight +
    packaging/handling/other) + the agency fee (all 0 for a voided PO). The agency fee is a
    percentage of the billing base; it is not compounded on itself."""
    return po_total_client_billing_paise(po) + agency_fee_paise(po)


# --------------------------------------------------------------- labels (join)

@dataclass
class POLabels:
    """Cross-module display labels resolved by explicit SELECT (no ORM relationship
    from purchase_order to project/client/gstin/file — module decoupling)."""

    client_names: dict[int, str]
    project_codes: dict[int, str]
    gstins: dict[int, str]
    files: dict[int, str]


def label_maps(db: Session, pos: list[PurchaseOrder]) -> POLabels:
    """Resolve client name / project code / GSTIN / stored-file filename for a page of
    POs in one query each (no per-row N+1). Missing ids simply drop out."""
    client_ids = {p.client_id for p in pos}
    project_ids = {p.project_id for p in pos}
    gstin_ids = {p.client_gstin_id for p in pos if p.client_gstin_id is not None}
    file_ids = {p.soft_copy_file_id for p in pos if p.soft_copy_file_id is not None}

    client_names: dict[int, str] = {}
    if client_ids:
        for row in db.execute(
            select(ProjectClient.id, ProjectClient.name).where(
                ProjectClient.id.in_(client_ids))
        ):
            client_names[row.id] = row.name
    project_codes: dict[int, str] = {}
    if project_ids:
        for row in db.execute(
            select(Project.id, Project.code).where(Project.id.in_(project_ids))
        ):
            project_codes[row.id] = row.code
    gstins: dict[int, str] = {}
    if gstin_ids:
        for row in db.execute(
            select(ClientGstin.id, ClientGstin.gstin).where(ClientGstin.id.in_(gstin_ids))
        ):
            gstins[row.id] = row.gstin
    files: dict[int, str] = {}
    if file_ids:
        for row in db.execute(
            select(StoredFile.id, StoredFile.filename).where(StoredFile.id.in_(file_ids))
        ):
            files[row.id] = row.filename
    return POLabels(
        client_names=client_names, project_codes=project_codes,
        gstins=gstins, files=files,
    )


# -------------------------------------------------------------------- amend

def _snapshot(po: PurchaseOrder) -> dict[str, Any]:
    """A JSON-safe capture of the PO + its lines BEFORE a mutation (dates -> ISO,
    Decimal -> str) so an amendment is a faithful, replayable record of prior state."""
    return {
        "po_number": po.po_number,
        "client_id": po.client_id,
        "client_gstin_id": po.client_gstin_id,
        "project_id": po.project_id,
        "soft_copy_file_id": po.soft_copy_file_id,
        "po_date": po.po_date.isoformat() if po.po_date else None,
        "expected_procurement_date": (
            po.expected_procurement_date.isoformat()
            if po.expected_procurement_date else None
        ),
        "status": po.status,
        "notes": po.notes,
        "agency_fee_type": po.agency_fee_type,
        "agency_fee_percent": (
            str(po.agency_fee_percent) if po.agency_fee_percent is not None else None
        ),
        "agency_fee_amount_paise": po.agency_fee_amount_paise,
        "lines": [
            {
                "product_id": line.product_id,
                "description": line.description,
                "uom": line.uom,
                "ordered_qty": str(line.ordered_qty),
                "cost_price_paise": line.cost_price_paise,
                "original_cost_price_paise": line.original_cost_price_paise,
                "client_sell_price_paise": line.client_sell_price_paise,
                "vendor_sell_price_paise": line.vendor_sell_price_paise,
                "sell_price_paise": line.sell_price_paise,
                "client_freight_paise": line.client_freight_paise,
                "vendor_freight_paise": line.vendor_freight_paise,
                "freight_paise": line.freight_paise,
                "packaging_paise": line.packaging_paise,
                "handling_paise": line.handling_paise,
                "other_paise": line.other_paise,
                "tax_rate": str(line.tax_rate),
                "line_status": line.line_status,
                "short_closed_qty": str(line.short_closed_qty),
                "short_close_reason": line.short_close_reason,
            }
            for line in po.lines
        ],
    }


_HEADER_KEYS = frozenset({
    "po_number", "client_gstin_id", "project_id", "po_date",
    "expected_procurement_date", "notes", "soft_copy_file_id",
    "agency_fee_type", "agency_fee_percent", "agency_fee_amount_paise",
})


def amend_po(
    db: Session,
    po: PurchaseOrder,
    *,
    header: dict[str, Any],
    lines: list[LineInput] | None = None,
    summary: str | None = None,
    can_set_actuals: bool = False,
    actor_uid: str | None = None,
) -> PurchaseOrder:
    """Snapshot the PO into a new ``POAmendment`` version, then apply the header edits
    (only keys present in ``header``) and, when ``lines`` is given, replace ALL lines.

    Blocked (422) on a CANCELLED or CLOSED PO — history there is terminal. A
    ``(client_id, po_number)`` collision after a po_number edit is ``DuplicatePO``.
    Audited ``po.amended``. Caller commits."""
    if po.status in (POStatus.CANCELLED.value, POStatus.CLOSED.value):
        raise POValidationError(
            f"a {po.status} purchase order cannot be amended")
    unknown = set(header) - _HEADER_KEYS
    if unknown:
        raise POValidationError(f"unknown header field(s): {sorted(unknown)}")

    next_version = max((a.version for a in po.amendments), default=0) + 1
    if lines is not None and not lines:
        raise POValidationError("a line replacement must contain at least one line")

    # ALL mutations happen INSIDE the SAVEPOINT: ``begin_nested()`` pre-flushes any dirty
    # state at entry, so a po_number/version change made BEFORE the savepoint would flush
    # (and a unique collision would break) OUTSIDE it. Mutating inside keeps a collision
    # contained to the savepoint (mirrors ``create_po``). The snapshot is still taken BEFORE
    # ``_apply_header`` so it records prior state.
    try:
        with db.begin_nested():
            po.amendments.append(POAmendment(
                version=next_version,
                summary=(summary or f"amendment v{next_version}").strip()[:_MAX_REASON_LEN],
                snapshot=_snapshot(po),
                created_by=actor_uid,
            ))
            _apply_header(db, po, header)
            if lines is not None:
                # Capture the pre-replacement lines so an edit carries each line's admin-set
                # actuals forward (matched by PRODUCT, not list position, so a reorder/insert
                # no longer wipes the margin) instead of resetting them to the client figure
                # (see `_resolve_line`). Greedy first-unconsumed match pairs duplicate-product
                # lines in order.
                carry_pool: dict[int, list[POLineItem]] = {}
                for old in po.lines:
                    carry_pool.setdefault(old.product_id, []).append(old)
                resolved = []
                for li in lines:
                    pool = carry_pool.get(li.product_id)
                    carry = pool.pop(0) if pool else None
                    resolved.append(_resolve_line(
                        db, li, can_set_actuals=can_set_actuals, carry_actual_from=carry))
                po.lines.clear()  # cascade delete-orphan removes the old rows
                po.lines.extend(resolved)
            db.flush()
    except IntegrityError as exc:
        # Two uniques can trip here: the PO number (only if po_number was edited) or
        # the amendment version (a concurrent amend of the SAME PO). Only call it a
        # duplicate PO when the number actually changed; otherwise it's amendment
        # contention — surface it as retryable, not a misleading "duplicate PO".
        if "po_number" in header:
            raise DuplicatePO(
                f"a PO '{po.po_number}' already exists for this client") from exc
        raise POValidationError(
            "this purchase order was amended concurrently — please retry") from exc

    audit.log(
        db,
        action="po.amended",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={
            "version": next_version,
            "header": sorted(header),
            "lines_replaced": lines is not None,
        },
    )
    return po


def _apply_header(db: Session, po: PurchaseOrder, header: dict[str, Any]) -> None:
    """Apply the present header keys, validating each cross-module reference."""
    if "po_number" in header:
        # None / blank ⇒ store NULL; the partial unique index still 409s a dup non-null.
        po.po_number = _clean_po_number(header["po_number"])
    if _AGENCY_KEYS & set(header):
        # The agency fee is one interdependent trio — a change must name its type so the
        # matching field / null-out rule is validated as a whole.
        if "agency_fee_type" not in header:
            raise POValidationError("agency_fee_type is required to change the agency fee")
        fee_type = str(header["agency_fee_type"])
        percent = header.get("agency_fee_percent")
        amount = header.get("agency_fee_amount_paise")
        _validate_agency_fee(fee_type, percent, amount)
        po.agency_fee_type = fee_type
        po.agency_fee_percent = percent
        po.agency_fee_amount_paise = amount
    if "project_id" in header and header["project_id"] != po.project_id:
        project_id = int(header["project_id"])
        _ensure_project_active(db, project_id, po.client_id)
        po.project_id = project_id
    if "client_gstin_id" in header:
        gstin_id = header["client_gstin_id"]
        if gstin_id is not None:
            _ensure_gstin(db, po.client_id, int(gstin_id))
            po.client_gstin_id = int(gstin_id)
        else:
            po.client_gstin_id = None
    if "soft_copy_file_id" in header:
        file_id = header["soft_copy_file_id"]
        if file_id is not None:
            _ensure_file(db, int(file_id))
            po.soft_copy_file_id = int(file_id)
        else:
            po.soft_copy_file_id = None
    if "po_date" in header and header["po_date"] is not None:
        po.po_date = header["po_date"]
    if "expected_procurement_date" in header:
        po.expected_procurement_date = header["expected_procurement_date"]
    if "notes" in header:
        notes = header["notes"]
        po.notes = notes.strip()[:_MAX_NOTES_LEN] if notes else None


# --------------------------------------------------------------- short-close

def short_close(
    db: Session,
    po: PurchaseOrder,
    *,
    reason: str,
    line_id: int | None = None,
    actor_uid: str | None = None,
) -> PurchaseOrder:
    """Short-close open-to-invoice quantity (retire, never delete).

    With ``line_id``: that one OPEN line becomes SHORT_CLOSED, retiring its
    REMAINING-OPEN qty (``ordered_qty − already-invoiced``). Without it: EVERY OPEN line
    is short-closed and the whole PO moves to CLOSED. Blocked on a CANCELLED/CLOSED PO.
    Audited."""
    reason = reason.strip()
    if not reason:
        raise POValidationError("reason is required")
    reason = reason[:_MAX_REASON_LEN]
    if po.status in (POStatus.CANCELLED.value, POStatus.CLOSED.value):
        raise POValidationError(
            f"a {po.status} purchase order cannot be short-closed")

    if line_id is not None:
        line = next((line for line in po.lines if line.id == line_id), None)
        if line is None:
            raise PONotFound(f"line {line_id} is not on this purchase order")
        if line.line_status != LineStatus.OPEN.value:
            raise POValidationError(f"line {line_id} is not OPEN")
        _short_close_line(db, line, reason)
    else:
        open_lines = [ln for ln in po.lines if ln.line_status == LineStatus.OPEN.value]
        if not open_lines:
            raise POValidationError("this purchase order has no OPEN lines to short-close")
        for line in open_lines:
            _short_close_line(db, line, reason)
        po.status = POStatus.CLOSED.value
        po.close_reason = reason
        po.closed_by = actor_uid
        po.closed_at = datetime.now(UTC)

    db.flush()
    audit.log(
        db,
        action="po.short_closed",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={"line_id": line_id, "whole_po": line_id is None, "reason": reason},
    )
    return po


def _short_close_line(db: Session, line: POLineItem, reason: str) -> None:
    # Retire only the REMAINING-OPEN qty (ordered − already-invoiced), clamped ≥ 0 — NOT the
    # full ordered qty. A line invoiced 4 of 10 retires 6. Lazy import: billing.invoice_service
    # imports po_service (the confirm hook), so a load-time import here would cycle.
    from app.modules.billing.invoice_service import invoiced_qty_for_po_line
    remaining = line.ordered_qty - invoiced_qty_for_po_line(db, line.id)
    line.line_status = LineStatus.SHORT_CLOSED.value
    line.short_closed_qty = remaining if remaining > Decimal("0") else Decimal("0")
    line.short_close_reason = reason


# --------------------------------------------------------------------- void

def void_po(
    db: Session, po: PurchaseOrder, *, reason: str, actor_uid: str | None = None,
) -> PurchaseOrder:
    """Void a PO: flip its status to CANCELLED (soft — nothing is deleted). Blocked if
    the PO is already CANCELLED. Audited ``po.voided``. Caller commits."""
    reason = reason.strip()
    if not reason:
        raise POValidationError("reason is required")
    if po.status == POStatus.CANCELLED.value:
        raise POValidationError("this purchase order is already cancelled")
    po.status = POStatus.CANCELLED.value
    po.close_reason = reason[:_MAX_REASON_LEN]
    po.closed_by = actor_uid
    po.closed_at = datetime.now(UTC)
    db.flush()
    audit.log(
        db,
        action="po.voided",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={"reason": po.close_reason},
    )
    return po


# --------------------------------------------------------------- confirm
def confirm_po(
    db: Session, po: PurchaseOrder, *, actor_uid: str | None = None,
) -> PurchaseOrder:
    """Confirm a DRAFT PO: transition DRAFT -> CONFIRMED (the client PO is now live).

    Blocked (route -> 422/400) unless the PO is DRAFT — a CONFIRMED / IN_PROGRESS /
    CLOSED / CANCELLED PO cannot be (re-)confirmed. Audited ``po.confirmed``. Caller
    commits (the row is already loaded by the route)."""
    if po.status != POStatus.DRAFT.value:
        raise POValidationError(
            f"only a DRAFT purchase order can be confirmed (this one is {po.status})")
    po.status = POStatus.CONFIRMED.value
    db.flush()
    audit.log(
        db,
        action="po.confirmed",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={"po_number": po.po_number},
    )
    return po


def mark_in_progress(
    db: Session, po_id: int | None, *, actor_uid: str | None = None,
) -> PurchaseOrder | None:
    """Best-effort nudge CONFIRMED -> IN_PROGRESS (a client invoice was confirmed against
    this PO). ONLY a CONFIRMED PO transitions; a DRAFT / IN_PROGRESS / CLOSED / CANCELLED
    PO (or a missing / None ``po_id``) is a no-op — never move DRAFT straight to IN_PROGRESS
    and never disturb a terminal state. Audited ``po.in_progress`` only on a real transition.
    Caller commits."""
    if po_id is None:
        return None
    po = db.get(PurchaseOrder, po_id)
    if po is None or po.status != POStatus.CONFIRMED.value:
        return po
    po.status = POStatus.IN_PROGRESS.value
    db.flush()
    audit.log(
        db,
        action="po.in_progress",
        actor_uid=actor_uid,
        entity="purchase_order",
        entity_id=str(po.id),
        detail={"po_number": po.po_number},
    )
    return po


# ------------------------------------------------------------- bulk (Excel)

# Canonical bulk columns -> the friendly headers accepted for each (case-insensitive,
# whitespace/hyphen collapsed to '_'). po_number + ordered_qty + cost_price +
# sell_price are required; a product_code OR product_name column must be present.
_BULK_ALIASES: dict[str, str] = {
    "po_number": "po_number", "po_no": "po_number", "po": "po_number",
    "product_code": "product_code", "code": "product_code", "sku": "product_code",
    "product_name": "product_name", "product": "product_name", "name": "product_name",
    "description": "description", "desc": "description",
    "uom": "uom", "unit": "uom",
    "ordered_qty": "ordered_qty", "qty": "ordered_qty", "quantity": "ordered_qty",
    "cost_price": "cost_price", "cost": "cost_price",
    "sell_price": "sell_price", "sell": "sell_price", "price": "sell_price",
    "freight": "freight", "packaging": "packaging", "handling": "handling",
    "other": "other",
    "tax_rate": "tax_rate", "tax": "tax_rate", "gst": "tax_rate", "gst_rate": "tax_rate",
    "po_date": "po_date", "date": "po_date", "order_date": "po_date",
}
_BULK_REQUIRED = ("po_number", "ordered_qty", "cost_price", "sell_price")

# The downloadable TEMPLATE: the canonical header in a fixed, friendly order (a subset of the
# accepted aliases — ``product_code`` is the product key the template ships) plus one
# illustrative example row. Money columns are RUPEES (``_build_bulk_line`` converts them to
# paise via ``parse_paise``), so the example uses rupee figures. Feeding this file straight
# back through the bulk-upload endpoint creates a PO, so the template is guaranteed-valid
# input, not just a column list. Required: po_number, product_code, ordered_qty, cost_price,
# sell_price; the rest are optional.
_BULK_TEMPLATE_COLUMNS: tuple[str, ...] = (
    "po_number", "product_code", "description", "uom", "ordered_qty",
    "cost_price", "sell_price", "freight", "packaging", "handling", "other", "tax_rate",
)
# One example row aligned to _BULK_TEMPLATE_COLUMNS. cost_price/sell_price/freight are in
# RUPEES (250.00 -> 25000 paise). product_code is illustrative — replace with a real SKU.
_BULK_TEMPLATE_EXAMPLE: tuple[object, ...] = (
    "PO-2026-001", "SKU-1001", "Sample line — replace with your product", "PCS",
    100, 250.00, 399.00, 500.00, 0, 0, 0, 18,
)


def build_bulk_template_xlsx() -> bytes:
    """Return the bulk-upload template as ``.xlsx`` bytes: the header row in the exact column
    order the parser accepts + one illustrative example row (money columns in rupees)."""
    wb = Workbook()
    ws = wb.active or wb.create_sheet()
    ws.title = "purchase_orders"
    ws.append(list(_BULK_TEMPLATE_COLUMNS))
    ws.append(list(_BULK_TEMPLATE_EXAMPLE))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _norm_header(raw: str) -> str:
    return "_".join(raw.strip().lower().replace("-", " ").split())


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def _parse_bulk_workbook(
    data: bytes,
) -> tuple[list[tuple[int, dict[str, str]]], list[tuple[int, str]]]:
    """Read the first worksheet against the bulk template. Returns (rows, errors);
    a bad file or a missing required header yields ([], [errors])."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - openpyxl raises many types on a bad file
        return [], [(1, "could not read the file as an .xlsx workbook")]
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
        col_index: dict[str, int] = {}
        for idx, raw in enumerate(header or ()):
            if raw is None:
                continue
            key = _BULK_ALIASES.get(_norm_header(str(raw)))
            if key is not None and key not in col_index:
                col_index[key] = idx

        errors: list[tuple[int, str]] = []
        for col in _BULK_REQUIRED:
            if col not in col_index:
                errors.append((1, f"required column '{col}' is missing"))
        if "product_code" not in col_index and "product_name" not in col_index:
            errors.append((1, "a 'product_code' or 'product_name' column is required"))
        if errors:
            return [], errors

        rows: list[tuple[int, dict[str, str]]] = []
        for sheet_row, values in enumerate(rows_iter, start=2):
            cells = {
                key: _cell_str(values[idx] if idx < len(values) else None)
                for key, idx in col_index.items()
            }
            if all(v == "" for v in cells.values()):
                continue  # skip a fully-blank row
            rows.append((sheet_row, cells))
        return rows, []
    finally:
        wb.close()


def _resolve_bulk_product(db: Session, code: str, name: str) -> Product | str:
    """Resolve a product by code (preferred) or exact name. Returns the ACTIVE Product,
    or an error string (unknown / inactive / ambiguous) — a row error, never a create."""
    if code:
        product = db.execute(
            select(Product).where(func.lower(Product.code) == code.lower())
        ).scalar_one_or_none()
        if product is None:
            return f"no product with code '{code}'"
    elif name:
        matches = list(db.execute(
            select(Product).where(func.lower(Product.name) == name.lower())
        ).scalars())
        if not matches:
            return f"no product named '{name}'"
        if len(matches) > 1:
            return f"product name '{name}' is ambiguous (matches {len(matches)}); use a code"
        product = matches[0]
    else:
        return "a product_code or product_name is required"
    if not product.active:
        return f"product '{product.name}' is not active"
    return product


def _build_bulk_line(db: Session, cells: dict[str, str]) -> LineInput | str:
    """Turn one validated Excel row into a ``LineInput``, or an error string."""
    product = _resolve_bulk_product(
        db, cells.get("product_code", "").strip(), cells.get("product_name", "").strip())
    if isinstance(product, str):
        return product

    qty = parse_qty(cells.get("ordered_qty", ""))
    if qty is None or not _qty_ok(qty):
        return "ordered_qty must be a positive number (max 15 digits, 3 decimals)"

    money: dict[str, int] = {}
    for col, required in (
        ("cost_price", True), ("sell_price", True),
        ("freight", False), ("packaging", False), ("handling", False), ("other", False),
    ):
        raw = cells.get(col, "").strip()
        if not raw:
            if required:
                return f"{col} is required"
            money[col] = 0
            continue
        paise = parse_paise(raw)
        if paise is None or paise < 0:
            return f"{col} must be a number >= 0"
        if paise > _MAX_MONEY_PAISE:
            return f"{col} is too large"
        money[col] = paise

    tax_raw = cells.get("tax_rate", "").strip()
    tax = Decimal("0")
    if tax_raw:
        parsed = parse_qty(tax_raw)
        if parsed is None or parsed < 0 or parsed > 100:
            return "tax_rate must be a number between 0 and 100"
        if parsed != parsed.quantize(Decimal("0.01")):
            return "tax_rate supports at most 2 decimal places"
        tax = parsed

    description = cells.get("description", "").strip() or None
    uom = cells.get("uom", "").strip() or None
    # Bulk carries only the visible client-facing figures: the sell/freight columns map to
    # client_sell / client_freight. The ADMIN-ONLY actuals are left to default to them
    # (bulk is a non-admin path — no per-row actual override).
    return LineInput(
        product_id=product.id,
        ordered_qty=qty,
        cost_price_paise=money["cost_price"],
        client_sell_price_paise=money["sell_price"],
        description=description,
        uom=uom,
        client_freight_paise=money["freight"],
        packaging_paise=money["packaging"],
        handling_paise=money["handling"],
        other_paise=money["other"],
        tax_rate=tax,
    )


def _parse_bulk_date(raw: str) -> date | None | str:
    """Parse an optional po_date cell. Returns a ``date`` when parseable, ``None`` when
    blank (caller defaults to today), or an error string when present-but-unparseable.

    Accepts ISO ``YYYY-MM-DD`` (openpyxl stringifies date cells to ``YYYY-MM-DD 00:00:00``,
    handled by the leading-10 slice) and the common Indian ``DD/MM/YYYY`` / ``DD-MM-YYYY``."""
    raw = raw.strip()
    if not raw:
        return None
    iso = raw[:10]
    for fmt, text in ((("%Y-%m-%d"), iso), ("%d/%m/%Y", raw), ("%d-%m-%Y", raw)):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return "po_date must be a date (YYYY-MM-DD or DD/MM/YYYY)"


def _po_exists(db: Session, client_id: int, po_number: str | None) -> bool:
    # A NULL / blank number is never a duplicate (the partial unique index excludes NULLs).
    if not po_number:
        return False
    return db.execute(
        select(PurchaseOrder.id).where(
            PurchaseOrder.client_id == client_id,
            PurchaseOrder.po_number == po_number,
        )
    ).scalar_one_or_none() is not None


def bulk_create_from_excel(
    db: Session,
    data: bytes,
    *,
    client_id: int,
    project_id: int,
    actor_uid: str | None = None,
) -> BulkResult:
    """Parse an ``.xlsx`` and create one PO per ``po_number`` group under the given
    client + project. An optional ``po_date`` column sets each PO's date (so a batch of
    back-dated client POs keeps its real dates for the price-history timeline); a blank
    date falls back to today, a bad date poisons that group.

    Discipline: NO row is silently dropped. A malformed / unresolved row lands in
    ``errors`` and POISONS its whole po_number group (a partial PO is never created);
    a po_number already present (in the DB or created earlier this run) is a skip.
    Returns created / skipped / errors. Caller commits."""
    _ensure_client_active(db, client_id)
    _ensure_project_active(db, project_id, client_id)

    rows, parse_errors = _parse_bulk_workbook(data)
    result = BulkResult(errors=list(parse_errors))
    if parse_errors:  # header/file problem — nothing to create
        return result

    groups: dict[str, list[LineInput]] = {}
    group_dates: dict[str, date] = {}  # first valid po_date per group; else today
    order: list[str] = []
    poisoned: set[str] = set()
    for sheet_row, cells in rows:
        po_number = " ".join(cells.get("po_number", "").split())
        line = _build_bulk_line(db, cells)
        if not po_number:
            result.errors.append((sheet_row, "po_number is required"))
            continue
        if po_number not in groups:
            groups[po_number] = []
            order.append(po_number)
        # Optional po_date column: a bad value poisons the group (never silently
        # ignored); a valid one sets the group's date; blank falls back to today.
        parsed_date = _parse_bulk_date(cells.get("po_date", ""))
        if isinstance(parsed_date, str):
            result.errors.append((sheet_row, parsed_date))
            poisoned.add(po_number)
        elif parsed_date is not None and po_number not in group_dates:
            group_dates[po_number] = parsed_date
        if isinstance(line, str):
            result.errors.append((sheet_row, line))
            poisoned.add(po_number)
        else:
            groups[po_number].append(line)

    for po_number in order:
        if po_number in poisoned:
            result.skipped.append((po_number, "one or more of its rows had errors"))
            continue
        lines = groups[po_number]
        if not lines:
            result.skipped.append((po_number, "no valid line items"))
            continue
        if _po_exists(db, client_id, po_number):
            result.skipped.append(
                (po_number, "a PO with this number already exists for this client"))
            continue
        try:
            create_po(
                db,
                po_number=po_number,
                client_id=client_id,
                project_id=project_id,
                po_date=group_dates.get(po_number, date.today()),
                lines=lines,
                actor_uid=actor_uid,
            )
        except DuplicatePO:
            result.skipped.append(
                (po_number, "a PO with this number already exists for this client"))
            continue
        result.created.append(po_number)

    return result
