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

from app.modules.challan.schema import (
    ChallanView,
    ConsigneeView,
    ConsignorView,
    LineView,
    ShipToView,
)


def _kv(label: str, value: str) -> Markup:
    """One escaped `<div>label: value</div>`, or empty when the value is blank."""
    if not value:
        return Markup("")
    return Markup(f"<div><span class='k'>{escape(label)}</span> {escape(value)}</div>")


def _consignor_block(c: ConsignorView) -> Markup:
    return Markup("").join([
        Markup(f"<div class='party-name'>{escape(c.name)}</div>"),
        _kv("Ware House :", c.warehouse_address),
        _kv("GST No. :", c.gstin),
        _kv("Phone :", c.phone),
    ])


def _consignee_block(c: ConsigneeView) -> Markup:
    return Markup("").join([
        Markup(f"<div class='party-name'>{escape(c.name)}</div>"),
        _kv("Address :", c.address),
        _kv("GST No. :", c.gstin),
        _kv("State :", c.state_label),
        _kv("Phone No. :", c.phone),
    ])


def _ship_to_block(s: ShipToView) -> Markup:
    return Markup("").join([
        _kv("Name -", s.name),
        _kv("Address -", s.address),
        _kv("Enterprise Name -", s.enterprise),
        _kv("Number -", s.number),
        _kv("Contact Person Name -", s.contact_person),
    ])


def _line_row(line: LineView, show_amount: bool) -> Markup:
    """One escaped `<tr>`: Sl.No | Product | HSN | Qty | Rate | Amt (incl Tax)."""
    cells = [
        str(line.line_no),
        line.description,
        line.hsn,
        line.quantity,
        line.rate if show_amount else "",
        line.amount if show_amount else "",
    ]
    tds = Markup("").join(Markup(f"<td>{escape(c)}</td>") for c in cells)
    return Markup(f"<tr>{tds}</tr>")


def build_challan_html(view: ChallanView) -> str:
    """Build a complete standalone A4 HTML document for one challan (faithful to
    the `L/433` layout). Pure function, no I/O; every interpolated value is
    HTML-escaped, so untrusted Excel cell text renders as inert data."""
    rows = Markup("").join(_line_row(line, view.show_amount) for line in view.lines)
    total_amount = escape(view.total_amount) if view.show_amount else Markup("")

    document = Markup(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Delivery Challan {number}</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, "Helvetica Neue", sans-serif; font-size: 11px;
         color: #111; margin: 0; }}
  .title {{ text-align: center; font-size: 16px; font-weight: bold; margin-bottom: 6px; }}
  table.frame {{ width: 100%; border-collapse: collapse; }}
  table.frame > tbody > tr > td {{ border: 1px solid #333; padding: 6px 8px;
                                   vertical-align: top; }}
  .label {{ font-weight: bold; text-transform: none; }}
  .party-name {{ font-weight: bold; }}
  .k {{ color: #333; }}
  .meta div {{ margin-bottom: 2px; }}
  .meta .num {{ font-weight: bold; }}
  table.lines {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  table.lines th, table.lines td {{ border: 1px solid #333; padding: 4px 6px;
                                    text-align: left; vertical-align: top; }}
  table.lines th {{ background: #f0f0f0; }}
  table.lines td:nth-child(4), table.lines td:nth-child(5),
  table.lines td:nth-child(6) {{ text-align: right; }}
  .total-row td {{ font-weight: bold; background: #f6f6f6; }}
  .notsale {{ font-style: italic; }}
  .sign {{ margin-top: 40px; text-align: right; padding-right: 6px; }}
</style>
</head>
<body>
  <div class="title">Delivery Challan</div>
  <table class="frame">
    <tbody>
      <tr>
        <td style="width:45%;">
          <div class="meta">
            <div>Delivery Challan No.: <span class="num">{number}</span></div>
            <div>Date of Challan: {date}</div>
          </div>
        </td>
        <td>
          <div class="label">Detail of Consignor</div>
          {consignor}
        </td>
      </tr>
      <tr>
        <td>
          <div class="label">Detail of Consignee</div>
          {consignee}
        </td>
        <td>
          <div class="label">Detail of Shipment to</div>
          {ship_to}
        </td>
      </tr>
    </tbody>
  </table>
  <table class="lines">
    <thead>
      <tr>
        <th>Sl. No.</th><th>Product Descriptions</th><th>HSN Code</th>
        <th>Qty.</th><th>Rate</th><th>Amt (incl Tax)</th>
      </tr>
    </thead>
    <tbody>
      {rows}
      <tr class="total-row">
        <td colspan="2" class="notsale">(Not for Sale)</td>
        <td style="text-align:right;">Total</td>
        <td>{total_qty}</td>
        <td></td>
        <td>{total_amount}</td>
      </tr>
    </tbody>
  </table>
  <div class="sign">Authorized Signatory</div>
</body>
</html>"""
    ).format(
        number=escape(view.number),
        date=escape(view.challan_date),
        consignor=_consignor_block(view.consignor),
        consignee=_consignee_block(view.consignee),
        ship_to=_ship_to_block(view.ship_to),
        rows=rows,
        total_qty=escape(view.total_qty),
        total_amount=total_amount,
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
