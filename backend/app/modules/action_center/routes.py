"""Action Center API — a single READ-ONLY computed "what needs attention" endpoint.

  GET /action-center?horizon_days=15  -> the three computed categories + counts (VIEW)

Mounted at ``/api/v1`` (so the path is ``/api/v1/action-center``). VIEW-gated on the
``action_center`` module: a user who cannot open Action Center gets a 403 before any
figure is computed. Nothing here writes — the categories are derived on read from the
existing Project-Spine data (see ``service`` for the exact rules). Money is integer paise;
``uninvoiced_qty`` is serialized as a decimal string so no precision is lost.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.action_center import service
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()

MODULE_KEY = "action_center"


def _require_view(user: User) -> None:
    """VIEW gate: opening Action Center at all (>= View)."""
    rbac.require_module(user, rbac.ACTION_CENTER)


# ------------------------------------------------------------------- schemas

class ProcurementItemOut(BaseModel):
    po_id: int
    po_number: str
    client_name: str | None
    project_code: str | None
    expected_procurement_date: date
    days_until: int


class InvoicingDueItemOut(BaseModel):
    po_id: int
    po_number: str
    client_name: str | None
    project_code: str | None
    uninvoiced_qty: str          # decimal string — no float/precision loss
    uninvoiced_value_paise: int


class ArOverdueItemOut(BaseModel):
    invoice_id: int
    invoice_number: str
    client_name: str | None
    outstanding_paise: int
    due_date: date
    days_overdue: int
    aging_bucket: str


class CountsOut(BaseModel):
    procurement: int
    invoicing_due: int
    ar_overdue: int


class ActionCenterOut(BaseModel):
    procurement: list[ProcurementItemOut]
    invoicing_due: list[InvoicingDueItemOut]
    ar_overdue: list[ArOverdueItemOut]
    counts: CountsOut


# ------------------------------------------------------------------- route

@router.get("/action-center", response_model=ActionCenterOut)
def get_action_center(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    horizon_days: Annotated[int, Query(ge=1, le=90)] = 15,
) -> ActionCenterOut:
    """The computed Action Center: procurement follow-ups, invoicing-due POs, and overdue
    receivables — each ordered most-urgent first — plus per-category counts."""
    _require_view(user)
    data = service.action_center(db, horizon_days)
    return ActionCenterOut(
        procurement=[
            ProcurementItemOut(
                po_id=it.po_id,
                po_number=it.po_number,
                client_name=it.client_name,
                project_code=it.project_code,
                expected_procurement_date=it.expected_procurement_date,
                days_until=it.days_until,
            )
            for it in data.procurement
        ],
        invoicing_due=[
            InvoicingDueItemOut(
                po_id=it.po_id,
                po_number=it.po_number,
                client_name=it.client_name,
                project_code=it.project_code,
                uninvoiced_qty=str(it.uninvoiced_qty),
                uninvoiced_value_paise=it.uninvoiced_value_paise,
            )
            for it in data.invoicing_due
        ],
        ar_overdue=[
            ArOverdueItemOut(
                invoice_id=it.invoice_id,
                invoice_number=it.invoice_number,
                client_name=it.client_name,
                outstanding_paise=it.outstanding_paise,
                due_date=it.due_date,
                days_overdue=it.days_overdue,
                aging_bucket=it.aging_bucket,
            )
            for it in data.ar_overdue
        ],
        counts=CountsOut(
            procurement=len(data.procurement),
            invoicing_due=len(data.invoicing_due),
            ar_overdue=len(data.ar_overdue),
        ),
    )
