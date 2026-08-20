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

from openpyxl import load_workbook
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
    "cost_price_paise", "sell_price_paise",
    "freight_paise", "packaging_paise", "handling_paise", "other_paise",
)


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
    name/uom when omitted. Money is per-UNIT (cost/sell) or per-LINE (the rest)."""

    product_id: int
    ordered_qty: Decimal
    cost_price_paise: int
    sell_price_paise: int
    description: str | None = None
    uom: str | None = None
    freight_paise: int = 0
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


def _ensure_project_active(db: Session, project_id: int) -> Project:
    project = db.execute(
        select(Project).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise PONotFound(f"project {project_id} not found")
    if project.status != ProjectStatus.ACTIVE.value:
        raise POValidationError(f"project {project_id} is not Active")
    return project


def _ensure_gstin(db: Session, client_id: int, gstin_id: int) -> None:
    """A referenced client GSTIN must exist AND belong to this client."""
    row = db.get(ClientGstin, gstin_id)
    if row is None:
        raise PONotFound(f"client gstin {gstin_id} not found")
    if row.client_id != client_id:
        raise POValidationError("client_gstin_id does not belong to this client")


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
    service caller (the route's pydantic guards most of this at the edge)."""
    if not _qty_ok(li.ordered_qty):
        raise POValidationError(
            "ordered_qty must be a positive number that fits 15 digits and 3 decimals")
    for attr in _MONEY_FIELDS:
        value = getattr(li, attr)
        if value < 0:
            raise POValidationError(f"{attr} must not be negative")
        if value > _MAX_MONEY_PAISE:
            raise POValidationError(f"{attr} is too large")
    if li.tax_rate < 0 or li.tax_rate > 100:
        raise POValidationError("tax_rate must be between 0 and 100")


def _resolve_line(db: Session, li: LineInput) -> POLineItem:
    """Validate a line's numbers + product (must exist and be active), snapshotting
    the description (product name when omitted) and uom (product uom when omitted)."""
    _validate_line_numbers(li)
    product = db.get(Product, li.product_id)
    if product is None:
        raise PONotFound(f"product {li.product_id} not found")
    if not product.active:
        raise POValidationError(f"product {li.product_id} is not active")
    description = (li.description or product.name).strip()[:_MAX_DESCRIPTION_LEN]
    uom = (li.uom or product.uom).strip()[:_MAX_UOM_LEN] or product.uom
    return POLineItem(
        product_id=product.id,
        description=description,
        uom=uom,
        ordered_qty=li.ordered_qty,
        cost_price_paise=li.cost_price_paise,
        sell_price_paise=li.sell_price_paise,
        freight_paise=li.freight_paise,
        packaging_paise=li.packaging_paise,
        handling_paise=li.handling_paise,
        other_paise=li.other_paise,
        tax_rate=li.tax_rate,
        line_status=LineStatus.OPEN.value,
        short_closed_qty=Decimal("0"),
    )


def _clean_po_number(po_number: str) -> str:
    cleaned = " ".join(po_number.split())
    if not cleaned:
        raise POValidationError("po_number is required")
    if len(cleaned) > _MAX_PO_NUMBER_LEN:
        raise POValidationError(f"po_number must be at most {_MAX_PO_NUMBER_LEN} characters")
    return cleaned


# ------------------------------------------------------------------- create

def create_po(
    db: Session,
    *,
    po_number: str,
    client_id: int,
    project_id: int,
    po_date: date,
    lines: list[LineInput],
    client_gstin_id: int | None = None,
    expected_procurement_date: date | None = None,
    notes: str | None = None,
    soft_copy_file_id: int | None = None,
    actor_uid: str | None = None,
) -> PurchaseOrder:
    """Create a PO with its lines. Validates client + project + every product are
    ACTIVE and the optional GSTIN/file references resolve, then inserts under a
    SAVEPOINT so a ``(client_id, po_number)`` collision surfaces as ``DuplicatePO``.
    Audited ``po.created``. Caller commits."""
    po_number = _clean_po_number(po_number)
    if not lines:
        raise POValidationError("at least one line item is required")
    _ensure_client_active(db, client_id)
    _ensure_project_active(db, project_id)
    if client_gstin_id is not None:
        _ensure_gstin(db, client_id, client_gstin_id)
    if soft_copy_file_id is not None:
        _ensure_file(db, soft_copy_file_id)

    resolved = [_resolve_line(db, li) for li in lines]

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

