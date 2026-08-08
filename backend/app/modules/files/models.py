"""Files module tables (prefixed `files_`).

NOTE: no `from __future__ import annotations` here — it triggers a SQLAlchemy
de-stringify crash on Python 3.14 when models are involved.
"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StoredFile(Base):
    """One uploaded blob's metadata; the bytes live in the storage backend by `storage_ref`."""

    __tablename__ = "files_stored_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), default="upload")
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    storage_ref: Mapped[str] = mapped_column(String(1024))
    uploaded_by: Mapped[str] = mapped_column(String(128), index=True)  # firebase_uid
    # Which module owns this file; download is gated by module access (None = owner/admin only).
    module_key: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
