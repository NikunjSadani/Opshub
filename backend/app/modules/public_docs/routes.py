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

import contextlib
import hmac
import html
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.modules.challan import invoice_access
from app.modules.challan.models import AccessOutcome, Challan
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
    """The 'not available' page for an UNKNOWN/invalid token or a disabled feature —
    identical to what the GET form already returns for those, so it reveals nothing new.
    A wrong password or unconfigured PIN on a VALID token does NOT land here: those
    re-render the password form with a clear error (see `_wrong_password`), and are
    byte-identical to each other so the two stay indistinguishable."""
    return HTMLResponse(_GENERIC_404_HTML, status_code=404)


def _form_page(token: str, challan_number: str, error: str = "") -> str:
    safe_number = html.escape(challan_number)
    # The action posts back to the same path; the token in the URL is already known
    # to the visitor (they scanned it), so echoing it in the form action leaks nothing.
    safe_action = f"/d/{html.escape(token, quote=True)}"
    error_html = (
        f"<p style=\"color:#b00020;font-weight:600\" role=\"alert\">{html.escape(error)}</p>"
        if error
        else ""
    )
    body = (
        f"<h1 style=\"font-size:1.25rem\">Delivery Challan {safe_number}</h1>"
        f"<p style=\"color:#555\">Enter the access password to view the invoice.</p>"
        f"{error_html}"
        f"<form method=\"post\" action=\"{safe_action}\">"
        f"<label>Password<br><input type=\"password\" name=\"password\" "
        f"autocomplete=\"off\" style=\"padding:.5rem;width:100%;max-width:20rem\"></label>"
        f"<br><br><button type=\"submit\" style=\"padding:.5rem 1rem\">View invoice</button>"
        f"</form>"
    )
    return _page(body, title=f"Challan {challan_number}")


# A valid token whose password submission fails — a WRONG PIN, or a client with NO PIN
# configured — re-renders the SAME password form with the SAME message, so the two remain
# byte-identical (an attacker still can't tell "wrong PIN" from "no PIN set"). Unknown
# tokens still collapse to the generic 404, exactly as the GET form already does, so token
# validity is no more discoverable than before. This replaces the old "This link is not
# available" page, which misled a legitimate visitor who simply mistyped their password.
_WRONG_PASSWORD_MSG = "Incorrect password. Please check it and try again."


def _wrong_password(token: str, challan_number: str) -> HTMLResponse:
    return HTMLResponse(
        _form_page(token, challan_number, error=_WRONG_PASSWORD_MSG),
        status_code=401,
    )


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


def _log_access(
    db: Session,
    request: Request,
    challan: Challan,
    outcome: str,
) -> None:
    """Best-effort audit-log write for one PIN submission (resolved token only).

    Wrapped so a logging failure NEVER changes the visitor's response: any error
    (including record_access being patched to raise) is swallowed. Commits the row
    so it survives independent of the response path.

    Client attribution ALWAYS resolves via `resolve_client_id` (challan -> project ->
    client, ANY project status) so on-hold/closed-project challans attribute
    consistently across every outcome — never the caller's ACTIVE-only client."""
    try:
        client_id = invoice_access.resolve_client_id(db, challan)
        viewer_hash = invoice_access.hash_ip(
            invoice_access.client_ip_from_request(request)
        )
        invoice_access.record_access(
            db, challan=challan, client_id=client_id,
            outcome=outcome, viewer_hash=viewer_hash,
        )
        db.commit()
    except Exception:
        with contextlib.suppress(Exception):
            db.rollback()


@router.post("/d/{token}", include_in_schema=False, response_model=None)
def submit_password(
    token: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    password: Annotated[str, Form()] = "",
) -> HTMLResponse | StreamingResponse:
    """Validate the password (constant-time) then stream the confirmed invoice PDF.

    Pre-auth failures are indistinguishable (generic 404); a locked token is 429.
    Every outcome on a RESOLVED token is recorded to the invoice-access audit log
    (best-effort — logging never alters the response).
    """
    if not _enabled():
        return _generic_404()

    challan = service.resolve_challan_by_token(db, token)
    if challan is None:
        return _generic_404()

    # Throttle brute force (only resolved tokens can reach here -> bounded state).
    if service.is_rate_limited(token):
        _log_access(db, request, challan, AccessOutcome.RATE_LIMITED)
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
    # Access-not-configured (no client, or no PIN set) is indistinguishable from a WRONG
    # password: both re-render the SAME password form with the SAME message (byte-identical),
    # AND advance the SAME throttle so both flip to 429 after N attempts. If this state
    # skipped the throttle, "never reaches 429" vs "does" would leak whether a PIN is set.
    if client is None or not client.access_pin:
        service.record_failure(token)
        _log_access(db, request, challan, AccessOutcome.NO_PIN)
        return _wrong_password(token, challan.number)

    expected = (client.access_pin + challan.number).encode("utf-8")
    supplied = password.encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        service.record_failure(token)
        _log_access(db, request, challan, AccessOutcome.WRONG_PIN)
        return _wrong_password(token, challan.number)

    # Correct password: unlock and late-bind the invoice.
    service.clear_failures(token)
    invoice = service.resolve_confirmed_invoice(db, challan)
    if invoice is None:
        _log_access(db, request, challan, AccessOutcome.NOT_AVAILABLE)
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
        _log_access(db, request, challan, AccessOutcome.NOT_AVAILABLE)
        return HTMLResponse(_NOT_AVAILABLE_YET_HTML)

    try:
        handle = get_storage().open(stored.storage_ref)
    except (FileNotFoundError, ValueError, NotImplementedError):
        # NotImplementedError guards a not-yet-wired backend (e.g. GcsStorage) -> degrade to
        # "not available" instead of a 500 that would leak a stack trace.
        _log_access(db, request, challan, AccessOutcome.NOT_AVAILABLE)
        return HTMLResponse(_NOT_AVAILABLE_YET_HTML)

    # Record the successful view BEFORE returning (the StreamingResponse body is lazy).
    _log_access(db, request, challan, AccessOutcome.VIEWED)

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
