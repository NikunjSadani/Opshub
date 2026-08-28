"""Finance (P&L) HTTP surface (mounted at `/api/v1`, module key `finance`).

  GET /finance/pnl/projects        -> per-project P&L (filters: client_id, date range)
  GET /finance/pnl/projects.csv    -> the same, as a CSV/Excel export
  GET /finance/pnl/projects/{id}   -> one project's P&L breakdown + identity
  GET /finance/pnl/consolidated    -> consolidated totals + the general-bucket line

READ-ONLY. Every route is VIEW-gated on the ``finance`` module (``pnl.view`` = View),
so a user who cannot open Finance gets a 403 before any figure is computed. Money is
returned as integer paise (net-of-GST taxable); the CSV renders 2dp rupees.
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.finance import service
from app.modules.projects.models import Project, ProjectClient
from app.platform import rbac
from app.platform.auth import current_user
from app.platform.models import User

router = APIRouter()

MODULE_KEY = "finance"


def _require_view(user: User) -> None:
    """VIEW gate: opening Finance at all (>= View) is exactly ``pnl.view``."""
    rbac.require_module(user, MODULE_KEY)


# ------------------------------------------------------------------- schemas

class PnlLineOut(BaseModel):
    revenue_paise: int
    cost_paise: int
    margin_paise: int
    margin_pct: float | None


class ProjectPnlOut(BaseModel):
    project_id: int
    project_code: str
    project_name: str
    client_id: int
    client_code: str
    client_name: str
    revenue_paise: int
    cost_paise: int
    margin_paise: int
    margin_pct: float | None


class ProjectPnlDetailOut(BaseModel):
    project_id: int
    project_code: str
    project_name: str
    client_id: int
    client_code: str
    client_name: str
    pnl: PnlLineOut


class ConsolidatedOut(BaseModel):
    projects: list[ProjectPnlOut]
    general_bucket: PnlLineOut
    # PO-less confirmed invoices/CNs with no direct project. Distinct from general_bucket
    # (GEN overhead). Excluded from the project rows, folded into totals so they reconcile.
    unattributed: PnlLineOut
    totals: PnlLineOut


def _line_out(line: service.PnlLine) -> PnlLineOut:
    return PnlLineOut(
        revenue_paise=line.revenue_paise,
        cost_paise=line.cost_paise,
        margin_paise=line.margin_paise,
        margin_pct=line.margin_pct,
    )


def _project_out(row: service.ProjectPnl) -> ProjectPnlOut:
    return ProjectPnlOut(
        project_id=row.project_id,
        project_code=row.project_code,
        project_name=row.project_name,
        client_id=row.client_id,
        client_code=row.client_code,
        client_name=row.client_name,
        revenue_paise=row.revenue_paise,
        cost_paise=row.cost_paise,
        margin_paise=row.margin_paise,
        margin_pct=row.margin_pct,
    )


# ------------------------------------------------------------------- routes

@router.get("/finance/pnl/projects", response_model=list[ProjectPnlOut])
def pnl_projects(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> list[ProjectPnlOut]:
    """Per-project P&L (the general/overhead bucket is excluded — it shows only in the
    consolidated view). Optional client + inclusive document-date-range filters."""
    _require_view(user)
    rows = service.pnl_by_project(
        db, service.PnlFilters(client_id=client_id, date_from=date_from, date_to=date_to)
    )
    return [_project_out(r) for r in rows]


@router.get("/finance/pnl/projects.csv")
def pnl_projects_csv(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
    client_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> Response:
    """Export the filtered per-project P&L as CSV (CSV-injection-guarded, 2dp rupees)."""
    _require_view(user)
    rows = service.pnl_by_project(
        db, service.PnlFilters(client_id=client_id, date_from=date_from, date_to=date_to)
    )
    return Response(
        content=service.pnl_csv(rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pnl-by-project.csv"'},
    )


@router.get("/finance/pnl/consolidated", response_model=ConsolidatedOut)
def pnl_consolidated(
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ConsolidatedOut:
    """Consolidated P&L: per-project rows, the general-bucket line, the unattributed line
    (PO-less confirmed invoices with no project), and the totals over every bucket
    (totals == Σ(project rows) + general bucket + unattributed)."""
    _require_view(user)
    data = service.consolidated_pnl(db)
    return ConsolidatedOut(
        projects=[_project_out(r) for r in data.projects],
        general_bucket=_line_out(data.general_bucket),
        unattributed=_line_out(data.unattributed),
        totals=_line_out(data.totals),
    )


@router.get("/finance/pnl/projects/{project_id}", response_model=ProjectPnlDetailOut)
def pnl_project_detail(
    project_id: int,
    user: Annotated[User, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ProjectPnlDetailOut:
    """One project's P&L breakdown + its identity. 404 if the project does not exist."""
    _require_view(user)
    row = db.execute(
        select(Project, ProjectClient)
        .join(ProjectClient, ProjectClient.id == Project.client_id)
        .where(Project.id == project_id)
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    project, client = row
    line = service.project_pnl(db, project_id)
    return ProjectPnlDetailOut(
        project_id=project.id,
        project_code=project.code,
        project_name=project.name,
        client_id=client.id,
        client_code=client.code,
        client_name=client.name,
        pnl=_line_out(line),
    )
