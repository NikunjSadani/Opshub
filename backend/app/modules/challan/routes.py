"""Challan generator operator surface (mounted at `/api/v1`).

  POST /challan/batches                 -> upload Excel + validate (module-gated)
  POST /challan/batches/{id}/generate   -> reserve+render+issue in background
  GET  /challan/batches | /batches/{id} -> batch status + artifact file ids
  GET  /challan/challans                -> the challan register (filter series/fy/status)
  POST /challan/challans/{id}/void      -> ADMIN void a challan + its number

Artifacts (error report / PDFs / ZIP / merged) are StoredFiles tagged with the
document_automation module, so they download via `/api/v1/files/{id}/download`
under that module's access gate. Uploads/reads need the module grant; void is
Admin-only (`challan.void`).
"""
from __future__ import annotations

import hmac
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.challan import download, render, service, template
from app.modules.challan.models import BatchStatus, Challan, ChallanBatch, ChallanStatus
from app.modules.files.models import StoredFile
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.rbac import can
from app.platform.storage import get_storage

router = APIRouter()

MODULE_KEY = "document_automation"
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Hard ceiling on a single CSV export so the response can never be unbounded.
MAX_CSV_ROWS = 50000
_CSV_HEADER = (
    "Number,Date,Project ID,Consignee Name,Ship-to State,E-way,Total (INR),Status"
)


def _sanitize_filename(name: str) -> str:
    return _CONTROL.sub("", name).strip()[:512] or "upload.xlsx"


def _require_module(user: User) -> None:
    rbac.require_module(user, MODULE_KEY)


# ------------------------------------------------------------------- schemas

class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    challan_count: int
    line_count: int
    message: str | None
    error_report_file_id: int | None
    zip_file_id: int | None
    merged_pdf_file_id: int | None


class GenerateBody(BaseModel):
    series: str = Field(default="L", min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]{1,8}$")


class ChallanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    number: str
    series: str
    fy: str
    challan_date: Any
    project_code: str
    consignee_name: str
    ship_to_state: str
    eway_required: bool
    total_paise: int | None  # None for a value-free challan
    status: str
    pdf_file_id: int | None


class SeriesBreakdownOut(BaseModel):
    series: str
    fy: str
    issued: int
    void: int
    total_value_paise: int


class ChallanSummaryOut(BaseModel):
    issued_count: int
    void_count: int
    eway_count: int
    valued_count: int
    total_value_paise: int
    by_series: list[SeriesBreakdownOut]


class VoidBody(BaseModel):
    reason: str = Field(min_length=1, max_length=300)


class StuckSweepBody(BaseModel):
    # Conservative floor: a batch is only auto-reset after this long WITHOUT a progress
    # heartbeat (see service._STUCK_AFTER / the render-loop heartbeat), so a live-but-
    # slow render is never swept. 60 min default; never below 15.
    older_than_minutes: int = Field(default=60, ge=15, le=1440)


class StuckSweepOut(BaseModel):
    reset: int


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    gstin: str
    consignee_name: str
    field: str
    stored_value: str
    uploaded_value: str
    choice: str


class DecisionItem(BaseModel):
    id: int
    choice: str = Field(pattern=r"^(UPDATE_MASTER|THIS_UPLOAD|REJECT)$")


class DecisionsBody(BaseModel):
    decisions: list[DecisionItem] = Field(min_length=1, max_length=5000)


_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/challan/template.xlsx")
def download_template(
    user: Annotated[User, Depends(current_user)],
) -> Response:
    """The self-documenting upload template (.xlsx): a filled 'Challans' sheet +
    an 'Instructions' sheet (per-column reference + Dos & Don'ts)."""
    _require_module(user)
    return Response(
        content=template.build_template_xlsx(),
        media_type=_XLSX_MEDIA,
        headers={
            "Content-Disposition": 'attachment; filename="challan-upload-template.xlsx"'
        },
    )


# ------------------------------------------------------------- upload/validate

