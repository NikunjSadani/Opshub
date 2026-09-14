"""Project-Product tag service — curates a project's PO product picker (money-free).

A tag is a link row (`project_product`) between a SHARED Product master and a Project,
never a re-parenting: one product can be tagged to many projects and vice-versa. The
`uq_project_product` unique constraint is the DB backstop, so the tag/untag operations
are IDEMPOTENT — a re-tag returns the existing row, an un-tag of an absent pair is a
no-op. Every write is audited inside the caller's transaction (WoW: everything traceable).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.projects.models import Project
from app.modules.sales_orders.models import Product, ProjectProduct
from app.platform import audit


class ProjectProductError(Exception):
    """Base error for a project-product tag operation."""


class ProjectNotFound(ProjectProductError):
    """The referenced project is missing (route -> 404)."""


class ProductNotFound(ProjectProductError):
    """The referenced product is missing (route -> 404)."""


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ProjectNotFound(f"project {project_id} not found")
    return project


def list_tagged_products(db: Session, *, project_id: int) -> list[Product]:
    """Return every product tagged to `project_id` (including inactive — the FE marks
    them), ordered by name. Raises `ProjectNotFound` if the project doesn't exist."""
    _require_project(db, project_id)
    stmt = (
        select(Product)
        .join(ProjectProduct, ProjectProduct.product_id == Product.id)
        .where(ProjectProduct.project_id == project_id)
        .order_by(Product.name)
    )
    return list(db.execute(stmt).scalars())


def tag_product(
    db: Session,
    *,
    project_id: int,
    product_id: int,
    actor_uid: str | None = None,
) -> tuple[Product, bool]:
    """Tag `product_id` to `project_id`. IDEMPOTENT: if the pair is already tagged,
    return the existing product with created=False; otherwise create the tag and
    return created=True. Validates both the project and the product exist (404 each).
    Audited `sales_orders.product_tagged` on a NEW tag only.

    Returns `(product, created)`.
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
        return product, False

    tag = ProjectProduct(
        project_id=project_id, product_id=product_id, created_by=actor_uid
    )
    try:
        # `with` releases the savepoint on success; a concurrent tag of the same pair
        # trips the `uq_project_product` unique constraint at flush and lands here —
        # idempotent, so the pre-existing tag is treated as success.
        with db.begin_nested():
            db.add(tag)
            db.flush()
    except IntegrityError:
        return product, False

    audit.log(
        db,
        action="sales_orders.product_tagged",
        actor_uid=actor_uid,
        entity="project_product",
        entity_id=str(tag.id),
        detail={"project_id": project_id, "product_id": product_id},
    )
    return product, True


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
