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
from app.platform import audit
from app.platform.models import Role, Setting, User, UserModuleAccess


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
    modules: list[str] | None = None,
) -> None:
    if db.execute(select(User).where(User.firebase_uid == uid)).scalar_one_or_none() is not None:
        return
    user = User(firebase_uid=uid, email=email, name=name, role=role, active=True)
    user.module_access = [UserModuleAccess(module_key=m) for m in (modules or [])]
    db.add(user)
    db.flush()
    audit.log(
        db,
        action="seed.user",
        actor_uid="seed",
        entity="user",
        entity_id=str(user.id),
        detail={"role": role.value},
    )


def seed(db: Session) -> None:
    """Idempotent: safe to run repeatedly (no duplicates)."""
    assert_seedable()
    # Initial Admin (sees all modules via the Admin bypass — no explicit grants needed).
    _ensure_user(db, uid="dev-admin", email="admin@opshub.local", name="Dev Admin", role=Role.ADMIN)
    # A non-admin MIS user with an EXPLICIT per-user module grant (proves the model).
    _ensure_user(
        db,
        uid="dev-mis",
        email="mis@opshub.local",
        name="Dev MIS",
        role=Role.MIS,
        modules=["document_automation"],
    )
    # A default config setting (config, never secrets — those go to Secret Manager).
    if db.get(Setting, "eway_threshold") is None:
        db.add(Setting(key="eway_threshold", value={"amount": 50000}, updated_by="seed"))
    db.commit()


def main() -> None:  # pragma: no cover - container entrypoint
    with SessionLocal() as db:
        seed(db)
    print("seed complete")


if __name__ == "__main__":  # pragma: no cover
    main()
