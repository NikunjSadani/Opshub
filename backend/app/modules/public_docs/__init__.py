"""Public (no-login) challan-QR invoice viewer.

A single self-contained wall behind `settings.qr_invoice_access_enabled`: a QR on a
delivery challan links to `/d/{access_token}`; the recipient enters a password
(the client's PIN + the challan number) to view the matching confirmed client
invoice PDF. No auth, no app shell, no enumeration hints. Off by default (404).
"""
