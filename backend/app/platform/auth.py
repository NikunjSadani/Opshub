"""Authentication — Firebase (email/password).

Every request verifies the Firebase ID token SERVER-SIDE; Firebase is the sole
authority for "is this account active". A valid token whose UID has no active
`user` row is DENIED (never auto-provisioned). Authorization (role/module) is
then checked against our Postgres `user` row via app.platform.rbac.

firebase_admin is imported lazily so the skeleton boots without credentials;
`current_user` raises 401 until Firebase is configured (see .env.example).
"""
from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.platform.models import User

_firebase_ready = False


def _ensure_firebase() -> None:
    global _firebase_ready
    if _firebase_ready:
        return
    settings = get_settings()
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "firebase-admin not installed / auth not wired"
        ) from exc
    if not firebase_admin._apps:
        cred = (
            credentials.Certificate(settings.firebase_credentials_file)
            if settings.firebase_credentials_file
            else credentials.ApplicationDefault()
        )
        firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})
    _firebase_ready = True


def _verify_token(id_token: str) -> dict[str, Any]:
    _ensure_firebase()
    from firebase_admin import auth as fb_auth

    try:
        return cast(dict[str, Any], fb_auth.verify_id_token(id_token))
    except Exception as exc:  # invalid/expired/revoked
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from exc


def current_user(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """FastAPI dependency: the authenticated, ACTIVE app user (else 401)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    decoded = _verify_token(authorization.split(" ", 1)[1])
    uid = decoded.get("uid") or decoded.get("user_id")
    user = db.execute(select(User).where(User.firebase_uid == uid)).scalar_one_or_none()
    if user is None or not user.active:
        # No active RBAC row for a valid token -> denied, never auto-provisioned.
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no active account for this user")
    return user
