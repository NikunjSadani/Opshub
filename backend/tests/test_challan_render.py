"""Render + packaging: faithful HTML (escaped), zip, and merged PDF.

WeasyPrint is not imported here (native dep, container-only); we test the pure
HTML builder + zip/merge helpers.
"""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
import segno
from pypdf import PdfReader, PdfWriter

from app.modules.challan.render import build_challan_html, merge_pdfs, zip_files
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


def test_page_is_a5_landscape() -> None:
    # The template designs for the half-A4 canvas so merge_2up stacks two per A4
    # sheet at 100% (no shrink-to-fit). The @page rule must target A5 landscape.
    html = build_challan_html(_view())
    assert "size: A5 landscape" in html
    assert "size: A4" not in html


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


def _one_page_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_merge_pdfs_concatenates_pages() -> None:
    merged = merge_pdfs([_one_page_pdf(), _one_page_pdf()])
    assert len(PdfReader(io.BytesIO(merged)).pages) == 2


def test_merge_pdfs_empty_raises() -> None:
    try:
        merge_pdfs([])
    except ValueError:
        return
    raise AssertionError("expected ValueError on empty merge")


def _blank_pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(width=595, height=421)  # A5-landscape half-A4
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


def test_merge_2up_stream_matches_merge_2up() -> None:
    # Streaming 2-up merge from file handles == the byte-based merge_2up (same page count).
    import tempfile

    from app.modules.challan.render import merge_2up, merge_2up_stream

    pdfs = [_blank_pdf(), _blank_pdf(), _blank_pdf()]  # 3 half-A4 pages -> 2 A4 sheets
    ref_pages = len(PdfReader(io.BytesIO(merge_2up(pdfs))).pages)
    with tempfile.NamedTemporaryFile() as out:
        merge_2up_stream([io.BytesIO(p) for p in pdfs], out)
        out.seek(0)
        streamed = out.read()
    assert len(PdfReader(io.BytesIO(streamed)).pages) == ref_pages == 2
