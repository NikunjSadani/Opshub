"""Files API — upload a blob, download it streamed back through the API.

Mounts at /api/v1/files. Downloads stream from the storage backend through the
app (never a public bucket) and are authorized + audited. Download access:
the uploader, an Admin, or a user with access to the file's owning module.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.files.models import StoredFile
from app.platform import audit
from app.platform.auth import current_user
from app.platform.models import Level, User
from app.platform.module_registry import ModuleSpec
from app.platform.rbac import can_access_module, has_any_module, has_at_least, is_administrator
from app.platform.storage import get_storage

router = APIRouter()

_DOWNLOAD_CHUNK = 64 * 1024
_UPLOAD_CHUNK = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_filename(name: str) -> str:
    # Strip control chars/nulls (would break the DB text column + headers) and cap length.
    return _CONTROL.sub("", name).strip()[:512] or "unnamed"


def _may_download(user: User, row: StoredFile) -> bool:
    if is_administrator(user) or row.uploaded_by == user.firebase_uid:
        return True
    return row.module_key is not None and can_access_module(user, row.module_key)


def _may_upload(user: User) -> bool:
    """Gate the upload endpoint: uploading is a WRITE, so an active caller may upload
    only if they hold OPERATE (or higher) on at least one module (Administrators
    always qualify). A View-only or no-grant user has no business stashing blobs."""
    if not user.active:
        return False
    return has_any_module(user, Level.OPERATE)


@router.post("/upload")
def upload_file(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile,
    module_key: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    """Accept a multipart file (bounded), persist the bytes, record metadata, audit it."""
    # Authorization: a caller with no module access at all cannot upload; and a
    # client-supplied module_key must be one this caller may access (no stamping
    # a blob into a module they don't hold). 403 on either failure.
    if not _may_upload(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no module access")
    if module_key is not None and not has_at_least(user, module_key, Level.OPERATE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no write access to that module")

    max_bytes = get_settings().max_upload_bytes
    data = bytearray()
    while chunk := file.file.read(_UPLOAD_CHUNK):
        data.extend(chunk)
        if len(data) > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

    filename = _sanitize_filename(file.filename or "unnamed")
    # The stored key is a fresh uuid (+ suffix) — the client filename never enters the path.
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix
    key = f"{uuid4().hex}{suffix}"

    storage_ref = get_storage().save(key, bytes(data))
    row = StoredFile(
        kind="upload",
        filename=filename,
        content_type=file.content_type,
        size=len(data),
        storage_ref=storage_ref,
        uploaded_by=user.firebase_uid,
        module_key=module_key,
    )
    db.add(row)
    db.flush()  # assign row.id

    # Do NOT put the raw filename (possible PII) into the immutable audit log.
    audit.log(
        db,
        action="file.upload",
        actor_uid=user.firebase_uid,
        entity="stored_file",
        entity_id=str(row.id),
        detail={"size": len(data), "module_key": module_key},
    )
    db.commit()
    return {"id": row.id, "filename": row.filename, "size": row.size}


@router.get("/{file_id}/download")
def download_file(
    file_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> StreamingResponse:
    """Stream a stored file's bytes to an authorized caller. 404 (not 403) on a miss
    OR an unauthorized id, so existence isn't confirmed to a probing user."""
    row = db.execute(select(StoredFile).where(StoredFile.id == file_id)).scalar_one_or_none()
    if row is None or not _may_download(user, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")

    try:
        handle = get_storage().open(row.storage_ref)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file bytes missing") from exc

    audit.log(
        db,
        action="file.download",
        actor_uid=user.firebase_uid,
        entity="stored_file",
        entity_id=str(row.id),
        detail={"size": row.size},
    )
    db.commit()

    def _stream() -> Iterator[bytes]:
        try:
            while chunk := handle.read(_DOWNLOAD_CHUNK):
                yield chunk
        finally:
            handle.close()

    # RFC 5987 filename* with an ascii fallback; sanitized so no header injection.
    ascii_name = row.filename.encode("ascii", "ignore").decode() or "download"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(row.filename)}"
    return StreamingResponse(
        _stream(),
        media_type=row.content_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


SPEC = ModuleSpec(key="files", title="Files", router=router, nav_group="_system")
