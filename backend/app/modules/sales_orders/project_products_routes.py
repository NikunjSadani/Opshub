"""Project-Product tag API (mounted under the sales_orders router at `/api/v1`).

  GET    /project-products?project_id={id}         -> a project's tagged products (VIEW)
  POST   /project-products {project_id, product_id} -> tag a product   (`product.tag` = OPERATE)
  DELETE /project-products/{project_id}/{product_id} -> untag a product (`product.tag` = OPERATE)

Tagging curates a project's PO product picker (money-free curation). Reads need the
`sales_orders` module grant (>= View); writes require the `product.tag` action (Operate).
Both writes are IDEMPOTENT and audited by the service. The tagged-products list returns
ALL of a project's tags (including inactive products — the FE marks them).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.sales_orders import project_products_service as service
from app.modules.sales_orders.models import Product
from app.modules.sales_orders.products_routes import ProductOut
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()


def _require_view(user: User) -> None:
    rbac.require_module(user, rbac.SALES_ORDERS)


def _require_tag(user: User) -> None:
    if not rbac.can(user, "product.tag"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "product.tag requires OPERATE")


# ------------------------------------------------------------------- schemas

class ProjectProductIn(BaseModel):
    project_id: int
    product_id: int


# ------------------------------------------------------------------- routes

@router.get("/project-products", response_model=list[ProductOut])
def list_project_products(
    project_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[Product]:
    _require_view(user)
    try:
        return service.list_tagged_products(db, project_id=project_id)
    except service.ProjectNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/project-products", response_model=ProductOut)
def tag_project_product(
    body: ProjectProductIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    response: Response,
) -> Product:
    _require_tag(user)
    try:
        product, created = service.tag_product(
            db,
            project_id=body.project_id,
            product_id=body.product_id,
            actor_uid=user.firebase_uid,
        )
    except service.ProjectNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except service.ProductNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    db.commit()
    db.refresh(product)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return product


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