@router.post("/challan/batches", response_model=BatchOut, status_code=201)
def upload_batch(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File()],
) -> ChallanBatch:
    """Upload an Excel workbook and validate it synchronously (no rendering)."""
    rbac.require_level(user, rbac.CHALLAN, Level.OPERATE)
    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):  # cap BEFORE appending, so peak
        if len(data) + len(chunk) > max_bytes:      # buffered bytes never exceed the cap
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
        data.extend(chunk)

    filename = _sanitize_filename(file.filename or "upload.xlsx")
    # Storage key is a fresh uuid — the client filename never enters the path.
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix
    ref = get_storage().save(f"challan-source/{uuid4().hex}{suffix}", bytes(data))
    source = StoredFile(
        kind="challan-source",
        filename=filename,
        content_type=file.content_type,
        size=len(data),
        storage_ref=ref,
        uploaded_by=user.firebase_uid,
        module_key=MODULE_KEY,
    )
    db.add(source)
    db.flush()

    batch = service.new_batch(db, source_file_id=source.id, actor_uid=user.firebase_uid)
    service.validate_batch(db, batch, actor_uid=user.firebase_uid)
    db.refresh(batch)
    return batch


# ------------------------------------------------------------------ generate

def _generate_worker(batch_id: int, series: str, actor_uid: str | None) -> None:
    """Background entrypoint: opens its own session, renders + issues the batch."""
    from app.db import SessionLocal  # local import to avoid request-scope coupling

    db = SessionLocal()
    try:
        batch = db.get(ChallanBatch, batch_id)
        if batch is not None:
            service.generate(db, batch, render.get_renderer(),
                             series=series, actor_uid=actor_uid)
    finally:
        db.close()


@router.post("/challan/batches/{batch_id}/generate", response_model=BatchOut)
def generate_batch(
    batch_id: int,
    body: GenerateBody,
    background: BackgroundTasks,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    """Kick off reserve->render->issue in the background for a VALIDATED batch."""
    rbac.require_level(user, rbac.CHALLAN, Level.OPERATE)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    # Atomically claim the batch: only a VALIDATED/FAILED row flips to GENERATING,
    # and only ONE of two overlapping requests wins the UPDATE — so we never
    # schedule two workers that race the batch's final status. The loser gets 409.
    claimed = cast(
        "CursorResult[Any]",
        db.execute(
            update(ChallanBatch)
            .where(
                ChallanBatch.id == batch_id,
                ChallanBatch.status.in_(
                    [BatchStatus.VALIDATED.value, BatchStatus.FAILED.value]
                ),
            )
            .values(status=BatchStatus.GENERATING.value)
            .execution_options(synchronize_session=False)
        ),
    ).rowcount
    db.commit()
    db.refresh(batch)
    if not claimed:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"batch is {batch.status}, expected VALIDATED or FAILED (retry)",
        )
    background.add_task(_generate_worker, batch.id, body.series, user.firebase_uid)
    return batch


# --------------------------------------------------------------------- reads

