"""Logistics delivery-tracking over the real app wiring.

In-memory sqlite (StaticPool, FKs ON) with the logistics router mounted. Seeds an
issued challan (with a number, po_number, invoice_number) so `challan_number`
resolution + the surfaced invoice#/PO# can be exercised. Covers: manual create
resolves `challan_id` by number; an unresolved number still creates (challan_id
NULL, not dropped); the bulk `.xlsx` UPSERTs on challan_number (a second upload with
the same number UPDATES, doesn't duplicate); a bad-status row errors while the
others land; list surfaces the challan's invoice#/PO#; status update; POD attach;
and the RBAC split (viewer 403 on create, operator 403 on delete).
"""
from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

# Import app.main so every module + table is registered on Base before create_all.
import app.main  # noqa: F401
from app.db import Base, get_db
from app.modules.challan.models import Challan, ChallanBatch
from app.modules.logistics.models import Shipment
from app.modules.logistics.routes import router as logistics_router
from app.modules.numbering.models import NumberingAllocation
from app.platform.auth import current_user
from app.platform.models import AuditLog, Level, Role, RoleModulePermission, User
from app.platform.roles_builtin import ensure_builtin_roles

_CHALLAN_NUMBER = "GIF/DC/26-27/L/000189"


@pytest.fixture
def client() -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn: object, _rec: object) -> None:  # enforce FKs like Postgres
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    operator_role = Role(name="Logi Operator", description="", is_system=False)
    operator_role.module_permissions = [
        RoleModulePermission(module_key="logistics", level=Level.OPERATE)
    ]
    seed.add(operator_role)
    seed.flush()
    seed.add(User(firebase_uid="admin", email="admin@t.local", name="Admin",
                  role_id=roles["Administrator"].id, active=True))
    seed.add(User(firebase_uid="viewer", email="viewer@t.local", name="Viewer",
                  role_id=roles["Viewer"].id, active=True))
    seed.add(User(firebase_uid="operator", email="op@t.local", name="Operator",
                  role_id=operator_role.id, active=True))
    seed.commit()
    seed.close()

    def _db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(logistics_router, prefix="/api/v1/logistics")
    app.dependency_overrides[get_db] = _db
    app.state.TestSession = TestSession
    _as(app, "admin")
    yield TestClient(app)
    engine.dispose()


def _as(app: FastAPI, uid: str) -> None:
    db = app.state.TestSession()
    user = db.execute(
        select(User).options(
            selectinload(User.role).selectinload(Role.module_permissions),
            selectinload(User.role).selectinload(Role.platform_permissions),
        ).where(User.firebase_uid == uid)
    ).scalar_one()
    db.close()
    app.dependency_overrides[current_user] = lambda: user


def _act(client: TestClient, uid: str) -> None:
    _as(client.app, uid)


@pytest.fixture
def issued_challan(client: TestClient) -> int:
    """Seed one ISSUED challan carrying the number, po_number, invoice_number that a
    shipment resolves against. Returns its id."""
    db = client.app.state.TestSession()
    batch = ChallanBatch(status="COMPLETED")
    alloc = NumberingAllocation(
        series="L", fy="26-27", number=189, formatted=_CHALLAN_NUMBER, status="ISSUED")
    db.add_all([batch, alloc])
    db.flush()
    challan = Challan(
        batch_id=batch.id, allocation_id=alloc.id, number=_CHALLAN_NUMBER,
        series="L", fy="26-27", number_int=189, challan_date=date(2026, 8, 1),
        project_code="BRI-001",
        consignor_name="Gifsy", consignor_gstin="27AAAAA0000A1Z5", consignor_state="MH",
        consignee_name="Britannia Depot", consignee_gstin="29BBBBB1111B2Z6",
        consignee_state="KA", consignee_address="12 MG Road, Bengaluru",
        ship_to_name="Britannia Depot", ship_to_address="12 MG Road", ship_to_state="KA",
        po_number="PO-4477", invoice_number="INV-9001",
    )
    db.add(challan)
    db.commit()
    cid = challan.id
    db.close()
    return cid


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(header)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_BULK_HEADER = [
    "challan_number", "tracking_id", "delivery_partner", "status",
    "consignee_name", "address", "phone", "pincode", "dispatched_on",
    "delivered_on", "notes",
]


# ------------------------------------------------------------ manual create


