"""Logistics API — delivery tracking for issued challans. Mounted at `/api/v1/logistics`.

  POST   /logistics/shipments            -> create one shipment (OPERATE)
  POST   /logistics/shipments/upload     -> bulk .xlsx UPSERT on challan_number (OPERATE)
  GET    /logistics/shipments            -> list + filters (VIEW)
  GET    /logistics/shipments/{id}       -> one shipment incl. resolved challan + POD (VIEW)
  PATCH  /logistics/shipments/{id}       -> update status/tracking/partner/dates/... (OPERATE)
  POST   /logistics/shipments/{id}/pod   -> attach an already-uploaded POD file (OPERATE)
  DELETE /logistics/shipments/{id}       -> delete a shipment (MANAGE)

RBAC module key ``logistics``: ``shipment.upload`` = OPERATE, ``shipment.manage`` =
MANAGE, reads = VIEW. `status` is validated against DeliveryStatus (400 on a bad
value). Writes are audited by the service.

POD contract: `POST .../{id}/pod` takes a JSON `pod_file_id` that was ALREADY
uploaded via `POST /files/upload` (which returns `{id}`); the shipment stores that
id in `pod_file_id`. (One path, chosen for simplicity — no second multipart lane.)

Excel contract for the bulk upload — first sheet, row 1 = headers (case-insensitive,
friendly aliases accepted), columns: challan_number (required), tracking_id,
delivery_partner, status, consignee_name, address, phone, pincode, dispatched_on,
delivered_on, notes. UPSERT on challan_number; returns {created, updated, errors}.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.files.models import StoredFile
from app.modules.logistics import service
from app.modules.logistics.models import Shipment
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User

router = APIRouter()

MODULE_KEY = "logistics"

_UPLOAD_CHUNK = 1024 * 1024


# ------------------------------------------------------------------- gates


def _require_view(user: User) -> None:
    rbac.require_module(user, rbac.LOGISTICS)


def _require_operate(user: User) -> None:
    rbac.require_level(user, rbac.LOGISTICS, Level.OPERATE)


def _require_manage(user: User) -> None:
    rbac.require_level(user, rbac.LOGISTICS, Level.MANAGE)


# ----------------------------------------------------------------- schemas


class ShipmentCreateIn(BaseModel):
    challan_number: str = Field(min_length=1, max_length=64)
    tracking_id: str | None = Field(default=None, max_length=120)
    delivery_partner: str | None = Field(default=None, max_length=120)
    # `status` is a plain str (not the enum) so an invalid value yields a service-raised
    # 400 with a clear message, not pydantic's 422 — per the module contract.
    status: str | None = Field(default=None, max_length=24)
    consignee_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=600)
    phone: str | None = Field(default=None, max_length=40)
    pincode: str | None = Field(default=None, max_length=10)
    dispatched_on: date | None = None
    delivered_on: date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class ShipmentUpdateIn(BaseModel):
    # All optional: a PATCH changes only the supplied fields (exclude_unset). A
    # nullable field can be explicitly cleared by sending null.
    status: str | None = Field(default=None, max_length=24)
    tracking_id: str | None = Field(default=None, max_length=120)
    delivery_partner: str | None = Field(default=None, max_length=120)
    dispatched_on: date | None = None
    delivered_on: date | None = None
    consignee_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=600)
    phone: str | None = Field(default=None, max_length=40)
    pincode: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=1000)


class PodIn(BaseModel):
    pod_file_id: int = Field(gt=0)


class ChallanInfoOut(BaseModel):
    invoice_number: str
    po_number: str
    project_code: str
    consignee_name: str
    consignee_gstin: str
    consignee_address: str


class FileRefOut(BaseModel):
    id: int
    filename: str


class ShipmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    challan_number: str
    challan_id: int | None
    po_id: int | None
    tracking_id: str | None
    delivery_partner: str | None
    status: str
    consignee_name: str | None
    address: str | None
    phone: str | None
    pincode: str | None
    dispatched_on: date | None
    delivered_on: date | None
    pod_file_id: int | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    # The resolved challan's fields (None when challan_number matched no issued challan).
    challan: ChallanInfoOut | None = None
    # Populated on the detail endpoint only.
    pod_file: FileRefOut | None = None


class BulkResultOut(BaseModel):
    created: int
    updated: int
    errors: list[dict[str, Any]]


def _challan_out(challan: service.ResolvedChallan | None) -> ChallanInfoOut | None:
    if challan is None:
        return None
    return ChallanInfoOut(
        invoice_number=challan.invoice_number,
        po_number=challan.po_number,
        project_code=challan.project_code,
        consignee_name=challan.consignee_name,
        consignee_gstin=challan.consignee_gstin,
        consignee_address=challan.consignee_address,
    )


def _shipment_out(
    shipment: Shipment,
    challan: service.ResolvedChallan | None,
    pod_file: StoredFile | None = None,
) -> ShipmentOut:
    out = ShipmentOut.model_validate(shipment)
    out.challan = _challan_out(challan)
    if pod_file is not None:
        out.pod_file = FileRefOut(id=pod_file.id, filename=pod_file.filename)
    return out


def _map_error(exc: service.LogisticsError) -> HTTPException:
    if isinstance(exc, service.ShipmentNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.DuplicateChallanNumber):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, service.InvalidStatus):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


# ------------------------------------------------------------------ create


@router.post("/shipments", response_model=ShipmentOut, status_code=201)
def create_shipment(
    body: ShipmentCreateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ShipmentOut:
    _require_operate(user)
    try:
        shipment = service.create_shipment(
            db, actor_uid=user.firebase_uid, **body.model_dump()
        )
    except service.LogisticsError as exc:
        raise _map_error(exc) from exc
    db.commit()
    db.refresh(shipment)
    return _shipment_out(shipment, service.resolve_challan(db, shipment.challan_id))


@router.post("/shipments/upload", response_model=BulkResultOut)
def upload_shipments(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
) -> BulkResultOut:
    """Bulk-upsert shipments from a partner `.xlsx` dump (UPSERT on challan_number)."""
    _require_operate(user)
    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):
        if len(data) + len(chunk) > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)

    result = service.bulk_upsert_from_excel(
        db, data=bytes(data), actor_uid=user.firebase_uid
    )
    db.commit()
    return BulkResultOut(**result)


# ------------------------------------------------------------------- reads


@router.get("/shipments", response_model=list[ShipmentOut])
def list_shipments(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status", max_length=24)] = None,
    challan_number: Annotated[str | None, Query(max_length=64)] = None,
    partner: Annotated[str | None, Query(max_length=120)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ShipmentOut]:
    _require_view(user)
    try:
        rows = service.list_shipments(
            db,
            status=status_filter,
            challan_number=challan_number,
            delivery_partner=partner,
            q=q,
            limit=limit,
            offset=offset,
        )
    except service.LogisticsError as exc:
        raise _map_error(exc) from exc
    return [_shipment_out(r.shipment, r.challan) for r in rows]


@router.get("/shipments/{shipment_id}", response_model=ShipmentOut)
def get_shipment(
    shipment_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ShipmentOut:
    _require_view(user)
    try:
        row = service.get_shipment_detail(db, shipment_id)
    except service.LogisticsError as exc:
        raise _map_error(exc) from exc
    pod_file = None
    if row.shipment.pod_file_id is not None:
        pod_file = db.execute(
            select(StoredFile).where(StoredFile.id == row.shipment.pod_file_id)
        ).scalar_one_or_none()
    return _shipment_out(row.shipment, row.challan, pod_file)


# ------------------------------------------------------------------ mutate


@router.patch("/shipments/{shipment_id}", response_model=ShipmentOut)
def patch_shipment(
    shipment_id: int,
    body: ShipmentUpdateIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ShipmentOut:
    _require_operate(user)
    shipment = service.get_shipment(db, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "shipment not found")
    try:
        service.update_shipment(
            db,
            shipment=shipment,
            actor_uid=user.firebase_uid,
            **body.model_dump(exclude_unset=True),
        )
    except service.LogisticsError as exc:
        raise _map_error(exc) from exc
    db.commit()
    db.refresh(shipment)
    return _shipment_out(shipment, service.resolve_challan(db, shipment.challan_id))


@router.post("/shipments/{shipment_id}/pod", response_model=ShipmentOut)
def attach_pod(
    shipment_id: int,
    body: PodIn,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ShipmentOut:
    """Attach an already-uploaded POD file (its id from `POST /files/upload`)."""
    _require_operate(user)
    shipment = service.get_shipment(db, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "shipment not found")
    # The referenced file must exist (FK would fail at flush; a clean 404 is friendlier).
    pod_file = db.execute(
        select(StoredFile).where(StoredFile.id == body.pod_file_id)
    ).scalar_one_or_none()
    if pod_file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pod file not found")
    service.set_pod(db, shipment=shipment, pod_file_id=body.pod_file_id,
                    actor_uid=user.firebase_uid)
    db.commit()
    db.refresh(shipment)
    return _shipment_out(
        shipment, service.resolve_challan(db, shipment.challan_id), pod_file
    )


@router.delete("/shipments/{shipment_id}", status_code=204, response_class=Response)
def delete_shipment(
    shipment_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    _require_manage(user)
    shipment = service.get_shipment(db, shipment_id)
    if shipment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "shipment not found")
    service.delete_shipment(db, shipment=shipment, actor_uid=user.firebase_uid)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
