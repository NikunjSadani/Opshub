"""One-off PROD bootstrap: create the FIRST Administrator so the owner can log in.

`app.seed` is fail-closed on prod (dev/staging only). This is the sanctioned prod path to mint
the first admin — after which everyone else is managed through the in-app User-Management screen.
It runs as an in-VPC Cloud Run job (the prod database is private-IP). Idempotent.

What it does (reads BOOTSTRAP_ADMIN_EMAIL, required; BOOTSTRAP_ADMIN_NAME, optional):
  1. ensure the built-in roles exist (incl. Administrator);
  2. create a PASSWORD-LESS Firebase account for the email (or reuse an existing one);
  3. link it to an ACTIVE Administrator `user` row;
  4. print a password-setup link the admin follows to set their own password, then sign in.
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.modules.users.provisioner import ProvisionError, get_provisioner
from app.platform import audit
from app.platform.models import User
from app.platform.roles_builtin import ensure_builtin_roles


def bootstrap_admin(email: str, name: str) -> None:
    settings = get_settings()
    provisioner = get_provisioner(settings)
    with SessionLocal() as db:
        roles = ensure_builtin_roles(db)
        admin_role = roles["Administrator"]

        # Firebase account: create a password-less one, or reuse an existing account.
        try:
            uid = provisioner.create_auth_user(email)
        except ProvisionError:
            from app.platform import auth as platform_auth

            platform_auth._ensure_firebase()
            from firebase_admin import auth as fb_auth

            uid = str(fb_auth.get_user_by_email(email).uid)

        existing = db.execute(
            select(User).where(User.firebase_uid == uid)
        ).scalar_one_or_none()
        if existing is None:
            user = User(
                firebase_uid=uid, email=email, name=name, role_id=admin_role.id, active=True
            )
            db.add(user)
            db.flush()
            audit.log(
                db,
                action="bootstrap.admin",
                actor_uid="bootstrap",
                entity="user",
                entity_id=str(user.id),
                detail={"role": "Administrator", "email": email},
            )
            db.commit()
            print(f"Created Administrator user for {email} (uid={uid}).")
        else:
            print(f"Administrator user already exists for {email} (uid={uid}).")

        link = provisioner.password_setup_link(email)
        print("\n=== PASSWORD SETUP LINK (open this to set your password, then sign in) ===")
        print(link or "(no link available — set the password in the Firebase console)")


def main() -> None:  # pragma: no cover - container entrypoint
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    name = os.environ.get("BOOTSTRAP_ADMIN_NAME", "").strip() or "Administrator"
    if not email:
        print("BOOTSTRAP_ADMIN_EMAIL is required", file=sys.stderr)
        raise SystemExit(2)
    bootstrap_admin(email, name)


if __name__ == "__main__":  # pragma: no cover
    main()