def test_create_resolves_challan_id_by_number(client: TestClient, issued_challan: int) -> None:
    _act(client, "operator")
    r = client.post("/api/v1/logistics/shipments", json={
        "challan_number": _CHALLAN_NUMBER, "delivery_partner": "BlueDart",
        "tracking_id": "BD123",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    # challan_id resolved to the seeded challan, and its invoice#/PO# are surfaced.
    assert body["challan_id"] == issued_challan
    assert body["challan"]["invoice_number"] == "INV-9001"
    assert body["challan"]["po_number"] == "PO-4477"
    assert body["status"] == "PENDING"


def test_unresolved_number_still_creates(client: TestClient) -> None:
    """A challan number that matches nothing is NEVER rejected — the shipment is
    created with challan_id NULL and no resolved challan block."""
    _act(client, "operator")
    r = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/999999", "delivery_partner": "Delhivery",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["challan_id"] is None
    assert body["challan"] is None


def test_create_rejects_bad_status_400(client: TestClient) -> None:
    _act(client, "operator")
    r = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000001", "status": "LOST",
    })
    assert r.status_code == 400, r.text


def test_duplicate_challan_number_409(client: TestClient) -> None:
    _act(client, "operator")
    body = {"challan_number": "GIF/DC/26-27/L/000002"}
    assert client.post("/api/v1/logistics/shipments", json=body).status_code == 201
    dup = client.post("/api/v1/logistics/shipments", json=body)
    assert dup.status_code == 409, dup.text


# ------------------------------------------------------------- bulk upsert


def test_bulk_upsert_updates_not_duplicates(client: TestClient, issued_challan: int) -> None:
    _act(client, "operator")
    data = _xlsx(_BULK_HEADER, [
        [_CHALLAN_NUMBER, "TRK1", "BlueDart", "DISPATCHED",
         "Depot", "12 MG Road", "9990001111", "560001", "2026-08-10", "", "first"],
    ])
    r1 = client.post("/api/v1/logistics/shipments/upload",
                     files={"file": ("dump.xlsx", data, "application/xlsx")})
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"created": 1, "updated": 0, "errors": []}

    # Second upload with the SAME challan_number updates the row (status + tracking),
    # and must NOT create a duplicate.
    data2 = _xlsx(_BULK_HEADER, [
        [_CHALLAN_NUMBER, "TRK2", "BlueDart", "DELIVERED",
         "", "", "", "", "", "2026-08-12", ""],
    ])
    r2 = client.post("/api/v1/logistics/shipments/upload",
                     files={"file": ("dump.xlsx", data2, "application/xlsx")})
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"created": 0, "updated": 1, "errors": []}

    db = client.app.state.TestSession()
    ships = db.execute(select(Shipment).where(
        Shipment.challan_number == _CHALLAN_NUMBER)).scalars().all()
    db.close()
    assert len(ships) == 1  # updated in place, not duplicated
    assert ships[0].status == "DELIVERED"
    assert ships[0].tracking_id == "TRK2"
    # a blank consignee cell on the 2nd upload did NOT wipe the stored value
    assert ships[0].consignee_name == "Depot"
    assert ships[0].delivered_on is not None
    assert ships[0].challan_id == issued_challan  # resolved on insert


def test_bulk_bad_status_row_errors_others_land(client: TestClient) -> None:
    _act(client, "operator")
    data = _xlsx(_BULK_HEADER, [
        ["GIF/DC/26-27/L/000010", "T10", "BlueDart", "IN_TRANSIT",
         "", "", "", "", "", "", ""],
        ["GIF/DC/26-27/L/000011", "T11", "BlueDart", "TELEPORTED",  # bad status
         "", "", "", "", "", "", ""],
        ["", "T12", "BlueDart", "PENDING", "", "", "", "", "", "", ""],  # missing number
    ])
    r = client.post("/api/v1/logistics/shipments/upload",
                    files={"file": ("dump.xlsx", data, "application/xlsx")})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["created"] == 1  # only the valid first row landed
    assert result["updated"] == 0
    rows = {e["row"] for e in result["errors"]}
    assert rows == {3, 4}  # sheet rows: bad status (3) + missing number (4)

    db = client.app.state.TestSession()
    numbers = {s.challan_number for s in db.execute(select(Shipment)).scalars()}
    db.close()
    assert numbers == {"GIF/DC/26-27/L/000010"}


# -------------------------------------------------------------------- list


