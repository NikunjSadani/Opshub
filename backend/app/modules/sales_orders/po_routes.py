"""Purchase Orders API (mounted under the sales_orders aggregator at `/api/v1`).

  POST   /purchase-orders                 -> create a PO with lines (po.create)
  POST   /purchase-orders/upload          -> bulk-create POs from an .xlsx (po.create)
  GET    /purchase-orders                 -> filtered register (VIEW)
  GET    /purchase-orders/{id}            -> one PO + lines + amendment count (VIEW)
  PATCH  /purchase-orders/{id}            -> amend (snapshots a version) (po.amend)
  POST   /purchase-orders/{id}/confirm    -> DRAFT -> CONFIRMED (po.create)
  POST   /purchase-orders/{id}/short-close-> retire open-to-invoice qty (po.short_close)
  POST   /purchase-orders/{id}/void       -> soft-cancel the PO (po.void)

Reads need the ``sales_orders`` module (>= View). ``po.create``/``po.amend`` are
OPERATE; ``po.short_close``/``po.void`` are MANAGE. Every mutation is audited by the
service; money is integer paise, quantities/tax are serialized as strings.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.sales_orders import po_service
from app.modules.sales_orders.models import PurchaseOrder
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, PlatformPerm, User
from app.platform.rbac import can, has_platform

router = APIRouter()

MODULE_KEY = "sales_orders"
_UPLOAD_CHUNK = 1024 * 1024
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


def _can_see_actuals(user: User) -> bool:
    """Only an IAM (admin) user may VIEW or SET the ACTUAL sell/freight figures. The read
    serializers mask them to None otherwise; the create/amend path defaults them to the
    client figure for a non-admin so they can never diverge."""
    return has_platform(user, PlatformPerm.IAM)


def _map_error(err: po_service.POError, *, validation_status: int = 400) -> HTTPException:
    """Map a typed service error to HTTP. ``validation_status`` lets a caller raise a
    POValidationError as 422 (a blocked amend) instead of the default 400."""
    if isinstance(err, po_service.PONotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(err))
    if isinstance(err, po_service.DuplicatePO):
        return HTTPException(status.HTTP_409_CONFLICT, str(err))
    if isinstance(err, po_service.POValidationError):
        return HTTPException(validation_status, str(err))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# ------------------------------------------------------------------- schemas

class POLineIn(BaseModel):
    product_id: int
    description: str | None = Field(default=None, max_length=500)
    uom: str | None = Field(default=None, max_length=20)
    ordered_qty: Decimal = Field(gt=0)
    # cost tier
    cost_price_paise: int = Field(ge=0)                               # Our CP, visible, required
    original_cost_price_paise: int | None = Field(default=None, ge=0)  # Original CP, optional
    # sell tiers
    client_sell_price_paise: int = Field(ge=0)                       # client-quoted, REQUIRED
    vendor_sell_price_paise: int | None = Field(default=None, ge=0)   # vendor sell, optional
    sell_price_paise: int | None = Field(default=None, ge=0)          # ACTUAL — admin-only
    # freight tiers
    client_freight_paise: int | None = Field(default=0, ge=0)        # client freight, visible
    vendor_freight_paise: int | None = Field(default=None, ge=0)     # vendor freight, optional
    freight_paise: int | None = Field(default=None, ge=0)            # ACTUAL — admin-only
    packaging_paise: int = Field(default=0, ge=0)
    handling_paise: int = Field(default=0, ge=0)
    other_paise: int = Field(default=0, ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)

    def to_input(self) -> po_service.LineInput:
        return po_service.LineInput(
            product_id=self.product_id,
            ordered_qty=self.ordered_qty,
            cost_price_paise=self.cost_price_paise,
            client_sell_price_paise=self.client_sell_price_paise,
            description=self.description,
            uom=self.uom,
            original_cost_price_paise=self.original_cost_price_paise,
            vendor_sell_price_paise=self.vendor_sell_price_paise,
            sell_price_paise=self.sell_price_paise,
            client_freight_paise=self.client_freight_paise,
            vendor_freight_paise=self.vendor_freight_paise,
            freight_paise=self.freight_paise,
            packaging_paise=self.packaging_paise,
            handling_paise=self.handling_paise,
            other_paise=self.other_paise,
            tax_rate=self.tax_rate,
        )


class POCreateIn(BaseModel):
    po_number: str | None = Field(default=None, max_length=64)  # OPTIONAL client reference
    client_id: int
    client_gstin_id: int | None = None
    project_id: int
    po_date: date
    expected_procurement_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    soft_copy_file_id: int | None = None
    agency_fee_type: Literal["NONE", "PERCENT", "FIXED"] = "NONE"
    agency_fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    agency_fee_amount_paise: int | None = Field(default=None, ge=0)
    lines: list[POLineIn] = Field(min_length=1)


class POAmendIn(BaseModel):
    """Editable header (only supplied keys apply) + an OPTIONAL full line replacement."""

    model_config = ConfigDict(extra="forbid")
    po_number: str | None = Field(default=None, max_length=64)
    client_gstin_id: int | None = None
    project_id: int | None = None
    po_date: date | None = None
    expected_procurement_date: date | None = None
    notes: str | None = Field(default=None, max_length=1000)
    soft_copy_file_id: int | None = None
    agency_fee_type: Literal["NONE", "PERCENT", "FIXED"] | None = None
    agency_fee_percent: Decimal | None = Field(default=None, ge=0, le=100)
    agency_fee_amount_paise: int | None = Field(default=None, ge=0)
    lines: list[POLineIn] | None = Field(default=None, min_length=1)
    summary: str | None = Field(default=None, max_length=500)


class ShortCloseIn(BaseModel):
    line_id: int | None = None
    reason: str = Field(min_length=1, max_length=500)


class VoidIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SoftCopyOut(BaseModel):
    id: int
    filename: str


class POLineOut(BaseModel):
    id: int
    product_id: int
    product_name: str | None
    brand: str | None
    model_number: str | None
    description: str
    uom: str
    ordered_qty: str
    cost_price_paise: int
    original_cost_price_paise: int | None
    client_sell_price_paise: int | None
    vendor_sell_price_paise: int | None
    sell_price_paise: int | None       # ACTUAL sell — None for non-admins (masked)
    client_freight_paise: int | None
    vendor_freight_paise: int | None
    freight_paise: int | None          # ACTUAL freight — None for non-admins (masked)
    packaging_paise: int
    handling_paise: int
    other_paise: int
    tax_rate: str
    line_status: str
    short_closed_qty: str
    short_close_reason: str | None


class POSummaryOut(BaseModel):
    id: int
    po_number: str | None
    client_id: int
    client_name: str | None
    project_id: int
    project_code: str | None
    po_date: date
    expected_procurement_date: date | None
    status: str
    agency_fee_type: str
    agency_fee_percent: float | None
    agency_fee_amount_paise: int | None
    line_count: int
    total_sell_paise: int | None       # ACTUAL aggregate — None for non-admins (masked)
    total_client_sell_paise: int       # client-quoted GOODS aggregate — visible to all
    total_client_freight_paise: int    # client-quoted freight aggregate (revenue) — visible
    total_client_extras_paise: int     # packaging + handling + other (revenue) — visible
    # Agency fee is REVENUE we charge the client (not a cost, not sensitive) — visible to all.
    agency_fee_computed_paise: int     # resolved fee (percent-of-entire-client-billing or fixed)
    total_with_agency_paise: int       # entire client billing + agency = full revenue
    created_at: datetime


class PODetailOut(POSummaryOut):
    client_gstin: str | None
    notes: str | None
    soft_copy_file: SoftCopyOut | None
    amendments_count: int
    lines: list[POLineOut]


class BulkSkip(BaseModel):
    po_number: str
    reason: str


class BulkError(BaseModel):
    row: int
    reason: str


class BulkUploadOut(BaseModel):
    created: list[str]
    skipped: list[BulkSkip]
    errors: list[BulkError]


# ---------------------------------------------------------------- serializers

def _line_out(line: Any, *, can_see_actuals: bool) -> POLineOut:
    product = line.product
    return POLineOut(
        id=line.id,
        product_id=line.product_id,
        product_name=product.name if product is not None else None,
        brand=product.brand if product is not None else None,
        model_number=product.model_number if product is not None else None,
        description=line.description,
        uom=line.uom,
        ordered_qty=str(line.ordered_qty),
        cost_price_paise=line.cost_price_paise,
        original_cost_price_paise=line.original_cost_price_paise,
        client_sell_price_paise=line.client_sell_price_paise,
        vendor_sell_price_paise=line.vendor_sell_price_paise,
        # ADMIN-ONLY actuals: masked to None for a non-admin so the margin never leaks.
        sell_price_paise=line.sell_price_paise if can_see_actuals else None,
        client_freight_paise=line.client_freight_paise,
        vendor_freight_paise=line.vendor_freight_paise,
        freight_paise=line.freight_paise if can_see_actuals else None,
        packaging_paise=line.packaging_paise,
        handling_paise=line.handling_paise,
        other_paise=line.other_paise,
        tax_rate=str(line.tax_rate),
        line_status=line.line_status,
        short_closed_qty=str(line.short_closed_qty),
        short_close_reason=line.short_close_reason,
    )


def _summary_out(
    po: PurchaseOrder, labels: po_service.POLabels, *, can_see_actuals: bool
) -> POSummaryOut:
    return POSummaryOut(
        id=po.id,
        po_number=po.po_number,
        client_id=po.client_id,
        client_name=labels.client_names.get(po.client_id),
        project_id=po.project_id,
        project_code=labels.project_codes.get(po.project_id),
        po_date=po.po_date,
        expected_procurement_date=po.expected_procurement_date,
        status=po.status,
        agency_fee_type=po.agency_fee_type,
        agency_fee_percent=(
            float(po.agency_fee_percent) if po.agency_fee_percent is not None else None
        ),
        agency_fee_amount_paise=po.agency_fee_amount_paise,
        line_count=len(po.lines),
        # ACTUAL aggregate masked for non-admins; the client-sell total is always visible.
        total_sell_paise=po_service.po_total_sell_paise(po) if can_see_actuals else None,
        total_client_sell_paise=po_service.po_total_client_sell_paise(po),
        total_client_freight_paise=po_service.po_total_client_freight_paise(po),
        total_client_extras_paise=po_service.po_total_client_extras_paise(po),
        agency_fee_computed_paise=po_service.agency_fee_paise(po),
        total_with_agency_paise=po_service.po_total_with_agency_paise(po),
        created_at=po.created_at,
    )


def _detail_out(
    po: PurchaseOrder, labels: po_service.POLabels, *, can_see_actuals: bool
) -> PODetailOut:
    summary = _summary_out(po, labels, can_see_actuals=can_see_actuals)
    soft_copy: SoftCopyOut | None = None
    if po.soft_copy_file_id is not None:
        filename = labels.files.get(po.soft_copy_file_id)
        if filename is not None:
            soft_copy = SoftCopyOut(id=po.soft_copy_file_id, filename=filename)
    return PODetailOut(
        **summary.model_dump(),
        client_gstin=(
            labels.gstins.get(po.client_gstin_id)
            if po.client_gstin_id is not None else None
        ),
        notes=po.notes,
        soft_copy_file=soft_copy,
        amendments_count=len(po.amendments),
        lines=[_line_out(line, can_see_actuals=can_see_actuals) for line in po.lines],
    )


def _load_detail(db: Session, po_id: int, *, can_see_actuals: bool) -> PODetailOut:
    try:
        po = po_service.get_po_detail(db, po_id)
    except po_service.PONotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    return _detail_out(po, po_service.label_maps(db, [po]), can_see_actuals=can_see_actuals)


# ------------------------------------------------------------------- create

@router.post("/purchase-orders", response_model=PODetailOut, status_code=201)
def create_purchase_order(
    body: POCreateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    rbac.require_level(user, rbac.SALES_ORDERS, Level.OPERATE)
    can_actuals = _can_see_actuals(user)
    try:
        po = po_service.create_po(
            db,
            po_number=body.po_number,
            client_id=body.client_id,
            project_id=body.project_id,
            po_date=body.po_date,
            lines=[line.to_input() for line in body.lines],
            client_gstin_id=body.client_gstin_id,
            expected_procurement_date=body.expected_procurement_date,
            notes=body.notes,
            soft_copy_file_id=body.soft_copy_file_id,
            agency_fee_type=body.agency_fee_type,
            agency_fee_percent=body.agency_fee_percent,
            agency_fee_amount_paise=body.agency_fee_amount_paise,
            can_set_actuals=can_actuals,
            actor_uid=user.firebase_uid,
        )
    except po_service.POError as err:
        raise _map_error(err) from err
    db.commit()
    return _load_detail(db, po.id, can_see_actuals=can_actuals)


# ------------------------------------------------------------------- upload

@router.post("/purchase-orders/upload", response_model=BulkUploadOut, status_code=201)
def upload_purchase_orders(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
    client_id: Annotated[int, Form()],
    project_id: Annotated[int, Form()],
) -> BulkUploadOut:
    """Bulk-create POs from an .xlsx (one PO per ``po_number`` group). The client +
    project are validated (exist + Active) BEFORE anything is parsed/stored, so a bad
    allocation is a clean error that persists nothing.

    Excel columns: po_number, product_code (or product_name), description, uom,
    ordered_qty, cost_price, sell_price, freight, packaging, handling, other, tax_rate.
    Money columns are rupees; unknown products / malformed rows come back per-row in
    ``errors`` (and skip their whole PO); an existing po_number is a ``skipped`` entry."""
    rbac.require_level(user, rbac.SALES_ORDERS, Level.OPERATE)
    # Validate the allocation up-front (clean 400/404 before reading any bytes).
    try:
        po_service._ensure_client_active(db, client_id)
        po_service._ensure_project_active(db, project_id, client_id)
    except po_service.POError as err:
        raise _map_error(err) from err

    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)

    result = po_service.bulk_create_from_excel(
        db, bytes(data), client_id=client_id, project_id=project_id,
        actor_uid=user.firebase_uid)
    db.commit()
    return BulkUploadOut(
        created=result.created,
        skipped=[BulkSkip(po_number=pon, reason=reason) for pon, reason in result.skipped],
        errors=[BulkError(row=row, reason=reason) for row, reason in result.errors],
    )


# --------------------------------------------------------------- bulk template

# Registered BEFORE `/purchase-orders/{po_id}` so the literal `.xlsx` path is matched here
# rather than trying (and failing) to coerce "bulk-template.xlsx" to an int po_id.
@router.get("/purchase-orders/bulk-template.xlsx")
def download_bulk_template(
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """Download a ready-to-fill ``.xlsx`` template for the bulk PO upload: the exact header
    the parser accepts + one illustrative example row (money columns in rupees). Same OPERATE
    gate as the upload itself, so the auth-gated download never leaks to a viewer."""
    rbac.require_level(user, rbac.SALES_ORDERS, Level.OPERATE)
    return Response(
        content=po_service.build_bulk_template_xlsx(),
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="po-bulk-template.xlsx"',
        },
    )


# ------------------------------------------------------------------- register

@router.get("/purchase-orders", response_model=list[POSummaryOut])
def list_purchase_orders(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
    project_id: Annotated[int | None, Query()] = None,
    status_filter: Annotated[
        Literal["DRAFT", "CONFIRMED", "IN_PROGRESS", "CLOSED", "CANCELLED"] | None,
        Query(alias="status"),
    ] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[POSummaryOut]:
    _require_module(user)
    can_actuals = _can_see_actuals(user)
    pos = po_service.list_pos(
        db, client_id=client_id, project_id=project_id,
        status=status_filter, q=q, limit=limit, offset=offset)
    labels = po_service.label_maps(db, pos)
    return [_summary_out(po, labels, can_see_actuals=can_actuals) for po in pos]


@router.get("/purchase-orders/{po_id}", response_model=PODetailOut)
def get_purchase_order(
    po_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    _require_module(user)
    return _load_detail(db, po_id, can_see_actuals=_can_see_actuals(user))


# -------------------------------------------------------------------- amend

@router.patch("/purchase-orders/{po_id}", response_model=PODetailOut)
def amend_purchase_order(
    po_id: int,
    body: POAmendIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    rbac.require_level(user, rbac.SALES_ORDERS, Level.OPERATE)
    can_actuals = _can_see_actuals(user)
    try:
        po = po_service.get_po_detail(db, po_id)
    except po_service.PONotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    header = body.model_dump(exclude_unset=True, exclude={"lines", "summary"})
    lines = [line.to_input() for line in body.lines] if body.lines is not None else None
    try:
        po_service.amend_po(
            db, po, header=header, lines=lines, summary=body.summary,
            can_set_actuals=can_actuals, actor_uid=user.firebase_uid)
    except po_service.POValidationError as err:
        # A blocked amend (CANCELLED/CLOSED, empty line replacement) is 422.
        raise _map_error(err, validation_status=422) from err
    except po_service.POError as err:
        raise _map_error(err) from err
    db.commit()
    return _load_detail(db, po_id, can_see_actuals=can_actuals)


# ------------------------------------------------------------------- confirm

@router.post("/purchase-orders/{po_id}/confirm", response_model=PODetailOut)
def confirm_purchase_order(
    po_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    rbac.require_level(user, rbac.SALES_ORDERS, Level.OPERATE)
    try:
        po = po_service.get_po_detail(db, po_id)
    except po_service.PONotFound as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    try:
        po_service.confirm_po(db, po, actor_uid=user.firebase_uid)
    except po_service.POValidationError as err:
        # Confirming a non-DRAFT PO is a blocked transition -> 422 (mirrors amend).
        raise _map_error(err, validation_status=422) from err
    except po_service.POError as err:
        raise _map_error(err) from err
    db.commit()
    return _load_detail(db, po_id, can_see_actuals=_can_see_actuals(user))


# --------------------------------------------------------------- short-close

@router.post("/purchase-orders/{po_id}/short-close", response_model=PODetailOut)
def short_close_purchase_order(
    po_id: int,
    body: ShortCloseIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    if not can(user, "po.short_close"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "po.short_close requires Manage on this module")
    try:
        po = po_service.get_po_detail(db, po_id)
        po_service.short_close(
            db, po, reason=body.reason, line_id=body.line_id,
            actor_uid=user.firebase_uid)
    except po_service.POError as err:
        raise _map_error(err) from err
    db.commit()
    return _load_detail(db, po_id, can_see_actuals=_can_see_actuals(user))


# --------------------------------------------------------------------- void

@router.post("/purchase-orders/{po_id}/void", response_model=PODetailOut)
def void_purchase_order(
    po_id: int,
    body: VoidIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> PODetailOut:
    if not can(user, "po.void"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "po.void requires Manage on this module")
    try:
        po = po_service.get_po_detail(db, po_id)
        po_service.void_po(db, po, reason=body.reason, actor_uid=user.firebase_uid)
    except po_service.POError as err:
        raise _map_error(err) from err
    db.commit()
    return _load_detail(db, po_id, can_see_actuals=_can_see_actuals(user))
