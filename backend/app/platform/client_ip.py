"""Best-effort client-IP extraction, shared across platform surfaces.

Behind our Cloudflare worker the real visitor IP arrives in `x-client-ip`; a
generic proxy hop lands in `x-forwarded-for`; the socket peer is the last resort.
Mirrors the challan invoice-viewer helper (kept as its own per-module copy) so
the login-event recorder does not depend on the challan module.
"""
from __future__ import annotations

from typing import Any


def client_ip_from_request(request: Any) -> str | None:  # noqa: ANN401
    """Best-effort client IP: our Cloudflare worker's `x-client-ip`, else the first
    hop of `x-forwarded-for`, else the socket peer. None when nothing is available.
    Every candidate is truncated to 64 chars to fit the audit/login-event columns."""
    headers = getattr(request, "headers", None)
    if headers is not None:
        direct = (headers.get("x-client-ip") or "").strip()
        if direct:
            return direct[:64]
        forwarded = headers.get("x-forwarded-for") or ""
        first_hop = forwarded.split(",")[0].strip()
        if first_hop:
            return first_hop[:64]
    peer = getattr(request, "client", None)
    host = getattr(peer, "host", None)
    if host:
        return str(host).strip()[:64] or None
    return None
