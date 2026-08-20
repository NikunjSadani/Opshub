"""Guarded, idempotent seed — an initial Admin + demo users/settings.

FAIL-CLOSED for prod: refuses to run when env=prod. Data seeding is a dev/staging
convenience; prod users come from real Firebase + the admin User-Management screen.
Run in a container via `python -m app.seed` (no compile step — it's pure Python).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.modules.numbering import service as numbering
from app.modules.numbering.models import NumberingCounter
from app.modules.projects import service as projects
from app.modules.projects.models import Project, ProjectClient
from app.platform import audit
from app.platform.models import Role, Setting, User
from app.platform.roles_builtin import ensure_builtin_roles

# The catch-all project code + name for overhead expenses with no client project.
OVERHEAD_CLIENT_CODE = "GEN"
OVERHEAD_PROJECT_NAME = "General / Overhead"


def assert_seedable() -> None:
    """Fail closed: never seed a prod database."""
    if get_settings().is_prod:
        raise RuntimeError("refusing to seed a prod database")


def _ensure_user(
    db: Session,
    *,
    uid: str,
    email: str,
    name: str,
    role: Role,
) -> None:
    if db.execute(select(User).where(User.firebase_uid == uid)).scalar_one_or_none() is not None:
        return
    user = User(firebase_uid=uid, email=email, name=name, role_id=role.id, active=True)
    db.add(user)
    db.flush()
    audit.log(
        db,
        action="seed.user",
        actor_uid="seed",
        entity="user",
        entity_id=str(user.id),
        detail={"role": role.name},
    )


def seed(db: Session) -> None:
    """Idempotent: safe to run repeatedly (no duplicates)."""
    assert_seedable()
    roles = ensure_builtin_roles(db)
    # Dev users, one per access shape, so every level is exercisable locally + in E2E.
    _ensure_user(db, uid="dev-admin", email="admin@opshub.local", name="Dev Admin",
                 role=roles["Administrator"])
    _ensure_user(db, uid="dev-manager", email="manager@opshub.local", name="Dev Challan Manager",
                 role=roles["Challan Manager"])
    _ensure_user(db, uid="dev-operator", email="operator@opshub.local", name="Dev Challan Operator",
                 role=roles["Challan Operator"])
    _ensure_user(db, uid="dev-viewer", email="viewer@opshub.local", name="Dev Viewer",
                 role=roles["Viewer"])
    # A default config setting (config, never secrets — those go to Secret Manager).
    if db.get(Setting, "eway_threshold") is None:
        db.add(Setting(key="eway_threshold", value={"amount": 50000}, updated_by="seed"))
    # PLACEHOLDER challan counter: series "L", current FY, high-water 0 (first issue
    # would be L/000001). The owner sets the REAL last number before go-live via the
    # admin mode-2 seed (POST /numbering/seed). Idempotent.
    _seed_placeholder_counter(db, series="L")
    _seed_overhead_project(db)
    db.commit()


def ensure_overhead_project(db: Session) -> Project:
    """Idempotently ensure the catch-all "General / Overhead" project exists.

    Inc 27 requires a Project on every expense, so overhead with no client project
    (rent, utilities, software) needs a home. Kept as a standalone helper so a future
    prod bootstrap can create it too (seed itself is fail-closed on prod). Returns the
    project row. Caller commits.
    """
    client = db.execute(
        select(ProjectClient).where(ProjectClient.code == OVERHEAD_CLIENT_CODE)
    ).scalar_one_or_none()
    if client is None:
        client = projects.create_client(
            db, name="General", code=OVERHEAD_CLIENT_CODE, actor_uid="seed")
        db.flush()
    # create_project is idempotent on (client_id, name), so this never mints a duplicate.
    return projects.create_project(
        db, client_id=client.id, name=OVERHEAD_PROJECT_NAME, actor_uid="seed")


def _seed_overhead_project(db: Session) -> None:
    ensure_overhead_project(db)


def _seed_placeholder_counter(db: Session, *, series: str) -> None:
    fy = numbering.current_fy()
    exists = db.execute(
        select(NumberingCounter).where(
            NumberingCounter.series == series, NumberingCounter.fy == fy
        )
    ).scalar_one_or_none()
    if exists is not None:
        return
    db.add(NumberingCounter(series=series, fy=fy, last_number=0))
    db.flush()
    audit.log(
        db,
        action="seed.numbering_counter",
        actor_uid="seed",
        entity="numbering_counter",
        entity_id=f"{series}/{fy}",
        detail={"last_number": 0, "placeholder": True},
    )


def main() -> None:  # pragma: no cover - container entrypoint
    with SessionLocal() as db:
        seed(db)
    print("seed complete")


if __name__ == "__main__":  # pragma: no cover
    main()
