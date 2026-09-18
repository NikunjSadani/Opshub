"""Product Master API (mounted under the sales_orders router at `/api/v1`).

  GET   /products        -> list/search products (module VIEW)
  GET   /products/{id}   -> one product (module VIEW)
  POST  /products        -> create a product (`product.manage` = MANAGE)
  PATCH /products/{id}   -> edit a product (`product.manage` = MANAGE)

Reads need the `sales_orders` module grant (>= View); writes require the
`product.manage` action (Manage). Products dedupe case-insensitively on
(name, brand, model_number); a collision (or a duplicate `code`) is a 409.
Every write is audited by the service.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
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
from app.modules.sales_orders import products_service as service
from app.modules.sales_orders.models import Product
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()

_UPLOAD_CHUNK = 1024 * 1024
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _require_view(user: User) -> None:
    rbac.require_module(user, rbac.SALES_ORDERS)


def _require_manage(user: User) -> None:
    if not rbac.can(user, "product.manage"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "product.manage requires MANAGE")


# ------------------------------------------------------------------- schemas

class ProductIn(BaseModel):
    code: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    model_number: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=120)
    uom: str = Field(default="PCS", min_length=1, max_length=20)
    hsn: str | None = Field(default=None, max_length=10)
    # GST % (0..100, <=2 dp). Numeric(5,2) — matches the PO line's tax_rate constraints.
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)


class ProductUpdate(BaseModel):
    # All optional; only fields the client actually sends are applied (an explicit
    # null clears an optional field, an omitted key is left untouched).
    code: str | None = Field(default=None, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, max_length=120)
    model_number: str | None = Field(default=None, max_length=120)
    category: str | None = Field(default=None, max_length=120)
    uom: str | None = Field(default=None, min_length=1, max_length=20)
    hsn: str | None = Field(default=None, max_length=10)
    gst_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)
    active: bool | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    code: str | None
    name: str
    brand: str | None
    model_number: str | None
    category: str | None
    uom: str
    hsn: str | None
    gst_rate: Decimal | None
    active: bool
    created_at: datetime


class ProductBulkError(BaseModel):
    row: int
    message: str


class ProductsBulkOut(BaseModel):
    created: list[str]   # CODES of the products created this upload
    updated: list[str]   # CODES of the products updated this upload
    errors: list[ProductBulkError]


class HsnSyncUpdated(BaseModel):
    hsn: str
    old_rate: str
    new_rate: str


class HsnSyncConflict(BaseModel):
    hsn: str
    rates: list[str]         # the distinct rates the active products disagree on
    product_ids: list[int]   # the active products carrying this hsn


class ProductsHsnSyncOut(BaseModel):
    created: list[str]                  # HSN codes newly added to md_hsn
    updated: list[HsnSyncUpdated]       # HSN codes whose rate was corrected (old -> new)
    conflicts: list[HsnSyncConflict]    # HSNs with disagreeing active products (still applied)


# ------------------------------------------------------------------- routes

@router.get("/products", response_model=list[ProductOut])
def list_products(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200)] = None,
    category: Annotated[str | None, Query(max_length=120)] = None,
    active: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    project_id: Annotated[int | None, Query()] = None,
) -> list[Product]:
    _require_view(user)
    return service.list_products(
        db, q=q, category=category, active=active, limit=limit, offset=offset,
        project_id=project_id,
    )


# --------------------------------------------------------------- bulk template
#
# Registered BEFORE `/products/{product_id}` so the literal `.xlsx` path is matched here
# rather than trying (and failing) to coerce "bulk-template.xlsx" to an int product_id.
@router.get("/products/bulk-template.xlsx")
def download_bulk_template(
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """Download a ready-to-fill ``.xlsx`` template for the bulk product upsert: the exact
    header the parser accepts + one illustrative example row. MANAGE-gated like the upload
    (same as single create), so the download never leaks to a viewer."""
    _require_manage(user)
    return Response(
        content=service.build_products_template_xlsx(),
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="products-bulk-template.xlsx"',
        },
    )


# ------------------------------------------------------------------- upload

@router.post("/products/upload", response_model=ProductsBulkOut, status_code=201)
def upload_products(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
) -> ProductsBulkOut:
    """Bulk CREATE/UPDATE products from an .xlsx. Each row matches an existing product by
    exact ``code`` (when supplied) or by case-insensitive identity (name, brand,
    model_number); a match is updated with the row's provided fields, a miss is created (code
    auto-minted when omitted). Malformed / colliding / reserved-code rows come back per-row in
    ``errors`` without poisoning the batch. MANAGE-gated (same as single create)."""
    _require_manage(user)
    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)

    result = service.bulk_upsert_from_excel(db, bytes(data), actor_uid=user.firebase_uid)
    db.commit()
    return ProductsBulkOut(
        created=result.created,
        updated=result.updated,
        errors=[ProductBulkError(row=row, message=msg) for row, msg in result.errors],
    )


# ------------------------------------------------------------ HSN-master reconcile
#
# Registered BEFORE `/products/{product_id}` so the literal path matches here rather than
# coercing "sync-hsn-master" to an int product_id.
@router.post("/products/sync-hsn-master", response_model=ProductsHsnSyncOut)
def sync_hsn_master(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProductsHsnSyncOut:
    """Rebuild the challan HSN master (`md_hsn`) from every product carrying BOTH an `hsn`
    and a `gst_rate`. New HSNs are created, changed rates corrected (product master is the
    source of truth), and HSNs where active products disagree are reported as ``conflicts``
    while still applying a deterministic rate so the run is stable. MANAGE-gated (same as
    the product bulk upload)."""
    _require_manage(user)
    try:
        # Re-validation of a winning rate can raise (e.g. a rate imported directly into the DB
        # out of the 0..100 range) — surface it as a clean 422, never a 500, mirroring the
        # create/patch routes. The run is fail-closed: nothing is committed on the raise.
        result = service.sync_hsn_master_from_products(db, actor_uid=user.firebase_uid)
    except service.ProductError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    return ProductsHsnSyncOut(
        created=result.created,
        updated=[HsnSyncUpdated(**u) for u in result.updated],
        conflicts=[HsnSyncConflict(**c) for c in result.conflicts],
    )


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Product:
    _require_view(user)
    try:
        return service.get_product(db, product_id)
    except service.ProductNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(
    body: ProductIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Product:
    _require_manage(user)
    try:
        product = service.create_product(
            db,
            name=body.name,
            code=body.code,
            brand=body.brand,
            model_number=body.model_number,
            category=body.category,
            uom=body.uom,
            hsn=body.hsn,
            gst_rate=body.gst_rate,
            actor_uid=user.firebase_uid,
        )
    except service.DuplicateProduct as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.ProductError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(product)
    return product


@router.patch("/products/{product_id}", response_model=ProductOut)
def patch_product(
    product_id: int,
    body: ProductUpdate,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Product:
    _require_manage(user)
    fields: dict[str, Any] = body.model_dump(exclude_unset=True)
    try:
        product = service.update_product(
            db, product_id=product_id, fields=fields, actor_uid=user.firebase_uid
        )
    except service.ProductNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except service.DuplicateProduct as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.ProductError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    db.commit()
    db.refresh(product)
    return product
