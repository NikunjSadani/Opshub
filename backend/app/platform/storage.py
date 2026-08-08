"""File-storage primitive — a pluggable blob store behind a tiny Protocol.

`LocalStorage` writes under a base dir (env `FILES_DIR`, default `./_files`).
`GcsStorage` is a future backend (raises for now). `get_storage()` picks the impl.

Path-safety is the whole point of the sanitising in `_resolve_within`: a caller
(or a crafted filename that reaches this layer) must NEVER be able to read or
write outside the base dir. We reject `..`, absolute paths and drive letters,
then resolve and assert the final path stays inside the base.
"""
from __future__ import annotations

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


class GcsStorage:
    """Future Google Cloud Storage backend. Not yet implemented."""

    def __init__(self, bucket: str | None = None) -> None:
        self.bucket = bucket

    def save(self, key: str, data: bytes) -> str:
        raise NotImplementedError("GcsStorage is not implemented yet")

    def open(self, ref: str) -> BinaryIO:
        raise NotImplementedError("GcsStorage is not implemented yet")

    def delete(self, ref: str) -> None:
        raise NotImplementedError("GcsStorage is not implemented yet")


def get_storage() -> Storage:
    """Return the active storage backend (LocalStorage for now)."""
    return LocalStorage()
