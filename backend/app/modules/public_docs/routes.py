"""Public, no-login invoice viewer mounted at `/d/{token}` (NO /api prefix).

Mirrors the health module in one respect only — there is NO `Depends(current_user)`
on any route here; this surface is deliberately unauthenticated. Everything else is
locked down:

* The whole module is inert (404) unless `settings.qr_invoice_access_enabled` is on.
* An unknown token, a wrong password, an unresolvable client and an unconfigured PIN
  all return the SAME generic 404 page — a probing client cannot tell them apart
  (no enumeration oracle). The only observably-different responses happen AFTER a
  correct password (the PDF, or an "invoice not available yet" page).
* Password compare is constant-time (`hmac.compare_digest`).
* Failed attempts are throttled per token (429 after N within a cooldown window).
* Minimal self-contained HTML — no app shell, no scripts, no sensitive data on the
  unauthenticated form beyond the challan number the visitor already scanned.
"""
from __future__ import annotations

import hmac
import html
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.files.models import StoredFile
from app.modules.public_docs import service
from app.platform.storage import get_storage

router = APIRouter()

_DOWNLOAD_CHUNK = 64 * 1024


def _page(body: str, *, title: str) -> str:
    return (
        f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style=\"font-family:system-ui,sans-serif;max-width:32rem;"
        f"margin:4rem auto;padding:0 1rem\">{body}</body></html>"
    )


# A single generic body reused for every pre-auth failure so responses are
# byte-identical regardless of the underlying reason (anti-enumeration).
_GENERIC_404_HTML = _page(
    "<h1 style=\"font-size:1.25rem;text-align:center\">This link is not available</h1>"
    "<p style=\"color:#555;text-align:center\">The document you are looking for could "
    "not be found.</p>",
    title="Not available",
)
_NOT_AVAILABLE_YET_HTML = _page(
    "<h1 style=\"font-size:1.25rem\">Invoice not available yet</h1>"
    "<p style=\"color:#555\">The invoice for this delivery challan has not been "
    "published yet. Please check back later.</p>",
    title="Invoice not available yet",
)


def _generic_404() -> HTMLResponse:
    """The one and only pre-auth failure response (unknown token / wrong password /
    unconfigured PIN / unresolvable client all collapse to this identical page)."""
    return HTMLResponse(_GENERIC_404_HTML, status_code=404)


def _form_page(token: str, challan_number: str) -> str:
    safe_number = html.escape(challan_number)
    # The action posts back to the same path; the token in the URL is already known
    # to the visitor (they scanned it), so echoing it in the form action leaks nothing.
    safe_action = f"/d/{html.escape(token, quote=True)}"
    body = (
        f"<h1 style=\"font-size:1.25rem\">Delivery Challan {safe_number}</h1>"
        f"<p style=\"color:#555\">Enter the access password to view the invoice.</p>"
        f"<form method=\"post\" action=\"{safe_action}\">"
        f"<label>Password<br><input type=\"password\" name=\"password\" "
        f"autocomplete=\"off\" style=\"padding:.5rem;width:100%;max-width:20rem\"></label>"
        f"<br><br><button type=\"submit\" style=\"padding:.5rem 1rem\">View invoice</button>"
        f"</form>"
    )
    return _page(body, title=f"Challan {challan_number}")


def _enabled() -> bool:
    return get_settings().qr_invoice_access_enabled


@router.get("/d/{token}", include_in_schema=False)
def view_form(
    token: str,
    db: Annotated[Session, Depends(get_db)],
) -> HTMLResponse:
    """Render the password form for a valid token; generic 404 otherwise / when off."""
    if not _enabled():
        return _generic_404()
    challan = service.resolve_challan_by_token(db, token)
    if challan is None:
        return _generic_404()
    return HTMLResponse(_form_page(token, challan.number))


@router.post("/d/{token}", include_in_schema=False, response_model=None)
def submit_password(
    token: str,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()] = "",
) -> HTMLResponse | StreamingResponse:
    """Validate the password (constant-time) then stream the confirmed invoice PDF.

    Pre-auth failures are indistinguishable (generic 404); a locked token is 429.
    """
    if not _enabled():
        return _generic_404()

    challan = service.resolve_challan_by_token(db, token)
    if challan is None:
        return _generic_404()

    # Throttle brute force (only resolved tokens can reach here -> bounded state).
    if service.is_rate_limited(token):
        return HTMLResponse(
            _page(
                "<h1 style=\"font-size:1.25rem\">Too many attempts</h1>"
                "<p style=\"color:#555\">Please wait a few minutes and try again.</p>",
                title="Too many attempts",
            ),
            status_code=429,
            headers={"Retry-After": str(service.COOLDOWN_SECONDS)},
        )

    client = service.resolve_client_for_challan(db, challan)
    # Access-not-configured (no client, or no PIN set) is indistinguishable from a
    # wrong password: same generic 404, and we never say which. There is nothing to
    # brute-force in this state, so we don't spend a rate-limit slot on it.
    if client is None or not client.access_pin:
        return _generic_404()

    expected = (client.access_pin + challan.number).encode("utf-8")
    supplied = password.encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        service.record_failure(token)
        return _generic_404()

    # Correct password: unlock and late-bind the invoice.
    service.clear_failures(token)
    invoice = service.resolve_confirmed_invoice(db, challan)
    if invoice is None:
        return HTMLResponse(_NOT_AVAILABLE_YET_HTML)

    stored = db.execute(
        select(StoredFile).where(StoredFile.id == invoice.source_file_id)
    ).scalar_one_or_none()
    # Defense-in-depth (DUAL audit LOW): only ever stream a BILLING-module blob. source_file_id
    # is a server-set FK (an internal invariant), but this guard means a future mis-set ref can
    # never publicly serve some other module's upload as an "invoice".
    if stored is None or stored.module_key != "billing":
        # Confirmed invoice with a dangling/foreign blob ref: the visitor is already
        # authenticated, so surfacing "not available yet" is not an enumeration risk.
        return HTMLResponse(_NOT_AVAILABLE_YET_HTML)

    try:
        handle = get_storage().open(stored.storage_ref)
    except (FileNotFoundError, ValueError, NotImplementedError):
        # NotImplementedError guards a not-yet-wired backend (e.g. GcsStorage) -> degrade to
        # "not available" instead of a 500 that would leak a stack trace.
        return HTMLResponse(_NOT_AVAILABLE_YET_HTML)

    def _stream() -> Iterator[bytes]:
        try:
            while chunk := handle.read(_DOWNLOAD_CHUNK):
                yield chunk
        finally:
            handle.close()

    return StreamingResponse(
        _stream(),
        media_type="application/pdf",
        headers={"Content-Disposition": "inline; filename=\"invoice.pdf\""},
    )
