"""Logistics service — delivery-tracking shipments keyed on the challan number.

A shipment is a soft join to an issued challan: `challan_number` is the key, and
`challan_id` is RESOLVED to the challan row when that number matches an issued
challan (else left NULL — a partner dump may reference a number we can't yet
resolve, and such a row is NEVER rejected/dropped, just left unresolved).

Design notes:
  * `bulk_upsert_from_excel` UPSERTS on `challan_number`, so a re-uploaded partner
    dump UPDATES the existing row rather than duplicating it (the UNIQUE constraint
    `uq_logistics_shipment_challan_number` is the DB backstop). Every non-blank row
    either creates, updates, or lands in the returned `errors` list — nothing is
    silently dropped (mirrors the challan importer's discipline).
  * On update (manual or bulk) a BLANK cell leaves the stored value unchanged, so a
    partner dump that omits a column can't wipe existing tracking/consignee data.
  * Reads surface the resolved challan's invoice#/PO#/project/consignee by an
    explicit LEFT JOIN (module-boundary rule: no ORM relationship across modules).
  * Every mutation is audited inside the caller's transaction.

⚠️ A `db.begin_nested()` MUST be used as `with db.begin_nested():` — a bare call
leaks a SAVEPOINT per insert.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.challan.models import Challan
from app.modules.challan.parsing import parse_date
from app.modules.logistics.models import DeliveryStatus, Shipment
from app.platform import audit

# ------------------------------------------------------------------- errors


class LogisticsError(Exception):
    """Base for invalid logistics operations."""


class ShipmentNotFound(LogisticsError):
    """A referenced shipment is missing. Route maps to 404."""


class InvalidStatus(LogisticsError):
    """A supplied delivery status is not a DeliveryStatus value. Route maps to 400."""


class DuplicateChallanNumber(LogisticsError):
    """A shipment already exists for this challan number. Route maps to 409."""


# --------------------------------------------------------------- sentinels


class _Unset:
    """Marker for 'argument not supplied' in a partial (PATCH) update, so we can
    tell "leave unchanged" apart from "set to NULL" for a nullable column."""


_UNSET: Final = _Unset()

_STATUSES: frozenset[str] = frozenset(s.value for s in DeliveryStatus)


# ----------------------------------------------------------------- helpers


def _validate_status(value: str) -> str:
    """Normalize + validate a delivery status; raise `InvalidStatus` if unknown."""
    normalized = value.strip().upper()
    if normalized not in _STATUSES:
        raise InvalidStatus(
            f"status must be one of {sorted(_STATUSES)}"
        )
    return normalized


def _opt(value: str | None) -> str | None:
    """Trim an optional free-text field; blank collapses to None."""
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _resolve_challan_id(db: Session, challan_number: str) -> int | None:
    """The id of the issued challan whose `number` equals `challan_number`, else None.

    `.first()` (not `scalar_one`) is deliberate: a number that matches nothing (a
    partner dump referencing a not-yet-issued challan) resolves to None rather than
    raising, so the shipment is still created with `challan_id` NULL.
    """
    return db.execute(
        select(Challan.id).where(Challan.number == challan_number).limit(1)
    ).scalars().first()


@dataclass(frozen=True)
class ResolvedChallan:
    """The issued challan's fields surfaced on a shipment (via join, not FK relationship)."""

    invoice_number: str
    po_number: str
    project_code: str
    consignee_name: str
    consignee_gstin: str
    consignee_address: str


@dataclass(frozen=True)
class ShipmentRow:
    """A shipment plus its resolved challan info (None when `challan_id` is unresolved)."""

    shipment: Shipment
    challan: ResolvedChallan | None


def resolve_challan(db: Session, challan_id: int | None) -> ResolvedChallan | None:
    """Load the resolved challan's display fields for `challan_id`, or None."""
    if challan_id is None:
        return None
    row = db.execute(
        select(
            Challan.invoice_number,
            Challan.po_number,
            Challan.project_code,
            Challan.consignee_name,
            Challan.consignee_gstin,
            Challan.consignee_address,
        ).where(Challan.id == challan_id)
    ).first()
    if row is None:
        return None
    return ResolvedChallan(
        invoice_number=row[0],
        po_number=row[1],
        project_code=row[2],
        consignee_name=row[3],
        consignee_gstin=row[4],
        consignee_address=row[5],
    )


