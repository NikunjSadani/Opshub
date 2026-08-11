"""Render + packaging: faithful HTML (escaped), zip, and merged PDF.

WeasyPrint is not imported here (native dep, container-only); we test the pure
HTML builder + zip/merge helpers.
"""
from __future__ import annotations

import io
import zipfile

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
        challan_date="28th July 2026",
        consignor=ConsignorView("Tech Gifsy Solutions Limited", "Howrah warehouse",
                                "19AAACT9811F1Z9", "+91 6289864191"),
        consignee=ConsigneeView("Britannia Industries Limited", "Hajipur, Bihar",
                                "10AABCB2066P2ZT", "Bihar (10)", "+91 6289864191"),
        ship_to=ShipToView("Nihit Agarwal", "Harsiddhi Main Market",
                           "M/S SHUBH LAXMI TRADERS", "9031281906", "Nihit"),
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


def test_html_escapes_untrusted_values() -> None:
    view = _view()
    view.lines[0].description = "<script>alert(1)</script>"
    view.ship_to.name = "</td><td>x"
    html = build_challan_html(view)
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "</td><td>x" not in html


def test_value_free_hides_amounts() -> None:
    html = build_challan_html(_view(show_amount=False))
    # Amounts/rates are suppressed when value-free.
    assert "41,890" not in html
    assert "19,470" not in html
    # ...but the goods still list.
    assert "Titan Couple Watch" in html


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
