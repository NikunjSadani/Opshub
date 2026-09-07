"""Render + packaging: faithful HTML (escaped), zip, and merged PDF.

WeasyPrint is not imported here (native dep, container-only); we test the pure
HTML builder + zip/merge helpers.
"""
from __future__ import annotations

import io
import re
import zipfile
from types import SimpleNamespace

import pytest
import segno
from pypdf import PdfReader, PdfWriter

from app.modules.challan.render import (
    build_challan_html,
    merge_2up,
    render_fitted_challan,
    zip_files,
)
from app.modules.challan.schema import (
    ChallanView,
    ConsigneeView,
    ConsignorView,
    LineView,
    ShipToView,
)


def _view(show_amount: bool = True) -> ChallanView:
    return ChallanView(
        number="GIF/DC/26-27/L/000189",
        project_id="BRI-001",
        po_number="PO-2026-778",
        invoice_number="INV-2026-778",
        challan_date="28th July 2026",
        consignor=ConsignorView("Tech Gifsy Solutions Limited", "Howrah warehouse",
                                "19AAACT9811F1Z9", "+91 6289864191"),
        consignee=ConsigneeView("Britannia Industries Limited", "Hajipur, Bihar",
                                "10AABCB2066P2ZT", "Bihar (10)", "+91 6289864191"),
        ship_to=ShipToView("Nihit Agarwal", "Harsiddhi Main Market, Ujjain",
                           "M/S SHUBH LAXMI TRADERS", "9031281906"),
        lines=[
            LineView(1, "Titan Men's Watch", "91022900", "3", "5,500", "19,470"),
            LineView(2, "Titan Couple Watch", "91022900", "2", "9,500", "22,420"),
        ],
        total_qty="5",
        total_amount="41,890",
        show_amount=show_amount,
    )


def test_html_has_l433_structure() -> None:
    html = build_challan_html(_view())
    for token in ["Delivery Challan", "Detail of Consignor", "Detail of Consignee",
                  "Detail of Shipment to", "GIF/DC/26-27/L/000189", "91022900",
                  "Amt (incl Tax)", "(Not for Sale)", "Authorized Signatory",
                  "Nihit Agarwal", "41,890"]:
        assert token in html, token


def test_invoice_number_prints_above_challan_number() -> None:
    html = build_challan_html(_view())
    assert "Invoice No.: INV-2026-778" in html
    # positioned just above the delivery challan number
    assert html.index("Invoice No.:") < html.index("Delivery Challan No.:")
    # omitted entirely when blank
    view = _view()
    view.invoice_number = ""
    assert "Invoice No.:" not in build_challan_html(view)


def test_po_number_prints_under_challan_number() -> None:
    html = build_challan_html(_view())
    assert "PO No.: PO-2026-778" in html
    # positioned just below the delivery challan number
    assert html.index("Delivery Challan No.:") < html.index("PO No.:")
    # omitted entirely (label and all) when blank
    view = _view()
    view.po_number = ""
    assert "PO No.:" not in build_challan_html(view)


def test_project_id_is_never_printed() -> None:
    # Project ID is an internal reference and must NOT appear on the document,
    # whether present or blank.
    html = build_challan_html(_view())
    assert "Project ID" not in html
    assert "BRI-001" not in html


def test_page_is_wide_half_landscape_a4() -> None:
    # The template designs for the wide half-of-landscape-A4 canvas (842pt x 297.5pt)
    # so merge_2up stacks two full-width challans per LANDSCAPE-A4 sheet at 100% (no
    # shrink-to-fit). The @page rule must target that compact wide half-sheet.
    html = build_challan_html(_view())
    assert "size: 842pt 297.5pt" in html
    assert "A5 landscape" not in html


def test_html_escapes_untrusted_values() -> None:
    view = _view()
    view.lines[0].description = "<script>alert(1)</script>"
    view.ship_to.name = "</td><td>x"
    view.consignee.address = "<b>inject</b>"
    view.po_number = "<img src=x onerror=alert(1)>"
    html = build_challan_html(view)
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "</td><td>x" not in html
    assert "<b>inject</b>" not in html
    assert "<img src=x" not in html


def test_value_free_hides_amounts() -> None:
    html = build_challan_html(_view(show_amount=False))
    # Amounts/rates are suppressed when value-free.
    assert "41,890" not in html
    assert "19,470" not in html
    # ...but the goods still list.
    assert "Titan Couple Watch" in html