# ------------------------------------------------------------------- create


def create_shipment(
    db: Session,
    *,
    challan_number: str,
    tracking_id: str | None = None,
    delivery_partner: str | None = None,
    status: str | None = None,
    consignee_name: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    pincode: str | None = None,
    dispatched_on: date | None = None,
    delivered_on: date | None = None,
    notes: str | None = None,
    actor_uid: str | None = None,
) -> Shipment:
    """Create one shipment manually.

    `challan_id` is resolved by matching `challan.number == challan_number`; an
    unresolved number is NOT rejected — the row is created with `challan_id` NULL.
    A duplicate `challan_number` raises `DuplicateChallanNumber` (route -> 409).
    Audited.
    """
    number = challan_number.strip()
    if not number:
        raise LogisticsError("challan_number is required")
    status_value = _validate_status(status) if status else DeliveryStatus.PENDING.value

    shipment = Shipment(
        challan_number=number,
        challan_id=_resolve_challan_id(db, number),
        tracking_id=_opt(tracking_id),
        delivery_partner=_opt(delivery_partner),
        status=status_value,
        consignee_name=_opt(consignee_name),
        address=_opt(address),
        phone=_opt(phone),
        pincode=_opt(pincode),
        dispatched_on=dispatched_on,
        delivered_on=delivered_on,
        notes=_opt(notes),
        created_by=actor_uid,
    )
    try:
        with db.begin_nested():
            db.add(shipment)
            db.flush()
    except IntegrityError as exc:
        raise DuplicateChallanNumber(
            f"a shipment already exists for challan {number}"
        ) from exc

    audit.log(
        db,
        action="logistics.shipment_created",
        actor_uid=actor_uid,
        entity="logistics_shipment",
        entity_id=str(shipment.id),
        detail={"challan_number": number, "resolved": shipment.challan_id is not None},
    )
    return shipment


# ---------------------------------------------------------------- bulk excel

# Canonical column keys + the friendly header aliases the importer accepts. Headers
# are matched case-insensitively with spaces/underscores collapsed.
_COLUMN_ALIASES: dict[str, str] = {
    "challan_number": "challan_number",
    "challan_no": "challan_number",
    "challan": "challan_number",
    "tracking_id": "tracking_id",
    "tracking_no": "tracking_id",
    "awb": "tracking_id",
    "delivery_partner": "delivery_partner",
    "partner": "delivery_partner",
    "courier": "delivery_partner",
    "status": "status",
    "consignee_name": "consignee_name",
    "consignee": "consignee_name",
    "address": "address",
    "phone": "phone",
    "mobile": "phone",
    "pincode": "pincode",
    "pin": "pincode",
    "dispatched_on": "dispatched_on",
    "dispatch_date": "dispatched_on",
    "delivered_on": "delivered_on",
    "delivery_date": "delivered_on",
    "notes": "notes",
    "remarks": "notes",
}

_REQUIRED_COLUMN = "challan_number"


def _norm_header(raw: str) -> str:
    """Lower + collapse whitespace/underscores so 'Challan Number' == 'challan_number'."""
    return "_".join(raw.strip().lower().replace("_", " ").split())


