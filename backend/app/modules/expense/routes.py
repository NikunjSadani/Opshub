"""Expense/Invoice HTTP surface (mounted at `/api/v1`, module key `expense_invoice`).

Endpoints are built out by the service layer:
  POST   /expense/invoices             -> bulk upload N PDFs -> extract -> per-file outcome
  GET    /expense/invoices[.csv]       -> searchable register + CSV export
  GET    /expense/invoices/{id}        -> canonical record + fields + line items
  GET/PATCH /expense/invoices/{id}/reviews -> per-field low-confidence corrections
  DELETE /expense/invoices/{id}        -> delete (supports delete-and-re-upload on a dup 409)

Every route is gated on `can_access_module(MODULE_KEY)`; every mutation is audited.
"""
from fastapi import APIRouter

router = APIRouter()

MODULE_KEY = "expense_invoice"
