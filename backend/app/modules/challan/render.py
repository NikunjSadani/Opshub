"""Render + packaging layer for delivery challans.

Turns a fully-resolved, ORM-free `ChallanView` (see `schema.py`) into a faithful
A4 business-document HTML, then into PDF bytes, and packages many of those into a
single ZIP or a merged PDF for a batch download.

SECURITY — every string in a `ChallanView` originates from an untrusted Excel
upload. `build_challan_html` binds each value through `markupsafe.escape`, so a
cell containing `<script>` or `</td><td>` renders as inert text and can never
break out of its element or inject markup. Nothing is string-concatenated raw
into the template.

The heavy, native dependencies are imported LAZILY inside the functions that need
them (`weasyprint` for PDF rendering, `pypdf` for merging), so importing this
module — and unit-testing `build_challan_html` / `zip_files` — needs neither
installed.
"""
from __future__ import annotations

import io
import zipfile
from typing import Protocol

from markupsafe import Markup, escape

from app.modules.challan.schema import ChallanView, LineView, PartyView


def _party_block(party: PartyView) -> Markup:
    """Escaped multi-line block for a consignor/consignee party."""
    parts = [f"<div class='party-name'>{escape(party.name)}</div>"]
    if party.address:
        parts.append(f"<div>{escape(party.address)}</div>")
    if party.state:
        parts.append(f"<div>State: {escape(party.state)}</div>")
    if party.gstin:
        parts.append(f"<div>GSTIN: {escape(party.gstin)}</div>")
    return Markup("").join(Markup(p) for p in parts)


def _line_row(line: LineView) -> Markup:
    """One escaped `<tr>` for the line-item table (columns in statutory order)."""
    cells = [
        str(line.line_no),
        line.description,
        line.hsn,
        line.quantity,
        line.uom,
        line.rate,
        line.amount,
        line.gst_rate,
    ]
    tds = Markup("").join(Markup(f"<td>{escape(c)}</td>") for c in cells)
    return Markup(f"<tr>{tds}</tr>")


