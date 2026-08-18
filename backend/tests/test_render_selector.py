"""The challan renderer selector is fail-closed: the native-free StubRenderer is
usable ONLY in a local env with `stub_render` on; staging/prod ALWAYS get WeasyPrint
so production can never silently ship a blank stub PDF."""
from __future__ import annotations

import pytest

from app.config import get_settings
from app.modules.challan import render


def _renderer(monkeypatch: pytest.MonkeyPatch, *, env: str, stub: bool) -> render.Renderer:
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("STUB_RENDER", "true" if stub else "false")
    get_settings.cache_clear()
    try:
        return render.get_renderer()
    finally:
        get_settings.cache_clear()


def test_stub_renderer_is_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(_renderer(monkeypatch, env="local", stub=True), render.StubRenderer)
    # local WITHOUT the flag -> the real renderer
    assert isinstance(_renderer(monkeypatch, env="local", stub=False), render.WeasyPrintRenderer)
    # a non-local env NEVER stubs, even if the flag is (mis)set -> fail-closed
    assert isinstance(_renderer(monkeypatch, env="staging", stub=True), render.WeasyPrintRenderer)
    assert isinstance(_renderer(monkeypatch, env="prod", stub=True), render.WeasyPrintRenderer)


def test_stub_renderer_emits_valid_pdf() -> None:
    pdf = render.StubRenderer().render_pdf("<html><body>x</body></html>")
    assert pdf.startswith(b"%PDF") and len(pdf) > 100
