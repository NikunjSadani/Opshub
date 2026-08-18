"""LOCAL-ONLY E2E master-data bootstrap.

Seeds the consignor, HSN, and projects that the challan upload TEMPLATE's example
rows reference (BRI-001 / BRI-002, HSN 1509), so the Playwright harness can drive
the full upload -> validate -> generate -> register -> void flow against the real
backend. Idempotent. Fail-closed: refuses to run outside a local env (it is never
shipped to staging/prod, where real master data is entered via the admin screens).

Run after `app.seed`:  python -m app.e2e_bootstrap
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.modules.masterdata.models import Consignor, HsnCode
from app.modules.projects import service as projects
from app.modules.projects.models import ProjectClient


def assert_local() -> None:
    if get_settings().env != "local":
        raise RuntimeError("e2e_bootstrap is local-only (refusing non-local env)")


def bootstrap(db: Session) -> None:
    """Idempotent: safe to run repeatedly (no duplicates)."""
    assert_local()
    if db.execute(select(Consignor).where(Consignor.active.is_(True))).first() is None:
        db.add(Consignor(
            name="Tech Gifsy Solutions Limited", gstin="27AAAAA0000A1Z5",
            state="Maharashtra", address="Howrah warehouse", phone="", active=True))
    if db.execute(select(HsnCode).where(HsnCode.hsn == "1509")).scalar_one_or_none() is None:
        db.add(HsnCode(hsn="1509", description="Olive oil", gst_rate=Decimal("5"), active=True))
    db.flush()

    client = db.execute(
        select(ProjectClient).where(ProjectClient.code == "BRI")
    ).scalar_one_or_none()
    if client is None:
        client = projects.create_client(db, name="Britannia", code="BRI", actor_uid="e2e")
    # create_project is idempotent on (client_id, name); the first two mint BRI-001/002.
    for name in ("Rewards Alpha", "Rewards Beta"):
        projects.create_project(db, client_id=client.id, name=name, actor_uid="e2e")
    db.commit()


def main() -> None:  # pragma: no cover - E2E entrypoint
    with SessionLocal() as db:
        bootstrap(db)
    print("e2e bootstrap complete")


if __name__ == "__main__":  # pragma: no cover
    main()