def _cell_to_str(value: object) -> str:
    """Coerce a raw openpyxl cell value to a trimmed string ('' for blank)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, date):  # date/datetime -> ISO (parse_date reads it back)
        return value.isoformat()
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


@dataclass
class _RowError:
    row: int
    message: str


def bulk_upsert_from_excel(
    db: Session, *, data: bytes, actor_uid: str | None = None
) -> dict[str, Any]:
    """Parse a partner `.xlsx` dump and UPSERT each row on `challan_number`.

    Columns (case-insensitive headers, friendly aliases accepted): challan_number
    (required), tracking_id, delivery_partner, status, consignee_name, address,
    phone, pincode, dispatched_on, delivered_on, notes.

    Behaviour:
      * UPSERT on `challan_number` — a row whose number already has a shipment
        UPDATES it (re-uploaded dump), else a new shipment is created. `challan_id`
        is (re)resolved on both paths.
      * On UPDATE, a BLANK cell leaves the stored value unchanged (a dump that omits
        a column never wipes existing data).
      * A malformed row (missing challan_number, bad status, unparseable date) is
        reported in `errors` and skipped — every OTHER row still lands. No non-blank
        row is silently dropped.

    Returns `{"created": int, "updated": int, "errors": [{"row", "message"}, ...]}`.
    Audited (one summary row).
    """
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - openpyxl raises many types on a bad file
        return {"created": 0, "updated": 0,
                "errors": [{"row": 1, "message": "could not read the file as an .xlsx workbook"}]}

    errors: list[_RowError] = []
    created = 0
    updated = 0
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)

        col_index: dict[str, int] = {}
        for idx, raw in enumerate(header or ()):
            if raw is None:
                continue
            key = _COLUMN_ALIASES.get(_norm_header(str(raw)))
            if key is not None and key not in col_index:
                col_index[key] = idx

        if _REQUIRED_COLUMN not in col_index:
            return {"created": 0, "updated": 0, "errors": [
                {"row": 1, "message": f"required column '{_REQUIRED_COLUMN}' is missing"}]}

        for sheet_row, values in enumerate(rows_iter, start=2):
            cells = {
                key: _cell_to_str(values[idx] if idx < len(values) else None)
                for key, idx in col_index.items()
            }
            if all(v == "" for v in cells.values()):
                continue  # skip a fully-blank row

            outcome = _upsert_row(db, sheet_row, cells, errors, actor_uid)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
    finally:
        wb.close()

    audit.log(
        db,
        action="logistics.bulk_upsert",
        actor_uid=actor_uid,
        entity="logistics_shipment",
        entity_id=None,
        detail={"created": created, "updated": updated, "error_count": len(errors)},
    )
    return {
        "created": created,
        "updated": updated,
        "errors": [{"row": e.row, "message": e.message} for e in errors],
    }


def _parse_row_dates(
    row: int, cells: dict[str, str], errors: list[_RowError]
) -> tuple[date | None, date | None, bool]:
    """Parse dispatched_on/delivered_on; append a row error for any unparseable value.

    Returns (dispatched_on, delivered_on, ok) — `ok` is False if any date was present
    but unparseable, so the caller skips the row.
    """
    ok = True
    parsed: list[date | None] = []
    for col in ("dispatched_on", "delivered_on"):
        text = cells.get(col, "").strip()
        if not text:
            parsed.append(None)
            continue
        value = parse_date(text)
        if value is None:
            errors.append(_RowError(row, f"{col} is not a recognisable date"))
            ok = False
        parsed.append(value)
    return parsed[0], parsed[1], ok


def _existing_by_number(db: Session, number: str) -> Shipment | None:
    """The shipment currently stored for `challan_number`, or None (bulk-upsert lookup)."""
    return db.execute(
        select(Shipment).where(Shipment.challan_number == number)
    ).scalar_one_or_none()


def _upsert_row(
    db: Session,
    row: int,
    cells: dict[str, str],
    errors: list[_RowError],
    actor_uid: str | None,
) -> str | None:
    """Validate + upsert one parsed row. Returns 'created' / 'updated' / None (error)."""
    number = cells.get("challan_number", "").strip()
    if not number:
        errors.append(_RowError(row, "challan_number is required"))
        return None

    status_text = cells.get("status", "").strip()
    status_value: str | None = None
    if status_text:
        try:
            status_value = _validate_status(status_text)
        except InvalidStatus as exc:
            errors.append(_RowError(row, str(exc)))
            return None

    dispatched_on, delivered_on, dates_ok = _parse_row_dates(row, cells, errors)
    if not dates_ok:
        return None

    existing = _existing_by_number(db, number)

    # Only NON-BLANK cells overwrite; a blank cell leaves the stored value alone.
    def present(col: str) -> str | None:
        return _opt(cells.get(col))

    if existing is None:
        shipment = Shipment(
            challan_number=number,
            challan_id=_resolve_challan_id(db, number),
            tracking_id=present("tracking_id"),
            delivery_partner=present("delivery_partner"),
            status=status_value or DeliveryStatus.PENDING.value,
            consignee_name=present("consignee_name"),
            address=present("address"),
            phone=present("phone"),
            pincode=present("pincode"),
            dispatched_on=dispatched_on,
            delivered_on=delivered_on,
            notes=present("notes"),
            created_by=actor_uid,
        )
        try:
            # SAVEPOINT (mirrors create_shipment): a concurrent insert of the SAME
            # challan_number (two overlapping dumps, or an upload racing a manual
            # create) trips uq_logistics_shipment_challan_number — the savepoint
            # contains that failure so the whole batch's transaction isn't poisoned.
            with db.begin_nested():
                db.add(shipment)
                db.flush()  # materialise so a later dup number in the SAME file updates it
            return "created"
        except IntegrityError:
            # Lost the race: another writer inserted this number first. The savepoint
            # rolled the failed insert back — re-SELECT the now-existing row and fold
            # this row into the UPDATE path instead of 500-ing + losing the batch.
            existing = _existing_by_number(db, number)
            if existing is None:
                raise  # not the duplicate we assumed — surface the real error
            if shipment in db:
                db.expunge(shipment)  # drop the never-persisted transient

    # Update path — set only supplied (non-blank) fields.
    if status_value is not None:
        existing.status = status_value
    for col, attr in (
        ("tracking_id", "tracking_id"),
        ("delivery_partner", "delivery_partner"),
        ("consignee_name", "consignee_name"),
        ("address", "address"),
        ("phone", "phone"),
        ("pincode", "pincode"),
        ("notes", "notes"),
    ):
        value = present(col)
        if value is not None:
            setattr(existing, attr, value)
    if dispatched_on is not None:
        existing.dispatched_on = dispatched_on
    if delivered_on is not None:
        existing.delivered_on = delivered_on
    # Re-resolve the challan link if it was previously unresolved.
    if existing.challan_id is None:
        existing.challan_id = _resolve_challan_id(db, number)
    db.flush()
    return "updated"


# ------------------------------------------------------------------- update


def update_shipment(
    db: Session,
    *,
    shipment: Shipment,
    status: str | _Unset = _UNSET,
    tracking_id: str | None | _Unset = _UNSET,
    delivery_partner: str | None | _Unset = _UNSET,
    dispatched_on: date | None | _Unset = _UNSET,
    delivered_on: date | None | _Unset = _UNSET,
    consignee_name: str | None | _Unset = _UNSET,
    address: str | None | _Unset = _UNSET,
    phone: str | None | _Unset = _UNSET,
    pincode: str | None | _Unset = _UNSET,
    notes: str | None | _Unset = _UNSET,
    actor_uid: str | None = None,
) -> Shipment:
    """Patch a shipment's tracking / status / consignee / dates / notes.

    Only supplied fields change; a nullable field can be explicitly cleared by
    passing `None`. `status` is validated against DeliveryStatus (raises
    `InvalidStatus` -> 400). Audited.
    """
    changed: dict[str, object | None] = {}
    if not isinstance(status, _Unset):
        shipment.status = _validate_status(status)
        changed["status"] = shipment.status
    if not isinstance(tracking_id, _Unset):
        shipment.tracking_id = _opt(tracking_id)
        changed["tracking_id"] = shipment.tracking_id
    if not isinstance(delivery_partner, _Unset):
        shipment.delivery_partner = _opt(delivery_partner)
        changed["delivery_partner"] = shipment.delivery_partner
    if not isinstance(dispatched_on, _Unset):
        shipment.dispatched_on = dispatched_on
        changed["dispatched_on"] = str(dispatched_on) if dispatched_on else None
    if not isinstance(delivered_on, _Unset):
        shipment.delivered_on = delivered_on
        changed["delivered_on"] = str(delivered_on) if delivered_on else None
    if not isinstance(consignee_name, _Unset):
        shipment.consignee_name = _opt(consignee_name)
        changed["consignee_name"] = shipment.consignee_name
    if not isinstance(address, _Unset):
        shipment.address = _opt(address)
        changed["address"] = shipment.address
    if not isinstance(phone, _Unset):
        shipment.phone = _opt(phone)
        changed["phone"] = shipment.phone
    if not isinstance(pincode, _Unset):
        shipment.pincode = _opt(pincode)
        changed["pincode"] = shipment.pincode
    if not isinstance(notes, _Unset):
        shipment.notes = _opt(notes)
        changed["notes"] = shipment.notes

    db.flush()
    audit.log(
        db,
        action="logistics.shipment_updated",
        actor_uid=actor_uid,
        entity="logistics_shipment",
        entity_id=str(shipment.id),
        detail={"changed": changed},
    )
    return shipment


def set_pod(
    db: Session, *, shipment: Shipment, pod_file_id: int, actor_uid: str | None = None
) -> Shipment:
    """Attach a proof-of-delivery file (an already-uploaded `files_stored_file` id). Audited."""
    shipment.pod_file_id = pod_file_id
    db.flush()
    audit.log(
        db,
        action="logistics.pod_attached",
        actor_uid=actor_uid,
        entity="logistics_shipment",
        entity_id=str(shipment.id),
        detail={"pod_file_id": pod_file_id},
    )
    return shipment


def delete_shipment(
    db: Session, *, shipment: Shipment, actor_uid: str | None = None
) -> None:
    """Hard-delete a shipment (tracking data, not a statutory document). Audited."""
    shipment_id = shipment.id
    challan_number = shipment.challan_number
    db.delete(shipment)
    db.flush()
    audit.log(
        db,
        action="logistics.shipment_deleted",
        actor_uid=actor_uid,
        entity="logistics_shipment",
        entity_id=str(shipment_id),
        detail={"challan_number": challan_number},
    )


# -------------------------------------------------------------------- reads


def _escape_like(term: str) -> str:
    r"""Escape LIKE wildcards so a user-typed `%`/`_` matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_shipment(db: Session, shipment_id: int) -> Shipment | None:
    """Return a shipment by id (no join), or None."""
    return db.execute(
        select(Shipment).where(Shipment.id == shipment_id)
    ).scalar_one_or_none()