def test_list_surfaces_challan_invoice_and_po(client: TestClient, issued_challan: int) -> None:
    _act(client, "operator")
    client.post("/api/v1/logistics/shipments", json={
        "challan_number": _CHALLAN_NUMBER, "delivery_partner": "BlueDart"})
    client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000404", "delivery_partner": "Delhivery"})

    _act(client, "viewer")  # reads are module-gated (VIEW)
    rows = client.get("/api/v1/logistics/shipments").json()
    assert len(rows) == 2
    resolved = next(x for x in rows if x["challan_number"] == _CHALLAN_NUMBER)
    assert resolved["challan"]["invoice_number"] == "INV-9001"
    assert resolved["challan"]["po_number"] == "PO-4477"
    unresolved = next(x for x in rows if x["challan_number"] == "GIF/DC/26-27/L/000404")
    assert unresolved["challan"] is None

    # filter by challan_number
    only = client.get(
        f"/api/v1/logistics/shipments?challan_number={_CHALLAN_NUMBER}").json()
    assert [x["challan_number"] for x in only] == [_CHALLAN_NUMBER]
    # filter by partner
    dl = client.get("/api/v1/logistics/shipments?partner=Delhivery").json()
    assert [x["delivery_partner"] for x in dl] == ["Delhivery"]


# ------------------------------------------------------------ update + POD


def test_status_update(client: TestClient) -> None:
    _act(client, "operator")
    sid = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000020"}).json()["id"]
    r = client.patch(f"/api/v1/logistics/shipments/{sid}",
                     json={"status": "DISPATCHED", "tracking_id": "XPB-1"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "DISPATCHED"
    assert r.json()["tracking_id"] == "XPB-1"
    # a bad status on PATCH -> 400
    bad = client.patch(f"/api/v1/logistics/shipments/{sid}", json={"status": "NOPE"})
    assert bad.status_code == 400


def test_pod_attach(client: TestClient) -> None:
    _act(client, "operator")
    sid = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000030"}).json()["id"]

    # A real stored-file row (POD uploaded earlier via /files/upload).
    db = client.app.state.TestSession()
    from app.modules.files.models import StoredFile
    sf = StoredFile(kind="upload", filename="pod.jpg", content_type="image/jpeg",
                    size=10, storage_ref="x/pod.jpg", uploaded_by="operator",
                    module_key="logistics")
    db.add(sf)
    db.commit()
    file_id = sf.id
    db.close()

    r = client.post(f"/api/v1/logistics/shipments/{sid}/pod",
                    json={"pod_file_id": file_id})
    assert r.status_code == 200, r.text
    assert r.json()["pod_file_id"] == file_id
    assert r.json()["pod_file"] == {"id": file_id, "filename": "pod.jpg"}

    # detail endpoint surfaces the pod_file block too
    detail = client.get(f"/api/v1/logistics/shipments/{sid}").json()
    assert detail["pod_file"]["filename"] == "pod.jpg"

    # a missing pod file id -> 404
    miss = client.post(f"/api/v1/logistics/shipments/{sid}/pod",
                       json={"pod_file_id": 999999})
    assert miss.status_code == 404


# -------------------------------------------------------------------- rbac


def test_viewer_cannot_create(client: TestClient) -> None:
    _act(client, "viewer")
    r = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000040"})
    assert r.status_code == 403


def test_operator_cannot_delete(client: TestClient) -> None:
    _act(client, "operator")
    sid = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000050"}).json()["id"]
    # delete requires MANAGE — an operator (OPERATE) is forbidden.
    forbidden = client.delete(f"/api/v1/logistics/shipments/{sid}")
    assert forbidden.status_code == 403
    # an admin can delete.
    _act(client, "admin")
    ok = client.delete(f"/api/v1/logistics/shipments/{sid}")
    assert ok.status_code == 204


# ------------------------------------------------------------------- audit


def test_writes_are_audited(client: TestClient) -> None:
    _act(client, "operator")
    sid = client.post("/api/v1/logistics/shipments", json={
        "challan_number": "GIF/DC/26-27/L/000060"}).json()["id"]
    client.patch(f"/api/v1/logistics/shipments/{sid}", json={"status": "DELIVERED"})
    db = client.app.state.TestSession()
    actions = {a.action for a in db.execute(select(AuditLog)).scalars()}
    db.close()
    assert {"logistics.shipment_created", "logistics.shipment_updated"} <= actions
