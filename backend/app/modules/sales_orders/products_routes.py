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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.sales_orders import products_service as service
from app.modules.sales_orders.models import Product
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()


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
    active: bool
    created_at: datetime


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
) -> list[Product]:
    _require_view(user)
    return service.list_products(
        db, q=q, category=category, active=active, limit=limit, offset=offset
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
