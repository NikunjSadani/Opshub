"""Billing module — the OUTBOUND (sales) side of the Project Spine.

Client invoices + credit notes are CREATED in the accounting software and UPLOADED
here (not generated): upload → reuse the expense pdfplumber `Extractor` → auto-match
extracted lines to the PO's line items → review/correct (manual fallback) → confirm.
Plus payments, advances (FIFO-suggest hybrid application), and the AR tracker.

Reuses (unchanged) `app.modules.expense.extractor` / `canonical` / `app.platform.storage`;
owns its own sales-side models (buyer/seller are inverted vs expense) + the new match
stage + the AR tables. RBAC module key: ``billing``. See `docs/plans/PROJECT-SPINE-DESIGN.md` §5e.
"""
