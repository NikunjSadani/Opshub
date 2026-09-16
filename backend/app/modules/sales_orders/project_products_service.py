"""Project-Product tag service — curates a project's PO product picker + pricing template.

A tag is a link row (`project_product`) between a SHARED Product master and a Project,
never a re-parenting: one product can be tagged to many projects and vice-versa. The
`uq_project_product` unique constraint is the DB backstop, so the tag/untag operations
are IDEMPOTENT — a re-tag returns the existing row (WITHOUT overwriting its fields), an
un-tag of an absent pair is a no-op. Every write is audited inside the caller's
transaction (WoW: everything traceable).

Each tag also carries a per-project PRICING TEMPLATE (mirrors the PO line, minus qty) a
PO pre-fills from. `sell_price_paise`/`freight_paise` are the ACTUAL (margin-basis)
figures — ADMIN-ONLY; the ROUTE masks them on read and drops them from a non-admin's
write, so this service applies only what it is handed.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.modules.projects.models import Project
from app.modules.sales_orders.models import Product, ProjectProduct
from app.platform import audit

# The pricing-template columns a tag/update may carry (mirror the PO line, no ordered_qty).
# The route validates bounds (money ge 0, tax 0..100) and drops the two ADMIN-ONLY actuals
# (`sell_price_paise`/`freight_paise`) for a non-admin BEFORE calling in — the service just
# applies whatever survives.
PRICING_FIELDS: tuple[str, ...] = (
    "description",
    "uom",
    "cost_price_paise",
    "original_cost_price_paise",
    "client_sell_price_paise",
    "vendor_sell_price_paise",
    "sell_price_paise",
    "client_freight_paise",
    "vendor_freight_paise",
    "freight_paise",
    "packaging_paise",
    "handling_paise",
    "other_paise",
    "tax_rate",
)


class ProjectProductError(Exception):
    """Base error for a project-product tag operation."""


class ProjectNotFound(ProjectProductError):
    """The referenced project is missing (route -> 404)."""


class ProductNotFound(ProjectProductError):
    """The referenced product is missing (route -> 404)."""


class TagNotFound(ProjectProductError):
    """The (project, product) tag being edited does not exist (route -> 404)."""


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(f"project {project_id} not found")
    return project


def _apply_fields(tag: ProjectProduct, fields: dict[str, Any]) -> None:
    """Set only the recognized pricing keys present in `fields` on `tag`. Unknown keys
    are ignored (the route's pydantic model is the field allow-list)."""
    for key in PRICING_FIELDS:
        if key in fields:
            setattr(tag, key, fields[key])


def list_tagged(db: Session, *, project_id: int) -> list[ProjectProduct]:
    """Return every tag row for `project_id` with its joined product (including inactive
    products — the FE marks them), ordered by product name. Raises `ProjectNotFound` if
    the project doesn't exist. Each row carries the per-project pricing template."""
    _require_project(db, project_id)
    stmt = (
        select(ProjectProduct)
        .join(Product, ProjectProduct.product_id == Product.id)
        .options(joinedload(ProjectProduct.product))
        .where(ProjectProduct.project_id == project_id)
        .order_by(Product.name)
    )
    return list(db.execute(stmt).scalars())


def tag_product(
    db: Session,
    *,
    project_id: int,
    product_id: int,
    fields: dict[str, Any] | None = None,
    actor_uid: str | None = None,
) -> tuple[ProjectProduct, bool]:
    """Tag `product_id` to `project_id`, optionally seeding the pricing template from
    `fields`. IDEMPOTENT: if the pair is already tagged, return the EXISTING tag with
    created=False and DO NOT overwrite its fields (editing is via `update_template`);
    otherwise create the tag with `fields` and return created=True. Validates both the
    project and the product exist (404 each). Audited `sales_orders.product_tagged` on a
    NEW tag only.

    Returns `(tag, created)`.
    """
    _require_project(db, project_id)
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFound(f"product {product_id} not found")

    existing = db.execute(
        select(ProjectProduct).where(
            ProjectProduct.project_id == project_id,
            ProjectProduct.product_id == product_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent re-tag: never clobber an already-templated row.
        return existing, False

    tag = ProjectProduct(
        project_id=project_id, product_id=product_id, created_by=actor_uid
    )
    _apply_fields(tag, fields or {})
    try:
        # `with` releases the savepoint on success; a concurrent tag of the same pair
        # trips the `uq_project_product` unique constraint at flush and lands here —
        # idempotent, so the pre-existing tag is treated as success.
        with db.begin_nested():
            db.add(tag)
            db.flush()
    except IntegrityError:
        # Lost the race: the pair now exists — return it without overwriting its fields.
        winner = db.execute(
            select(ProjectProduct).where(
                ProjectProduct.project_id == project_id,
                ProjectProduct.product_id == product_id,
            )
        ).scalar_one()
        return winner, False

    audit.log(
        db,
        action="sales_orders.product_tagged",
        actor_uid=actor_uid,
        entity="project_product",
        entity_id=str(tag.id),
        detail={"project_id": project_id, "product_id": product_id},
    )
    return tag, True


def update_template(
    db: Session,
    *,
    project_id: int,
    product_id: int,
    fields: dict[str, Any],
    actor_uid: str | None = None,
) -> ProjectProduct:
    """Partial-update the pricing template of an EXISTING (project, product) tag: apply
    only the keys present in `fields` (the route passes an `exclude_unset` dump, already
    stripped of the ADMIN-ONLY actuals for a non-admin), leaving every other column
    untouched. Raises `TagNotFound` (route -> 404) if the pair isn't tagged.

    Audited `sales_orders.product_template_updated` with the SORTED changed field NAMES
    only — never the actual money values — so the trail can't leak a masked actual.
    """
    tag = db.execute(
        select(ProjectProduct).where(
            ProjectProduct.project_id == project_id,
            ProjectProduct.product_id == product_id,
        )
    ).scalar_one_or_none()
    if tag is None:
        raise TagNotFound(
            f"product {product_id} is not tagged to project {project_id}"
        )

    changed = [key for key in PRICING_FIELDS if key in fields]
    _apply_fields(tag, fields)
    db.flush()

    audit.log(
        db,
        action="sales_orders.product_template_updated",
        actor_uid=actor_uid,
        entity="project_product",
        entity_id=str(tag.id),
        detail={
            "project_id": project_id,
            "product_id": product_id,
            "fields": sorted(changed),
        },
    )
    return tag


def untag_product(
    db: Session,
    *,
    project_id: int,
    product_id: int,
    actor_uid: str | None = None,
) -> bool:
    """Remove the (project, product) tag if present. IDEMPOTENT: returns True when a
    row was removed, False when there was nothing to remove. Audited
    `sales_orders.product_untagged` only when a row was removed."""
    tag = db.execute(
        select(ProjectProduct).where(
            ProjectProduct.project_id == project_id,
            ProjectProduct.product_id == product_id,
        )
    ).scalar_one_or_none()
    if tag is None:
        return False

    tag_id = tag.id  # capture before delete — a deleted instance's attrs may expire
    db.delete(tag)
    db.flush()
    audit.log(
        db,
        action="sales_orders.product_untagged",
        actor_uid=actor_uid,
        entity="project_product",
        entity_id=str(tag_id),
        detail={"project_id": project_id, "product_id": product_id},
    )
    return True
