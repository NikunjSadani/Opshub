"""Project-Product tag + pricing-template API (mounted under the sales_orders router).

  GET    /project-products?project_id={id}          -> a project's tags + template (VIEW)
  POST   /project-products {project_id, product_id, ...pricing} -> tag + seed  (`product.tag`)
  PATCH  /project-products/{project_id}/{product_id} -> edit the template       (`product.tag`)
  DELETE /project-products/{project_id}/{product_id} -> untag a product         (`product.tag`)

Tagging curates a project's PO product picker AND holds a per-project PRICING TEMPLATE a
PO pre-fills from. Reads need the `sales_orders` module grant (>= View); writes require
the `product.tag` action (Operate). Tag is IDEMPOTENT (a re-tag never overwrites an
existing template — edit via PATCH); every write is audited by the service.

MASKING (mirrors the PO line exactly — `_can_see_actuals` = platform IAM):
* READ  — a non-admin sees `sell_price_paise`/`freight_paise` as null; every other field as-is.
* WRITE — a non-admin's `sell_price_paise`/`freight_paise` are DROPPED before persist (an admin
  may set them). A non-admin PATCH that names an actual simply skips it (never clears it).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.sales_orders import project_products_service as service
from app.modules.sales_orders.models import ProjectProduct

# Reuse the PO's admin check VERBATIM so the masking contract can never drift from the PO line.
from app.modules.sales_orders.po_routes import _can_see_actuals
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()

# The two ADMIN-ONLY actual columns — masked to None on read, dropped from a non-admin write.
_ACTUAL_FIELDS = ("sell_price_paise", "freight_paise")


def _require_view(user: User) -> None:
    rbac.require_module(user, rbac.SALES_ORDERS)


def _require_tag(user: User) -> None:
    if not rbac.can(user, "product.tag"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "product.tag requires OPERATE")


# ------------------------------------------------------------------- schemas

# Money ceiling: a per-unit/per-line paise value beyond this is a clean 422, never a
# BigInteger (int8) DB overflow -> 500 (a template is a human-entered default; a typo of
# 10^20 must degrade to a 4xx, mirroring the billing money guard).
_MAX_MONEY_PAISE = 10**15


class ProjectProductWrite(BaseModel):
    """The 14 pricing-template fields, all OPTIONAL — the write mixin shared by tag +
    patch. Bounds mirror the PO line (`POLineIn`): money is a non-negative paise value
    within the int8 ceiling, tax is a 0..100 percent fitting Numeric(5,2) (over-scale is a
    422, never a silent DB round). `sell_price_paise`/`freight_paise` are the ADMIN-ONLY
    actuals."""

    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=500)
    uom: str | None = Field(default=None, max_length=20)
    cost_price_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    original_cost_price_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    client_sell_price_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    vendor_sell_price_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    sell_price_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    client_freight_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    vendor_freight_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    freight_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    packaging_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    handling_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    other_paise: int | None = Field(default=None, ge=0, le=_MAX_MONEY_PAISE)
    # Numeric(5,2): reject an over-scale tax rather than let Postgres silently round it
    # (SQLite keeps full precision, which would hide the divergence in tests).
    tax_rate: Decimal | None = Field(default=None, ge=0, le=100, max_digits=5, decimal_places=2)


class ProjectProductIn(ProjectProductWrite):
    """POST body: which product to tag to which project, plus the optional seed template."""

    project_id: int
    product_id: int


class ProjectProductOut(BaseModel):
    """A tag row = the joined product identity + the per-project pricing template. The two
    ACTUAL fields are serialized as null for a non-admin (built masked in `_out`)."""

    # --- product identity (from the joined Product) ---
    product_id: int
    code: str | None
    name: str
    brand: str | None
    model_number: str | None
    category: str | None
    active: bool
    # The Product master's own unit of measure (String(20), default "PCS") — ALWAYS
    # present. A PO pre-fill falls back to this when the template's `uom` override is unset.
    product_uom: str
    # --- pricing template ---
    description: str | None
    uom: str | None
    cost_price_paise: int | None
    original_cost_price_paise: int | None
    client_sell_price_paise: int | None
    vendor_sell_price_paise: int | None
    sell_price_paise: int | None       # ACTUAL sell — None for non-admins (masked)
    client_freight_paise: int | None
    vendor_freight_paise: int | None
    freight_paise: int | None          # ACTUAL freight — None for non-admins (masked)
    packaging_paise: int | None
    handling_paise: int | None
    # Serialized as a string (e.g. "18.00") — matches the PO line's `tax_rate: str`, so the
    # FE always receives a string and never a bare number.
    other_paise: int | None
    tax_rate: str | None


# ---------------------------------------------------------------- serializer

def _out(tag: ProjectProduct, *, can_see_actuals: bool) -> ProjectProductOut:
    product = tag.product
    return ProjectProductOut(
        product_id=tag.product_id,
        code=product.code,
        name=product.name,
        brand=product.brand,
        model_number=product.model_number,
        category=product.category,
        active=product.active,
        product_uom=product.uom,
        description=tag.description,
        uom=tag.uom,
        cost_price_paise=tag.cost_price_paise,
        original_cost_price_paise=tag.original_cost_price_paise,
        client_sell_price_paise=tag.client_sell_price_paise,
        vendor_sell_price_paise=tag.vendor_sell_price_paise,
        # ADMIN-ONLY actuals: masked to None for a non-admin so the margin never leaks.
        sell_price_paise=tag.sell_price_paise if can_see_actuals else None,
        client_freight_paise=tag.client_freight_paise,
        vendor_freight_paise=tag.vendor_freight_paise,
        freight_paise=tag.freight_paise if can_see_actuals else None,
        packaging_paise=tag.packaging_paise,
        handling_paise=tag.handling_paise,
        other_paise=tag.other_paise,
        tax_rate=str(tag.tax_rate) if tag.tax_rate is not None else None,
    )


def _drop_actuals_if_masked(fields: dict[str, Any], *, can_see_actuals: bool) -> None:
    """A non-admin can never persist an actual (mirrors the PO create/amend path): drop
    both actual keys from the write payload entirely."""
    if not can_see_actuals:
        for key in _ACTUAL_FIELDS:
            fields.pop(key, None)


# ------------------------------------------------------------------- routes

@router.get("/project-products", response_model=list[ProjectProductOut])
def list_project_products(
    project_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[ProjectProductOut]:
    _require_view(user)
    can_actuals = _can_see_actuals(user)
    try:
        tags = service.list_tagged(db, project_id=project_id)
    except service.ProjectNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [_out(tag, can_see_actuals=can_actuals) for tag in tags]


@router.post("/project-products", response_model=ProjectProductOut)
def tag_project_product(
    body: ProjectProductIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> ProjectProductOut:
    _require_tag(user)
    can_actuals = _can_see_actuals(user)
    fields = body.model_dump(exclude_unset=True, exclude={"project_id", "product_id"})
    _drop_actuals_if_masked(fields, can_see_actuals=can_actuals)
    try:
        tag, created = service.tag_product(
            db,
            project_id=body.project_id,
            product_id=body.product_id,
            fields=fields,
            actor_uid=user.firebase_uid,
        )
    except service.ProjectNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except service.ProductNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    db.commit()
    db.refresh(tag)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _out(tag, can_see_actuals=can_actuals)


@router.patch(
    "/project-products/{project_id}/{product_id}", response_model=ProjectProductOut
)
def patch_project_product(
    project_id: int,
    product_id: int,
    body: ProjectProductWrite,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectProductOut:
    _require_tag(user)
    can_actuals = _can_see_actuals(user)
    fields = body.model_dump(exclude_unset=True)
    _drop_actuals_if_masked(fields, can_see_actuals=can_actuals)
    try:
        tag = service.update_template(
            db,
            project_id=project_id,
            product_id=product_id,
            fields=fields,
            actor_uid=user.firebase_uid,
        )
    except service.TagNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    db.commit()
    db.refresh(tag)
    return _out(tag, can_see_actuals=can_actuals)


@router.delete("/project-products/{project_id}/{product_id}")
def untag_project_product(
    project_id: int,
    product_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    _require_tag(user)
    deleted = service.untag_product(
        db, project_id=project_id, product_id=product_id, actor_uid=user.firebase_uid
    )
    db.commit()
    return {"deleted": deleted}