def _patch_base_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Point `get_settings().public_base_url` at `url` for the render under test
    (render.py imports get_settings lazily from app.config, so patch it there)."""
    monkeypatch.setattr("app.config.get_settings",
                        lambda: SimpleNamespace(public_base_url=url))


def test_qr_embedded_when_base_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # public_base_url set + a minted token -> the challan carries the QR for the
    # exact `/d/{token}` viewer URL (rstrip'd trailing slash), inline in the SVG.
    _patch_base_url(monkeypatch, "https://ops.example.com/")
    view = _view()
    view.access_token = "tok_ABC123"
    html = build_challan_html(view)
    assert "class='qr'" in html
    # The precise URL is proven by reproducing the very SVG the renderer embeds.
    expected = segno.make("https://ops.example.com/d/tok_ABC123", error="m").svg_inline(
        border=2, omitsize=True, svgclass="qrimg")
    assert expected in html


def test_qr_omitted_when_base_url_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dormant: base URL unconfigured -> NO QR even though a token exists.
    _patch_base_url(monkeypatch, "")
    view = _view()
    view.access_token = "tok_ABC123"
    html = build_challan_html(view)
    assert "class='qr'" not in html
    assert "<svg" not in html


def test_qr_omitted_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dormant: base URL set but no token on the view -> NO QR.
    _patch_base_url(monkeypatch, "https://ops.example.com")
    html = build_challan_html(_view())  # default access_token == ""
    assert "class='qr'" not in html
    assert "<svg" not in html


def test_zip_files_round_trips() -> None:
    data = zip_files([("a.pdf", b"AAA"), ("b.pdf", b"BBB")])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ["a.pdf", "b.pdf"]
        assert z.read("a.pdf") == b"AAA"


def _blank_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=842, height=297.5)  # wide half-of-landscape-A4
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_zip_stream_matches_zip_files() -> None:
    # The memory-bounded streaming ZIP must yield the same entries/content as the byte-based one.
    import tempfile

    from app.modules.challan.render import zip_stream

    named = [("a.pdf", b"hello world"), ("b.pdf", b"second file")]
    with tempfile.NamedTemporaryFile() as out:
        zip_stream([(n, io.BytesIO(d)) for n, d in named], out)
        out.seek(0)
        streamed = out.read()

    def entries(blob: bytes) -> dict[str, bytes]:
        z = zipfile.ZipFile(io.BytesIO(blob))
        return {n: z.read(n) for n in z.namelist()}

    assert entries(streamed) == entries(zip_files(named)) == {
        "a.pdf": b"hello world", "b.pdf": b"second file",
    }


def _pdf_of(width: float, height: float, count: int = 1) -> bytes:
    """A PDF of `count` blank pages, each `width` x `height` points."""
    w = PdfWriter()
    for _ in range(count):
        w.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _sheet_sizes(merged: bytes) -> list[tuple[float, float]]:
    return [(float(p.mediabox.width), float(p.mediabox.height))
            for p in PdfReader(io.BytesIO(merged)).pages]


def test_bin_pack_two_shorts_stack_on_one_landscape_sheet() -> None:
    # Two short (842x297.5) challans pair 2-up onto ONE landscape-A4 sheet.
    merged = merge_2up([_pdf_of(842.0, 297.5), _pdf_of(842.0, 297.5)])
    assert _sheet_sizes(merged) == [(842.0, 595.0)]


def test_bin_pack_single_tall_takes_a_whole_sheet() -> None:
    # A tall (842x595) challan occupies a whole landscape-A4 sheet on its own.
    merged = merge_2up([_pdf_of(842.0, 595.0)])
    assert _sheet_sizes(merged) == [(842.0, 595.0)]


def test_bin_pack_short_then_tall_never_share_a_sheet() -> None:
    # A short then a tall: the short must NOT be shrunk to share the tall's sheet — it
    # gets its own sheet (alone), and the tall gets its own. TWO sheets (if they shared,
    # it would be one) — both full landscape-A4.
    merged = merge_2up([_pdf_of(842.0, 297.5), _pdf_of(842.0, 595.0)])
    assert _sheet_sizes(merged) == [(842.0, 595.0), (842.0, 595.0)]


def test_bin_pack_three_shorts_make_two_sheets() -> None:
    # Three shorts -> a paired sheet (2 shorts) + a lone short (1) = 2 landscape sheets.
    merged = merge_2up([_pdf_of(842.0, 297.5)] * 3)
    assert _sheet_sizes(merged) == [(842.0, 595.0), (842.0, 595.0)]


class _HeightRenderer:
    """A fake `Renderer` that inspects the @page height in the html and emits ONE page
    at/above `fits_at`, else TWO (a challan that overflows the shorter page)."""

    def __init__(self, fits_at: float) -> None:
        self.fits_at = fits_at
        self.calls = 0

    def render_pdf(self, html: str) -> bytes:
        self.calls += 1
        m = re.search(r"size: 842pt ([0-9.]+)pt", html)
        assert m, "the @page size must carry the requested height"
        height = float(m.group(1))
        pages = 1 if height >= self.fits_at else 2
        return _pdf_of(842.0, height, pages)


def test_render_fitted_challan_keeps_short_when_it_fits() -> None:
    # Fits at 297.5 -> one render, a single 297.5-tall page.
    renderer = _HeightRenderer(fits_at=297.5)
    out = render_fitted_challan(renderer, _view())
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 1
    assert float(reader.pages[0].mediabox.height) == 297.5
    assert renderer.calls == 1


def test_render_fitted_challan_promotes_to_tall_when_short_overflows() -> None:
    # Overflows 297.5 (2 pages) but fits 595 (1 page) -> a single 595-tall page (own sheet).
    renderer = _HeightRenderer(fits_at=595.0)
    out = render_fitted_challan(renderer, _view())
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 1
    assert float(reader.pages[0].mediabox.height) == 595.0
    assert renderer.calls == 2


def test_merge_2up_stream_matches_merge_2up() -> None:
    # Streaming 2-up merge from file handles == the byte-based merge_2up (same page count).
    import tempfile

    from app.modules.challan.render import merge_2up, merge_2up_stream

    pdfs = [_blank_pdf(), _blank_pdf(), _blank_pdf()]  # 3 wide half-sheets -> 2 landscape-A4 sheets
    ref = PdfReader(io.BytesIO(merge_2up(pdfs)))
    ref_pages = len(ref.pages)
    # Output sheet is LANDSCAPE A4: 842 wide x 595 tall.
    box = ref.pages[0].mediabox
    assert (float(box.width), float(box.height)) == (842.0, 595.0)
    with tempfile.NamedTemporaryFile() as out:
        merge_2up_stream([io.BytesIO(p) for p in pdfs], out)
        out.seek(0)
        streamed = out.read()
    assert len(PdfReader(io.BytesIO(streamed)).pages) == ref_pages == 2
