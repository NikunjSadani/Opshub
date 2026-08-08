"""Same-origin SPA serving: real files served, client routes fall back to
index.html, API paths still resolve (and 404 as API, not as the SPA)."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>OpsHub</title>")
    (static / "assets" / "app.js").write_text("console.log('spa')")
    monkeypatch.setenv("STATIC_DIR", str(static))
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def test_index_served_at_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "OpsHub" in r.text


def test_client_route_falls_back_to_index(client: TestClient) -> None:
    r = client.get("/login")  # no such file → SPA index for client-side routing
    assert r.status_code == 200 and "OpsHub" in r.text


def test_real_asset_is_served(client: TestClient) -> None:
    r = client.get("/assets/app.js")
    assert r.status_code == 200 and "spa" in r.text


def test_api_still_resolves(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"


def test_unknown_api_path_is_404_not_spa(client: TestClient) -> None:
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert "OpsHub" not in r.text  # not the SPA index