def line_sell_paise(line: POLineItem) -> int:
    """A line's sell value in paise = ordered_qty * per-unit sell price (HALF-UP)."""
    total = Decimal(line.ordered_qty) * Decimal(line.sell_price_paise)
    return int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def po_total_sell_paise(po: PurchaseOrder) -> int:
    """Sum of every line's sell value in paise (all lines, regardless of status)."""
    return sum((line_sell_paise(line) for line in po.lines), 0)


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
        "lines": [
            {
                "product_id": line.product_id,
                "description": line.description,
                "uom": line.uom,
                "ordered_qty": str(line.ordered_qty),
                "cost_price_paise": line.cost_price_paise,
                "sell_price_paise": line.sell_price_paise,
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
})


def amend_po(
    db: Session,
    po: PurchaseOrder,
    *,
    header: dict[str, Any],
    lines: list[LineInput] | None = None,
    summary: str | None = None,
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

    # Snapshot BEFORE mutating so the amendment records prior state.
    next_version = max((a.version for a in po.amendments), default=0) + 1
    po.amendments.append(POAmendment(
        version=next_version,
        summary=(summary or f"amendment v{next_version}").strip()[:_MAX_REASON_LEN],
        snapshot=_snapshot(po),
        created_by=actor_uid,
    ))

    _apply_header(db, po, header)
    if lines is not None:
        if not lines:
            raise POValidationError("a line replacement must contain at least one line")
        resolved = [_resolve_line(db, li) for li in lines]
        po.lines.clear()  # cascade delete-orphan removes the old rows
        po.lines.extend(resolved)

    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise DuplicatePO(
            f"a PO '{po.po_number}' already exists for this client") from exc

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
        po.po_number = _clean_po_number(str(header["po_number"]))
    if "project_id" in header and header["project_id"] != po.project_id:
        project_id = int(header["project_id"])
        _ensure_project_active(db, project_id)
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

    With ``line_id``: that one OPEN line becomes SHORT_CLOSED with
    ``short_closed_qty = ordered_qty``. Without it: EVERY OPEN line is short-closed
    and the whole PO moves to CLOSED. Blocked on a CANCELLED/CLOSED PO. Audited."""
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
        _short_close_line(line, reason)
    else:
        open_lines = [ln for ln in po.lines if ln.line_status == LineStatus.OPEN.value]
        if not open_lines:
            raise POValidationError("this purchase order has no OPEN lines to short-close")
        for line in open_lines:
            _short_close_line(line, reason)
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


def _short_close_line(line: POLineItem, reason: str) -> None:
    line.line_status = LineStatus.SHORT_CLOSED.value
    line.short_closed_qty = line.ordered_qty
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
}
_BULK_REQUIRED = ("po_number", "ordered_qty", "cost_price", "sell_price")


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
        tax = parsed

    description = cells.get("description", "").strip() or None
    uom = cells.get("uom", "").strip() or None
    return LineInput(
        product_id=product.id,
        ordered_qty=qty,
        cost_price_paise=money["cost_price"],
        sell_price_paise=money["sell_price"],
        description=description,
        uom=uom,
        freight_paise=money["freight"],
        packaging_paise=money["packaging"],
        handling_paise=money["handling"],
        other_paise=money["other"],
        tax_rate=tax,
    )


def _po_exists(db: Session, client_id: int, po_number: str) -> bool:
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
    client + project (po_date defaults to today — the template carries no date column).

    Discipline: NO row is silently dropped. A malformed / unresolved row lands in
    ``errors`` and POISONS its whole po_number group (a partial PO is never created);
    a po_number already present (in the DB or created earlier this run) is a skip.
    Returns created / skipped / errors. Caller commits."""
    _ensure_client_active(db, client_id)
    _ensure_project_active(db, project_id)

    rows, parse_errors = _parse_bulk_workbook(data)
    result = BulkResult(errors=list(parse_errors))
    if parse_errors:  # header/file problem — nothing to create
        return result

    groups: dict[str, list[LineInput]] = {}
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
                po_date=date.today(),
                lines=lines,
                actor_uid=actor_uid,
            )
        except DuplicatePO:
            result.skipped.append(
                (po_number, "a PO with this number already exists for this client"))
            continue
        result.created.append(po_number)

    return result
