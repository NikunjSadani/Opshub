"""File-storage primitive — a pluggable blob store behind a tiny Protocol.

`LocalStorage` writes under a base dir (env `FILES_DIR`, default `./_files`).
`GcsStorage` is a future backend (raises for now). `get_storage()` picks the impl.

Path-safety is the whole point of the sanitising in `_resolve_within`: a caller
(or a crafted filename that reaches this layer) must NEVER be able to read or
write outside the base dir. We reject `..`, absolute paths and drive letters,
then resolve and assert the final path stays inside the base.
"""
from __future__ import annotations

import io
import os
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, runtime_checkable

DEFAULT_BASE_DIR = "./_files"


@runtime_checkable
class Storage(Protocol):
    """A minimal blob store. `save` returns an opaque storage ref used by `open`/`delete`."""

    def save(self, key: str, data: bytes) -> str: ...

    def open(self, ref: str) -> BinaryIO: ...

    def delete(self, ref: str) -> None: ...


def _resolve_within(base: Path, rel: str) -> Path:
    """Resolve `rel` under `base`, rejecting anything that escapes the base dir."""
    if not rel or not rel.strip():
        raise ValueError("empty storage key")
    candidate = rel.replace("\\", "/")
    # drive letter (e.g. "C:/...") or absolute path -> escape attempt.
    if len(candidate) >= 2 and candidate[1] == ":":
        raise ValueError(f"drive-qualified path rejected: {rel!r}")
    pure = PurePosixPath(candidate)
    if pure.is_absolute():
        raise ValueError(f"absolute path rejected: {rel!r}")
    if any(part == ".." for part in pure.parts):
        raise ValueError(f"parent-traversal rejected: {rel!r}")
    target = (base / pure).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes base dir: {rel!r}") from exc
    if target == base:
        raise ValueError(f"key resolves to the base dir itself: {rel!r}")
    return target


class LocalStorage:
    """Store blobs as files under a base directory on the local filesystem."""

    def __init__(self, base_dir: str | None = None) -> None:
        raw = base_dir if base_dir is not None else os.environ.get("FILES_DIR", DEFAULT_BASE_DIR)
        self.base_dir = Path(raw).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, data: bytes) -> str:
        target = _resolve_within(self.base_dir, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # The ref is the sanitised, posix-relative key — re-validatable by open/delete.
        return target.relative_to(self.base_dir).as_posix()

    def open(self, ref: str) -> BinaryIO:
        target = _resolve_within(self.base_dir, ref)
        if not target.is_file():
            raise FileNotFoundError(ref)
        return target.open("rb")

    def delete(self, ref: str) -> None:
        target = _resolve_within(self.base_dir, ref)
        target.unlink(missing_ok=True)


def _gcs_key(key: str) -> str:
    """Sanitise an object key: no drive letter, no absolute path, no `..` segment, non-empty.
    GCS object names are flat, but we keep the same defensive rules as LocalStorage so a bad
    key can never behave surprisingly. The (sanitised) key IS the ref."""
    candidate = (key or "").replace("\\", "/").strip().strip("/")
    if not candidate:
        raise ValueError("empty storage key")
    if len(candidate) >= 2 and candidate[1] == ":":
        raise ValueError(f"drive-qualified path rejected: {key!r}")
    if any(part == ".." for part in PurePosixPath(candidate).parts):
        raise ValueError(f"parent-traversal rejected: {key!r}")
    return candidate


class GcsStorage:
    """Google Cloud Storage backend. The object name is the ref. Credentials come from the
    ambient environment (the Cloud Run service account's ADC) — no key files. The heavy client
    library is imported lazily so importing this module stays cheap and native-free elsewhere."""

    def __init__(self, bucket: str | None = None) -> None:
        name = (bucket or os.environ.get("GCS_BUCKET", "") or "").strip()
        if not name:
            raise ValueError("GcsStorage requires a bucket name (GCS_BUCKET)")
        self.bucket_name = name
        from google.cloud import storage as gcs  # type: ignore[attr-defined]  # lazy heavy import

        self._bucket = gcs.Client().bucket(name)

    def save(self, key: str, data: bytes) -> str:
        k = _gcs_key(key)
        self._bucket.blob(k).upload_from_string(data)
        return k

    def open(self, ref: str) -> BinaryIO:
        k = _gcs_key(ref)
        from google.cloud.exceptions import NotFound  # lazy

        try:
            data = self._bucket.blob(k).download_as_bytes()
        except NotFound as exc:  # match LocalStorage: a missing blob is FileNotFoundError
            raise FileNotFoundError(ref) from exc
        return io.BytesIO(data)

    def delete(self, ref: str) -> None:
        k = _gcs_key(ref)
        from google.cloud.exceptions import NotFound  # lazy

        try:
            self._bucket.blob(k).delete()
        except NotFound:
            pass  # already gone — LocalStorage.delete is likewise missing-ok


def get_storage() -> Storage:
    """Return the active storage backend: GCS when `GCS_BUCKET` is set (prod/staging on Cloud
    Run), else LocalStorage (local dev / tests). Selection is env-driven so the same code runs
    everywhere with no branching in callers."""
    if os.environ.get("GCS_BUCKET", "").strip():
        return GcsStorage()
    return LocalStorage()
