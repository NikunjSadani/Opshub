"""Render + packaging layer for delivery challans.

Turns a fully-resolved, ORM-free `ChallanView` (see `schema.py`) into a faithful
half-A4 (A5-landscape) business-document HTML, then into PDF bytes, and packages
many of those into a single ZIP or a merged PDF for a batch download. Designing on
the half-A4 canvas lets `merge_2up` stack two challans per A4 sheet at 100% (no
shrink-to-fit), so the printed text stays full size.

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
        _kv("Enterprise Name -", s.enterprise),
        _kv("Address -", s.address),
        _kv("Number -", s.phone),
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


def _qr_markup(access_token: str) -> Markup:
    """Inline-SVG QR encoding the public invoice-viewer URL, or empty Markup when the
    feature is DORMANT — no token minted OR no `public_base_url` configured. Emitted as
    INLINE SVG (not an <img src>), so WeasyPrint's data-URL-only fetcher never sees an
    external resource request. `get_settings` is imported locally (mirrors `get_renderer`)
    and any config-access failure degrades to no-QR rather than hard-failing the render."""
    if not access_token:
        return Markup("")
    try:
        from app.config import get_settings

        base = get_settings().public_base_url
    except Exception:  # noqa: BLE001 - a config hiccup must never break document render
        return Markup("")
    if not base:
        return Markup("")
    import segno

    url = f"{base.rstrip('/')}/d/{access_token}"
    # omitsize -> the SVG carries a viewBox (no fixed px width/height), so CSS scales it
    # to the ~15mm corner box; border=2 keeps the statutory quiet zone for scannability.
    svg = segno.make(url, error="m").svg_inline(border=2, omitsize=True, svgclass="qrimg")
    return Markup(f"<div class='qr'>{svg}</div>")


def build_challan_html(view: ChallanView) -> str:
    """Build a complete standalone half-A4 (A5-landscape) HTML document for one
    challan (faithful to the `L/433` layout, re-fitted to the half-page canvas).
    Pure function, no I/O; every interpolated value is HTML-escaped, so untrusted
    Excel cell text renders as inert data."""
    rows = Markup("").join(_line_row(line, view.show_amount) for line in view.lines)
    qr = _qr_markup(view.access_token)
    total_amount = escape(view.total_amount) if view.show_amount else Markup("")
    invoice = (
        Markup(f"<div>Invoice No.: {escape(view.invoice_number)}</div>")
        if view.invoice_number
        else Markup("")
    )
    po = (
        Markup(f"<div>PO No.: {escape(view.po_number)}</div>")
        if view.po_number
        else Markup("")
    )

    document = Markup(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Delivery Challan {number}</title>
<style>
  @page {{ size: A5 landscape; margin: 8mm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, "Helvetica Neue", sans-serif; font-size: 9px;
         color: #111; margin: 0; }}
  .title {{ text-align: center; font-size: 13px; font-weight: bold; margin-bottom: 4px; }}
  table.frame {{ width: 100%; border-collapse: collapse; }}
  table.frame > tbody > tr > td {{ border: 1px solid #333; padding: 3px 5px;
                                   vertical-align: top; }}
  .label {{ font-weight: bold; text-transform: none; }}
  .party-name {{ font-weight: bold; }}
  .k {{ color: #333; }}
  .meta div {{ margin-bottom: 1px; }}
  .meta .num {{ font-weight: bold; }}
  .qr {{ float: right; width: 15mm; margin: 0 0 2px 4px; }}
  .qr svg {{ display: block; width: 15mm; height: 15mm; }}
  table.lines {{ width: 100%; border-collapse: collapse; margin-top: 5px; }}
  table.lines th, table.lines td {{ border: 1px solid #333; padding: 2px 4px;
                                    text-align: left; vertical-align: top; }}
  table.lines th {{ background: #f0f0f0; }}
  table.lines td:nth-child(4), table.lines td:nth-child(5),
  table.lines td:nth-child(6) {{ text-align: right; }}
  .total-row td {{ font-weight: bold; background: #f6f6f6; }}
  .notsale {{ font-style: italic; }}
  .sign {{ margin-top: 14px; text-align: right; padding-right: 6px; }}
</style>
</head>
<body>
  <div class="title">Delivery Challan</div>
  <table class="frame">
    <tbody>
      <tr>
        <td style="width:45%;">
          {qr}
          <div class="meta">
            {invoice}
            <div>Delivery Challan No.: <span class="num">{number}</span></div>
            {po}
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
        qr=qr,
        invoice=invoice,
        po=po,
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


class StubRenderer:
    """A native-free `Renderer` for LOCAL dev / the E2E harness only.

    WeasyPrint is container-only, so it can't render on a Windows dev box. This
    emits a minimal but VALID single-page PDF (via pypdf, already a dependency) so
    the full generate -> issue -> register -> void lifecycle can be exercised
    locally without the native library. It is NEVER selected in a non-local env
    (see `get_renderer`), so production always renders the faithful document.
    """

    def render_pdf(self, html: str) -> bytes:
        from pypdf import PdfWriter  # lazy, but always installed (used by merge_pdfs)

        writer = PdfWriter()
        writer.add_blank_page(width=595, height=421)  # A5 landscape (half-A4) points
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()


def get_renderer() -> Renderer:
    """Select the challan renderer. The `StubRenderer` is used ONLY in a local env
    with `stub_render` enabled (double-guarded, mirroring the dev-auth shim); every
    other env — staging/prod — ALWAYS uses `WeasyPrintRenderer` for the real PDF."""
    from app.config import get_settings

    settings = get_settings()
    if settings.env == "local" and settings.stub_render:
        return StubRenderer()
    return WeasyPrintRenderer()


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


# A4 portrait in PDF points — the OUTPUT sheet size for two-up compositing. Each
# source challan is half this height (A5 landscape 595x421, per @page + StubRenderer),
# so it drops into a slot at scale 1.0 (no shrink).
_A4_W_PT = 595.0
_A4_H_PT = 842.0


def merge_2up(pdfs: list[bytes]) -> bytes:
    """Composite challan PDFs TWO-UP — 2 source pages per portrait-A4 output sheet,
    each uniformly scaled to fit the top / bottom half and centred. Halves paper usage
    on a reprint. Pure `pypdf` page compositing of the ALREADY-rendered stored challans
    (no re-render / WeasyPrint), so it runs anywhere. Source pages are laid out in order;
    a multi-page challan simply consumes consecutive half-slots. Raises on an empty list.
    """
    if not pdfs:
        raise ValueError("merge_2up requires at least one PDF")

    from pypdf import PageObject, PdfReader, PdfWriter, Transformation  # lazy

    src_pages = []
    for pdf in pdfs:
        for page in PdfReader(io.BytesIO(pdf)).pages:
            src_pages.append(page)

    slot_h = _A4_H_PT / 2  # two stacked half-A4 slots per portrait sheet
    writer = PdfWriter()
    for i in range(0, len(src_pages), 2):
        sheet = PageObject.create_blank_page(width=_A4_W_PT, height=_A4_H_PT)
        for j, src in enumerate(src_pages[i:i + 2]):
            sw = float(src.mediabox.width) or _A4_W_PT
            sh = float(src.mediabox.height) or _A4_H_PT
            scale = min(_A4_W_PT / sw, slot_h / sh)  # fit the slot, preserve aspect
            slot_bottom = _A4_H_PT - slot_h * (j + 1)  # j=0 -> top half, j=1 -> bottom half
            tx = (_A4_W_PT - sw * scale) / 2
            ty = slot_bottom + (slot_h - sh * scale) / 2
            sheet.merge_transformed_page(src, Transformation().scale(scale).translate(tx, ty))
        writer.add_page(sheet)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
