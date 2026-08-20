"""Product Master service — the normalized product identity PO lines reference.

Integrity discipline (mirrors the projects/numbering services):

  * A product's identity is `(lower(name), lower(coalesce(brand,'')),
    lower(coalesce(model_number,'')))`, enforced case-insensitively by the DB
    functional unique index `uq_product_identity`. The optional `code` is a
    second globally-unique key. Both are the real dedup backstop — a pre-check
    can't close the race, so the insert/update runs inside a SAVEPOINT and a
    collision at flush is caught and re-raised as `DuplicateProduct` (route
    -> 409).
  * Every mutation is audited inside the caller's transaction, so the trail
    commits atomically with the change.

⚠️ `db.begin_nested()` MUST be used as `with db.begin_nested():` — a bare call
leaks a SAVEPOINT per insert.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.sales_orders.models import Product
from app.platform import audit

_MAX_NAME_LEN = 200
_MAX_CODE_LEN = 40
_MAX_SHORT_LEN = 120
_MAX_UOM_LEN = 20
_MAX_HSN_LEN = 10

# Required (non-null) string fields and their column caps.
_REQUIRED_FIELDS: dict[str, int] = {"name": _MAX_NAME_LEN, "uom": _MAX_UOM_LEN}
# Optional string fields (empty collapses to None) and their column caps.
_OPTIONAL_FIELDS: dict[str, int] = {
    "code": _MAX_CODE_LEN,
    "brand": _MAX_SHORT_LEN,
    "model_number": _MAX_SHORT_LEN,
    "category": _MAX_SHORT_LEN,
    "hsn": _MAX_HSN_LEN,
}
# Every mutable field a PATCH may touch (adds the boolean `active`).
_PATCHABLE: frozenset[str] = frozenset(_REQUIRED_FIELDS) | frozenset(_OPTIONAL_FIELDS) | {"active"}


class ProductError(Exception):
    """Raised for an invalid product operation (route maps to 422)."""


class DuplicateProduct(ProductError):
    """The product identity or code already exists (route maps to 409)."""


class ProductNotFound(ProductError):
    """The referenced product is missing (route maps to 404)."""


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed `%`/`_` matches literally (paired with
    `escape="\\"` on `.ilike()`). The backslash is escaped first so it stays the
    escape char; without this a literal `%` in `q` would match every row."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean_required(value: str, *, field: str, max_len: int) -> str:
    """Strip a required string; reject empty or over-cap so a direct service caller
    can't persist junk or exceed the column width."""
    cleaned = value.strip()
    if not cleaned:
        raise ProductError(f"{field} is required")
    if len(cleaned) > max_len:
        raise ProductError(f"{field} must be at most {max_len} characters")
    return cleaned