def get_shipment_detail(db: Session, shipment_id: int) -> ShipmentRow:
    """Return a shipment + its resolved challan info; raise `ShipmentNotFound` if missing."""
    shipment = get_shipment(db, shipment_id)
    if shipment is None:
        raise ShipmentNotFound(f"shipment {shipment_id} not found")
    return ShipmentRow(shipment=shipment, challan=resolve_challan(db, shipment.challan_id))


def list_shipments(
    db: Session,
    *,
    status: str | None = None,
    challan_number: str | None = None,
    delivery_partner: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ShipmentRow]:
    """List shipments (newest first) with each row's resolved challan info surfaced.

    Filters: `status` (exact), `challan_number` (exact), `delivery_partner` (exact),
    and `q` (case-insensitive contains across challan number / tracking id /
    consignee name / partner). The resolved challan's invoice#/PO#/project/consignee
    come from a LEFT JOIN, so an unresolved shipment still lists (challan = None).
    """
    stmt = (
        select(
            Shipment,
            Challan.invoice_number,
            Challan.po_number,
            Challan.project_code,
            Challan.consignee_name,
            Challan.consignee_gstin,
            Challan.consignee_address,
        )
        .outerjoin(Challan, Shipment.challan_id == Challan.id)
        .order_by(Shipment.id.desc())
    )
    if status:
        stmt = stmt.where(Shipment.status == _validate_status(status))
    if challan_number:
        stmt = stmt.where(Shipment.challan_number == challan_number.strip())
    if delivery_partner:
        stmt = stmt.where(Shipment.delivery_partner == delivery_partner.strip())
    if q:
        like = f"%{_escape_like(q.strip())}%"
        stmt = stmt.where(
            Shipment.challan_number.ilike(like, escape="\\")
            | Shipment.tracking_id.ilike(like, escape="\\")
            | Shipment.consignee_name.ilike(like, escape="\\")
            | Shipment.delivery_partner.ilike(like, escape="\\")
        )
    stmt = stmt.limit(limit).offset(offset)

    rows: list[ShipmentRow] = []
    for record in db.execute(stmt):
        shipment = record[0]
        challan = (
            None
            if shipment.challan_id is None
            else ResolvedChallan(
                invoice_number=record[1],
                po_number=record[2],
                project_code=record[3],
                consignee_name=record[4],
                consignee_gstin=record[5],
                consignee_address=record[6],
            )
        )
        rows.append(ShipmentRow(shipment=shipment, challan=challan))
    return rows
