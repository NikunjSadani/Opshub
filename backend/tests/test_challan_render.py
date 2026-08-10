"""Render + packaging layer tests.

Exercises the pure HTML builder (structure + statutory markers + the critical
untrusted-Excel escaping), the ZIP packager, and the PDF merge. WeasyPrint is
NOT imported here (it is not installed locally); only `build_challan_html`,
`zip_files`, and `merge_pdfs` are covered, and `merge_pdfs`'s tiny fixtures are
built with pypdf (already a dependency).
"""
from __future__ import annotations

import io
import zipfile

import pytest
from pypdf import PdfReader, PdfWriter

from app.modules.challan.render import build_challan_html, merge_pdfs, zip_files
from app.modules.challan.schema import ChallanView, LineView, PartyView


def _view(*, eway_required: bool = False) -> ChallanView:
    consignor = PartyView(
        name="Gifsy Foods Pvt Ltd", gstin="19AAACG1234A1Z5",
        state="West Bengal", address="12 Park Street, Kolkata",
    )
    consignee = PartyView(
        name="Acme Distributors", gstin="27AAACA9999B1Z2",
        state="Maharashtra", address="5 MG Road, Mumbai",
    )
    lines = [
        LineView(
            line_no=1, description="<script>alert(1)</script>", hsn="15099090",
            quantity="10", uom="NOS", rate="100.00", amount="1,000.00", gst_rate="5",
        ),
        LineView(
            line_no=2, description="Olive Oil 1L", hsn="15091000",
            quantity="4", uom="NOS", rate="250.00", amount="1,000.00", gst_rate="12",
        ),
    ]
    return ChallanView(
        number="GIF/DC/26-27/L/000189", challan_date="10-08-2026",
        consignor=consignor, consignee=consignee,
        ship_to_name="</td><td>x", ship_to_address="99 Dock Rd",
        ship_to_state="Gujarat", po_number="PO-77", invoice_number="INV-42",
        eway_required=eway_required, lines=lines, total_amount="2,000.00",
    )


def test_html_contains_core_document_fields() -> None:
    html = build_challan_html(_view())
    assert "DELIVERY CHALLAN" in html
    assert "GIF/DC/26-27/L/000189" in html
    assert "10-08-2026" in html
    # Both HSNs present.
    assert "15099090" in html
    assert "15091000" in html
    # Total present.
    assert "2,000.00" in html
    # Ship-to name shows up (escaped — see the escaping test).
    assert "Ship To" in html
    # PO / Invoice metadata rendered.
    assert "PO-77" in html
    assert "INV-42" in html
    # Statutory column headers.
    for header in ("Description", "HSN", "Qty", "UOM", "Rate", "Amount", "GST%"):
        assert header in html


def test_untrusted_values_are_escaped_not_raw() -> None:
    html = build_challan_html(_view())
    # A malicious description must be inert text, never a live <script> tag.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    # A cell that tries to break out of its <td> must be escaped, not raw markup.
    assert "&lt;/td&gt;&lt;td&gt;x" in html
    assert "</td><td>x" not in html


def test_eway_marker_only_when_required() -> None:
    assert "E-Way Bill Required" not in build_challan_html(_view(eway_required=False))
    assert "E-Way Bill Required" in build_challan_html(_view(eway_required=True))


def test_zip_files_roundtrips_names_and_contents() -> None:
    named = [
        ("GIF-DC-000189.pdf", b"%PDF-fake-1"),
        ("GIF-DC-000190.pdf", b"%PDF-fake-2"),
    ]
    blob = zip_files(named)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.namelist() == ["GIF-DC-000189.pdf", "GIF-DC-000190.pdf"]
        assert archive.read("GIF-DC-000189.pdf") == b"%PDF-fake-1"
        assert archive.read("GIF-DC-000190.pdf") == b"%PDF-fake-2"


def _tiny_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_merge_pdfs_concatenates_pages() -> None:
    merged = merge_pdfs([_tiny_pdf(), _tiny_pdf()])
    reader = PdfReader(io.BytesIO(merged))
    assert len(reader.pages) == 2


def test_merge_pdfs_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one PDF"):
        merge_pdfs([])