def _clean_optional(value: str | None, *, field: str, max_len: int) -> str | None:
    """Strip an optional string; empty/whitespace collapses to None so identity
    stays consistent (the index treats NULL brand/model as '')."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise ProductError(f"{field} must be at most {max_len} characters")
    return cleaned


def create_product(
    db: Session,
    *,
    name: str,
    code: str | None = None,
    brand: str | None = None,
    model_number: str | None = None,
    category: str | None = None,
    uom: str = "PCS",
    hsn: str | None = None,
    actor_uid: str | None = None,
) -> Product:
    """Create a product, deduped case-insensitively on (name, brand, model_number).

    Strings are normalized (strip; empty optionals -> None). A collision on the
    identity index OR the unique `code` trips the DB backstop at flush and is
    surfaced as `DuplicateProduct` (route -> 409). Audited `product.created`.
    """
    clean_name = _clean_required(name, field="name", max_len=_MAX_NAME_LEN)
    clean_uom = _clean_required(uom, field="uom", max_len=_MAX_UOM_LEN)
    clean_code = _clean_optional(code, field="code", max_len=_MAX_CODE_LEN)
    clean_brand = _clean_optional(brand, field="brand", max_len=_MAX_SHORT_LEN)
    clean_model = _clean_optional(model_number, field="model_number", max_len=_MAX_SHORT_LEN)
    clean_category = _clean_optional(category, field="category", max_len=_MAX_SHORT_LEN)
    clean_hsn = _clean_optional(hsn, field="hsn", max_len=_MAX_HSN_LEN)

    product = Product(
        name=clean_name,
        code=clean_code,
        brand=clean_brand,
        model_number=clean_model,
        category=clean_category,
        uom=clean_uom,
        hsn=clean_hsn,
        created_by=actor_uid,
    )
    try:
        # `with` releases the savepoint on success (no leak); a concurrent create
        # of the same identity/code trips a UNIQUE index at flush and lands here.
        with db.begin_nested():
            db.add(product)
            db.flush()
    except IntegrityError as exc:
        raise DuplicateProduct("a product with this identity or code already exists") from exc

    audit.log(
        db,
        action="product.created",
        actor_uid=actor_uid,
        entity="product",
        entity_id=str(product.id),
        detail={
            "name": clean_name,
            "code": clean_code,
            "brand": clean_brand,
            "model_number": clean_model,
            "category": clean_category,
            "uom": clean_uom,
            "hsn": clean_hsn,
        },
    )
    return product


def get_product(db: Session, product_id: int) -> Product:
    """Return the product with `product_id`, or raise `ProductNotFound`."""
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFound(f"product {product_id} not found")
    return product


def list_products(
    db: Session,
    *,
    q: str | None = None,
    category: str | None = None,
    active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Product]:
    """List/search products. `q` is a LIKE-escaped substring across name/brand/
    model_number; `category` and `active` are exact filters. Newest first."""
    stmt = select(Product).order_by(Product.id.desc())
    if q:
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            Product.name.ilike(like, escape="\\")
            | Product.brand.ilike(like, escape="\\")
            | Product.model_number.ilike(like, escape="\\")
        )
    if category:
        stmt = stmt.where(func.lower(Product.category) == category.strip().lower())
    if active is not None:
        stmt = stmt.where(Product.active == active)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def update_product(
    db: Session,
    *,
    product_id: int,
    fields: dict[str, Any],
    actor_uid: str | None = None,
) -> Product:
    """Patch a product with only the keys present in `fields` (the route passes the
    client's `exclude_unset` dump, so an omitted key is left untouched while an
    explicit `null` clears an optional field).

    A change that collides with another product's identity/code trips the DB
    backstop and is surfaced as `DuplicateProduct` (route -> 409). Missing row
    raises `ProductNotFound` (route -> 404). Audited `product.updated`.
    """
    unknown = set(fields) - _PATCHABLE
    if unknown:
        raise ProductError(f"unknown field(s): {sorted(unknown)}")

    product = get_product(db, product_id)
    changed: dict[str, Any] = {}

    for field, max_len in _REQUIRED_FIELDS.items():
        if field in fields:
            value = fields[field]
            if not isinstance(value, str):
                raise ProductError(f"{field} must be a string")
            required_clean = _clean_required(value, field=field, max_len=max_len)
            setattr(product, field, required_clean)
            changed[field] = required_clean

    for field, max_len in _OPTIONAL_FIELDS.items():
        if field in fields:
            value = fields[field]
            if value is not None and not isinstance(value, str):
                raise ProductError(f"{field} must be a string or null")
            optional_clean = _clean_optional(value, field=field, max_len=max_len)
            setattr(product, field, optional_clean)
            changed[field] = optional_clean

    if "active" in fields:
        value = fields["active"]
        if not isinstance(value, bool):
            raise ProductError("active must be a boolean")
        product.active = value
        changed["active"] = value

    try:
        with db.begin_nested():
            db.flush()  # trips the identity/code unique backstop if this edit collides
    except IntegrityError as exc:
        raise DuplicateProduct("a product with this identity or code already exists") from exc

    audit.log(
        db,
        action="product.updated",
        actor_uid=actor_uid,
        entity="product",
        entity_id=str(product.id),
        detail=changed,
    )
    return product