def build_challan_html(view: ChallanView) -> str:
    """Build a complete standalone A4 HTML document for one challan.

    Pure function, no I/O. Every interpolated value is HTML-escaped, so untrusted
    Excel cell text is rendered as inert data and cannot inject markup.
    """
    rows = Markup("").join(_line_row(line) for line in view.lines)

    meta_bits: list[Markup] = []
    if view.po_number:
        meta_bits.append(Markup(f"<div>PO No: {escape(view.po_number)}</div>"))
    if view.invoice_number:
        meta_bits.append(Markup(f"<div>Invoice No: {escape(view.invoice_number)}</div>"))
    meta = Markup("").join(meta_bits)

    eway = (
        Markup("<div class='eway'>E-Way Bill Required</div>")
        if view.eway_required
        else Markup("")
    )

    document = Markup(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Delivery Challan {number}</title>
<style>
  @page {{ size: A4; margin: 14mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, "Helvetica Neue", sans-serif; font-size: 11px;
         color: #111; margin: 0; }}
  .doc {{ width: 100%; }}
  .header {{ display: flex; justify-content: space-between;
            border-bottom: 2px solid #111; padding-bottom: 8px; }}
  .consignor {{ max-width: 55%; }}
  .party-name {{ font-weight: bold; font-size: 13px; }}
  .title-box {{ text-align: right; }}
  .doc-title {{ font-size: 20px; font-weight: bold; letter-spacing: 1px; }}
  .doc-meta {{ margin-top: 4px; }}
  .doc-meta .num {{ font-weight: bold; }}
  .parties {{ display: flex; gap: 12px; margin-top: 10px; }}
  .parties .box {{ flex: 1; border: 1px solid #999; padding: 6px; }}
  .box-label {{ font-weight: bold; text-transform: uppercase; font-size: 9px;
               color: #555; margin-bottom: 3px; }}
  table.lines {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  table.lines th, table.lines td {{ border: 1px solid #999; padding: 4px 5px;
                                    text-align: left; vertical-align: top; }}
  table.lines th {{ background: #f0f0f0; font-size: 10px; text-transform: uppercase; }}
  table.lines td:nth-child(4), table.lines td:nth-child(6),
  table.lines td:nth-child(7), table.lines td:nth-child(8) {{ text-align: right; }}
  .total-row td {{ font-weight: bold; background: #f6f6f6; }}
  .footer {{ margin-top: 14px; display: flex; justify-content: space-between;
            align-items: flex-end; }}
  .eway {{ display: inline-block; margin-top: 8px; padding: 4px 8px;
          border: 2px solid #b00; color: #b00; font-weight: bold; }}
  .sign {{ text-align: right; }}
  .sign .line {{ margin-top: 40px; border-top: 1px solid #111; width: 160px;
                display: inline-block; text-align: center; padding-top: 3px; }}
</style>
</head>
<body>
<div class="doc">
  <div class="header">
    <div class="consignor">{consignor}</div>
    <div class="title-box">
      <div class="doc-title">DELIVERY CHALLAN</div>
      <div class="doc-meta">
        <div>Challan No: <span class="num">{number}</span></div>
        <div>Date: {date}</div>
        {meta}
      </div>
    </div>
  </div>
  <div class="parties">
    <div class="box">
      <div class="box-label">Bill To</div>
      {consignee}
    </div>
    <div class="box">
      <div class="box-label">Ship To</div>
      <div class="party-name">{ship_to_name}</div>
      <div>{ship_to_address}</div>
      <div>State: {ship_to_state}</div>
    </div>
  </div>
  <table class="lines">
    <thead>
      <tr>
        <th>No.</th><th>Description</th><th>HSN</th><th>Qty</th><th>UOM</th>
        <th>Rate</th><th>Amount</th><th>GST%</th>
      </tr>
    </thead>
    <tbody>
      {rows}
      <tr class="total-row">
        <td colspan="6" style="text-align:right;">Total</td>
        <td>{total}</td>
        <td></td>
      </tr>
    </tbody>
  </table>
  <div class="footer">
    <div>{eway}</div>
    <div class="sign">
      {consignor_name}
      <div class="line">Authorised Signatory</div>
    </div>
  </div>
</div>
</body>
</html>"""
    ).format(
        number=escape(view.number),
        date=escape(view.challan_date),
        consignor=_party_block(view.consignor),
        consignee=_party_block(view.consignee),
        ship_to_name=escape(view.ship_to_name),
        ship_to_address=escape(view.ship_to_address),
        ship_to_state=escape(view.ship_to_state),
        meta=meta,
        rows=rows,
        total=escape(view.total_amount),
        eway=eway,
        consignor_name=escape(view.consignor.name),
    )
    return str(document)


class Renderer(Protocol):
    """Anything that turns an HTML document string into PDF bytes."""

    def render_pdf(self, html: str) -> bytes: ...


class WeasyPrintRenderer:
    """`Renderer` backed by WeasyPrint.

    WeasyPrint is imported lazily inside `render_pdf` so this module (and the
    pure-HTML unit tests) do not require the native library to be installed.
    """

    def render_pdf(self, html: str) -> bytes:
        from weasyprint import HTML  # lazy: native dep, not needed to import

        # Defense-in-depth SSRF guard: the HTML is rendered from 100%-untrusted
        # upload data. Even though every value is escaped into element text (no
        # attacker-controlled src/url today), forbid the renderer from fetching
        # any non-`data:` resource so a future template edit can't turn a crafted
        # cell into a request to file:// or the cloud metadata endpoint.
        def _only_data_urls(url: str, *args: object, **kwargs: object) -> object:
            if not url.startswith("data:"):
                raise ValueError(f"blocked external resource in challan render: {url}")
            from weasyprint.urls import default_url_fetcher

            return default_url_fetcher(url, *args, **kwargs)

        return bytes(HTML(string=html, url_fetcher=_only_data_urls).write_pdf())


def zip_files(named: list[tuple[str, bytes]]) -> bytes:
    """Build a ZIP archive from `(filename, bytes)` pairs and return its bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in named:
            archive.writestr(name, data)
    return buffer.getvalue()


def merge_pdfs(pdfs: list[bytes]) -> bytes:
    """Concatenate PDF byte-strings into one PDF and return its bytes.

    Raises `ValueError` on an empty list rather than emitting a page-less PDF that
    some readers reject.
    """
    if not pdfs:
        raise ValueError("merge_pdfs requires at least one PDF")

    from pypdf import PdfReader, PdfWriter  # lazy: keep import light

    writer = PdfWriter()
    for pdf in pdfs:
        reader = PdfReader(io.BytesIO(pdf))
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
