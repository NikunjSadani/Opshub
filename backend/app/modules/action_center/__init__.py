"""Action Center — computed "what needs attention" reminders (read-only, no infra).

Computes three categories ON READ from existing data (no tables, no scheduler):
* procurement follow-up — POs whose expected_procurement_date is within T-15 days,
* invoicing due — POs with units still to invoice (open_to_invoice > 0),
* AR overdue — CONFIRMED client invoices past due_date, with aging.

RBAC module key: ``action_center`` (VIEW). See docs/plans/PROJECT-SPINE-DESIGN.md §8.
The push/scheduled-email version is deferred to the owner-gated cloud wave.
"""
