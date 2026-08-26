"""Storage backend: key-safety, env-driven backend selection, and the GcsStorage contract.

The REAL GcsStorage was round-trip-verified against the live bucket during the first deploy;
these are the repeatable gate tests (GCS client mocked, real google.cloud.exceptions.NotFound
so the missing-blob -> FileNotFoundError mapping is exercised for real).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.platform import storage as st


def test_gcs_key_sanitises_and_rejects() -> None:
    assert st._gcs_key("a/b.pdf") == "a/b.pdf"
    assert st._gcs_key("/leading/x") == "leading/x"   # leading/trailing slashes stripped
    assert st._gcs_key("t/x/") == "t/x"
    for bad in ["", "   ", "../x", "a/../b", "C:/x", "\\\\", "/"]:
        with pytest.raises(ValueError):
            st._gcs_key(bad)


def test_get_storage_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    assert isinstance(st.get_storage(), st.LocalStorage)


def _mock_gcs(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the real google-cloud-storage Client so GcsStorage builds without a network call;
    returns the mock blob every bucket.blob(...) yields."""
    from google.cloud import storage as real_gcs

    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(real_gcs, "Client", MagicMock(return_value=client))
    monkeypatch.setenv("GCS_BUCKET", "opshub-test-bucket")
    return blob


def test_get_storage_selects_gcs_when_bucket_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_gcs(monkeypatch)
    assert isinstance(st.get_storage(), st.GcsStorage)


def test_gcs_save_open_delete_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud.exceptions import NotFound

    blob = _mock_gcs(monkeypatch)
    store = st.get_storage()

    # save -> uploads the bytes, returns the (sanitised) key as the ref.
    assert store.save("k/x.pdf", b"data") == "k/x.pdf"
    blob.upload_from_string.assert_called_once_with(b"data")

    # open -> downloads and returns a readable stream of the bytes.
    blob.download_as_bytes.return_value = b"data"
    assert store.open("k/x.pdf").read() == b"data"

    # open a missing blob -> FileNotFoundError (matches LocalStorage's contract).
    blob.download_as_bytes.side_effect = NotFound("gone")
    with pytest.raises(FileNotFoundError):
        store.open("k/x.pdf")

    # delete of a missing blob is swallowed (missing-ok, like LocalStorage).
    blob.delete.side_effect = NotFound("gone")
    store.delete("k/x.pdf")  # no raise