@router.get("/challan/batches/{batch_id}/decisions", response_model=list[DecisionOut])
def list_batch_decisions(
    batch_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> list[Any]:
    """The consignee contradictions to resolve for a NEEDS_REVIEW batch."""
    _require_module(user)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    return list(service.list_decisions(db, batch))


@router.patch("/challan/batches/{batch_id}/decisions", response_model=BatchOut)
def submit_batch_decisions(
    batch_id: int,
    body: DecisionsBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    """Record per-field review choices. When none remain PENDING the batch becomes
    VALIDATED (ready to generate)."""
    rbac.require_level(user, rbac.CHALLAN, Level.OPERATE)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    ids = [item.id for item in body.decisions]
    if len(ids) != len(set(ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "duplicate decision id")
    # UPDATE_MASTER permanently rewrites the SHARED consignee golden record, so it is
    # held to the same ADMIN gate as the direct master-edit route; THIS_UPLOAD / REJECT
    # (which never mutate the master) stay open to any module user.
    if any(item.choice == "UPDATE_MASTER" for item in body.decisions) and not can(
        user, "masterdata.edit"
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "updating the saved consignee record requires admin; choose 'This upload "
            "only' or 'Reject', or ask an admin to update the master")
    choices = {item.id: item.choice for item in body.decisions}
    try:
        service.submit_decisions(
            db, batch, choices, actor_uid=user.firebase_uid,
            allow_update_master=can(user, "masterdata.edit"),
        )
    except service.ChallanError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.refresh(batch)
    return batch


@router.post("/challan/batches/{batch_id}/recover", response_model=BatchOut)
def recover_batch(
    batch_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    """Reset a batch STUCK in GENERATING (its worker died — crash/OOM/deploy) back to
    a retryable FAILED state, so it can be finished via retry WITHOUT re-uploading
    (which would mint duplicate statutory numbers). ADMIN only; issued challans are
    kept and reserved numbers resume idempotently on the retry."""
    if not can(user, "challan.recover"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "challan.recover requires ADMIN")
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    try:
        service.recover_stuck_batch(db, batch, actor_uid=user.firebase_uid)
    except service.ChallanError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.refresh(batch)
    return batch


@router.post("/challan/batches/sweep-stuck", response_model=StuckSweepOut)
def sweep_stuck_batches(
    db: Annotated[Session, Depends(get_db)],
    body: StuckSweepBody | None = None,
    x_sweep_secret: Annotated[str | None, Header()] = None,
) -> StuckSweepOut:
    """Reset batches wedged in GENERATING past the cutoff back to FAILED (retryable).

    The unattended counterpart of the recover endpoint — for a scheduler, NOT a user.
    Authenticated by the same `X-Sweep-Secret` shared secret as the numbering sweep;
    fail-closed (503) when no secret is configured. The service audits each reset."""
    expected = get_settings().sweep_secret
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "sweep disabled")
    if x_sweep_secret is None or not hmac.compare_digest(x_sweep_secret, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid sweep secret")
    body = body or StuckSweepBody()
    ids = service.sweep_stuck_batches(
        db, older_than=timedelta(minutes=body.older_than_minutes)
    )
    db.commit()
    return StuckSweepOut(reset=len(ids))


@router.get("/challan/batches", response_model=list[BatchOut])
def list_batches(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ChallanBatch]:
    _require_module(user)
    return list(db.execute(
        select(ChallanBatch).order_by(ChallanBatch.id.desc()).limit(limit)
    ).scalars())


@router.get("/challan/batches/{batch_id}", response_model=BatchOut)
def get_batch(
    batch_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ChallanBatch:
    _require_module(user)
    batch = db.get(ChallanBatch, batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "batch not found")
    return batch


def _register_query(
    series: str | None,
    fy: str | None,
    status_filter: ChallanStatus | None,
    date_from: date | None,
    date_to: date | None,
) -> Select[tuple[Challan]]:
    """The filtered, newest-first register select shared by the list + CSV routes."""
    stmt = select(Challan)
    if series:
        stmt = stmt.where(Challan.series == series.strip().upper())
    if fy:
        stmt = stmt.where(Challan.fy == fy)
    if status_filter is not None:
        stmt = stmt.where(Challan.status == status_filter.value)
    if date_from is not None:
        stmt = stmt.where(Challan.challan_date >= date_from)
    if date_to is not None:  # inclusive upper bound
        stmt = stmt.where(Challan.challan_date <= date_to)
    return stmt.order_by(Challan.id.desc())


@router.get("/challan/challans", response_model=list[ChallanOut])
def list_challans(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str | None, Query(max_length=8)] = None,
    fy: Annotated[str | None, Query(max_length=7)] = None,
    status_filter: Annotated[ChallanStatus | None, Query(alias="status")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Challan]:
    _require_module(user)
    stmt = _register_query(series, fy, status_filter, date_from, date_to)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.execute(stmt).scalars())


def _csv_total(total_paise: int | None) -> str:
    """Rupees as a plain 2dp decimal (`1425.06`) so a spreadsheet can sum it;
    empty string for a value-free challan (`total_paise is None`)."""
    if total_paise is None:
        return ""
    return f"{Decimal(total_paise) / 100:.2f}"


@router.get("/challan/challans.csv")
def export_challans_csv(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str | None, Query(max_length=8)] = None,
    fy: Annotated[str | None, Query(max_length=7)] = None,
    status_filter: Annotated[ChallanStatus | None, Query(alias="status")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> Response:
    """Export the filtered register (newest-first, same filters/order as the list)
    as CSV. Text-derived fields are CSV-injection-guarded via `service._csv_field`.

    Capped at MAX_CSV_ROWS: a filtered set larger than the cap returns the newest
    MAX_CSV_ROWS rows and is NEVER silent about it — a trailing marker row and an
    `X-Truncated: true` header both signal the cut, so the file can't be mistaken
    for the complete register.
    """
    _require_module(user)
    # Fetch one past the cap so we can DETECT (not silently swallow) an over-cap set.
    stmt = _register_query(series, fy, status_filter, date_from, date_to).limit(MAX_CSV_ROWS + 1)
    rows = list(db.execute(stmt).scalars())
    truncated = len(rows) > MAX_CSV_ROWS
    rows = rows[:MAX_CSV_ROWS]
    lines = [_CSV_HEADER]
    for ch in rows:
        lines.append(",".join((
            service._csv_field(ch.number),
            f'"{ch.challan_date.isoformat()}"',
            service._csv_field(ch.project_code),
            service._csv_field(ch.consignee_name),
            service._csv_field(ch.ship_to_state),
            f'"{"Yes" if ch.eway_required else "No"}"',
            f'"{_csv_total(ch.total_paise)}"',
            f'"{ch.status}"',
        )))
    if truncated:
        marker = service._csv_field(
            f"truncated at {MAX_CSV_ROWS} rows — narrow the filters for the full register"
        )
        lines.append(",".join([marker, *['""'] * 7]))
    body = ("\n".join(lines) + "\n").encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="challan-register.csv"',
            "X-Truncated": "true" if truncated else "false",
        },
    )


# ------------------------------------------------------- range/list download

class ResolvedChallanOut(BaseModel):
    number_int: int
    number: str
    id: int


class DownloadPreviewOut(BaseModel):
    series: str
    fy: str
    count: int                         # ISSUED challans WITH a stored PDF — what will download
    resolved: list[ResolvedChallanOut]
    skipped_void: list[int]            # VOID numbers being skipped (shown to the operator)
    no_pdf: list[int]                  # ISSUED but not-yet-rendered — can't download, reported
    missing: list[int]                 # requested numbers with no challan in this series/FY
    errors: list[str]


# Statutory series/FY codes are short (see the sibling register/CSV routes); cap them so a
# multi-KB query value can't reach the header path or burn CPU in the spec regex.
_SERIES_Q = Query(max_length=8)
_FY_Q = Query(max_length=7)
_SPEC_Q = Query(max_length=20_000)


@router.get("/challan/download/preview", response_model=DownloadPreviewOut)
def download_preview(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str, _SERIES_Q],
    fy: Annotated[str, _FY_Q],
    spec: Annotated[str, _SPEC_Q],
) -> DownloadPreviewOut:
    """Resolve a (series, FY, range/list) selection WITHOUT downloading — so the operator
    sees how many issued challans they'll get, which VOID numbers are skipped, and which
    ISSUED numbers have no stored PDF yet (so the count never over-promises)."""
    _require_module(user)
    series, fy = series.strip().upper(), fy.strip()
    result = download.resolve(db, series, fy, spec)
    return DownloadPreviewOut(
        series=series, fy=fy, count=len(result.resolved),
        resolved=[ResolvedChallanOut(number_int=c.number_int, number=c.number, id=c.id)
                  for c in result.resolved],
        skipped_void=result.skipped_void, no_pdf=result.no_pdf,
        missing=result.missing, errors=result.errors)


@router.get("/challan/download")
def download_challans(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    series: Annotated[str, _SERIES_Q],
    fy: Annotated[str, _FY_Q],
    spec: Annotated[str, _SPEC_Q],
    mode: Literal["separate", "merged"] = "separate",
) -> Response:
    """Download the ISSUED challans in a (series, FY, range/list) selection — a ZIP of the
    individual full-A4 PDFs (`separate`) or a paper-saving 2-up merged PDF (`merged`, 2
    challans per A4).

    Nothing is ever skipped silently. VOID numbers ride in `X-Skipped-Void`; any challan
    that resolved but whose PDF can't be served right now — never rendered, or its blob is
    gone (e.g. after an ephemeral-disk redeploy) — is collected and reported in
    `X-Skipped-Unavailable` rather than being dropped without a trace or 500-ing the whole
    batch. The operator always gets every deliverable PDF plus an honest account of the rest.
    """
    _require_module(user)
    series, fy = series.strip().upper(), fy.strip()
    result = download.resolve(db, series, fy, spec)
    if result.errors:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(result.errors))
    if not result.resolved:
        detail = "no downloadable challans match that selection"
        extra = []
        if result.skipped_void:
            extra.append(f"{len(result.skipped_void)} voided")
        if result.no_pdf:
            extra.append(f"{len(result.no_pdf)} with no stored PDF")
        if extra:
            detail += f" ({', '.join(extra)}, skipped)"
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail)

    storage = get_storage()
    named: list[tuple[str, bytes]] = []
    # ISSUED-without-a-PDF is already split out by resolve() into no_pdf; here we also guard
    # the download-time races: the StoredFile row vanished, or the blob is gone on disk. Such
    # a challan is reported (unavailable), never silently omitted and never fatal to the batch.
    unavailable: list[int] = list(result.no_pdf)
    for c in result.resolved:
        sf = db.get(StoredFile, c.pdf_file_id) if c.pdf_file_id is not None else None
        if sf is None:
            unavailable.append(c.number_int)
            continue
        try:
            data = storage.open(sf.storage_ref).read()
        except FileNotFoundError:
            unavailable.append(c.number_int)
            continue
        named.append((f"{service._safe(c.number)}.pdf", data))
    if not named:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "the selected challans have no downloadable stored PDF")

    headers = {
        "X-Skipped-Void": ",".join(str(n) for n in result.skipped_void),
        "X-Skipped-Unavailable": ",".join(str(n) for n in sorted(unavailable)),
    }
    stamp = f"{series}-{fy}"
    if mode == "merged":
        headers["Content-Disposition"] = f'attachment; filename="challans-{stamp}-2up.pdf"'
        return Response(content=render.merge_2up([data for _, data in named]),
                        media_type="application/pdf", headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="challans-{stamp}.zip"'
    return Response(content=render.zip_files(named), media_type="application/zip",
                    headers=headers)


@router.get("/challan/summary", response_model=ChallanSummaryOut)
def challan_summary(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    fy: Annotated[str | None, Query(max_length=7)] = None,
    series: Annotated[str | None, Query(max_length=8)] = None,
) -> ChallanSummaryOut:
    """Aggregate counts + value over the challan register (module-gated, read-only).

    Value (`total_value_paise`) sums ISSUED rows only, ignoring VOID rows and
    value-free (NULL `total_paise`) rows; an empty scope yields 0, never NULL.
    """
    _require_module(user)
    issued = ChallanStatus.ISSUED.value
    void = ChallanStatus.VOID.value

    conds = []
    if series:
        conds.append(Challan.series == series.strip().upper())
    if fy:
        conds.append(Challan.fy == fy)

    issued_hit = case((Challan.status == issued, 1), else_=0)
    void_hit = case((Challan.status == void, 1), else_=0)
    eway_hit = case(
        ((Challan.status == issued) & (Challan.eway_required.is_(True)), 1), else_=0
    )
    valued_hit = case(
        ((Challan.status == issued) & (Challan.total_paise.is_not(None)), 1), else_=0
    )
    issued_value = case((Challan.status == issued, Challan.total_paise), else_=None)

    agg = db.execute(
        select(
            func.coalesce(func.sum(issued_hit), 0).label("issued_count"),
            func.coalesce(func.sum(void_hit), 0).label("void_count"),
            func.coalesce(func.sum(eway_hit), 0).label("eway_count"),
            func.coalesce(func.sum(valued_hit), 0).label("valued_count"),
            func.coalesce(func.sum(issued_value), 0).label("total_value_paise"),
        ).where(*conds)
    ).one()

    grp_stmt = (
        select(
            Challan.series.label("series"),
            Challan.fy.label("fy"),
            func.coalesce(func.sum(issued_hit), 0).label("issued"),
            func.coalesce(func.sum(void_hit), 0).label("void"),
            func.coalesce(func.sum(issued_value), 0).label("total_value_paise"),
        )
        .where(*conds)
        .group_by(Challan.series, Challan.fy)
        .order_by(Challan.fy.desc(), Challan.series.asc())
    )
    by_series = [
        SeriesBreakdownOut(
            series=row.series,
            fy=row.fy,
            issued=int(row.issued or 0),
            void=int(row.void or 0),
            total_value_paise=int(row.total_value_paise or 0),
        )
        for row in db.execute(grp_stmt)
    ]

    return ChallanSummaryOut(
        issued_count=int(agg.issued_count or 0),
        void_count=int(agg.void_count or 0),
        eway_count=int(agg.eway_count or 0),
        valued_count=int(agg.valued_count or 0),
        total_value_paise=int(agg.total_value_paise or 0),
        by_series=by_series,
    )


# --------------------------------------------------------------------- void

@router.post("/challan/challans/{challan_id}/void", response_model=ChallanOut)
def void_challan(
    challan_id: int,
    body: VoidBody,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> Challan:
    """Void a challan and its bound number. ADMIN only."""
    if not can(user, "challan.void"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "challan.void requires ADMIN")
    challan = db.get(Challan, challan_id)
    if challan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challan not found")
    try:
        service.void_challan(db, challan, reason=body.reason, actor_uid=user.firebase_uid)
    except service.ChallanError as err:
        raise HTTPException(status.HTTP_409_CONFLICT, str(err)) from err
    db.refresh(challan)
    return challan
