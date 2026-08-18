"""User provisioner seam — isolates the Firebase Admin create-user call.

Creating an OpsHub user MUST create a backing Firebase Auth account (Firebase is
the sole auth authority), but we **NEVER** accept or store a password: the account
is created password-less and the user sets their own via a Firebase
password-setup (reset) link.

`FirebaseProvisioner` wraps the real SDK (credentials init the same way as
`app.platform.auth`); `LocalProvisioner` is a no-network stub used in local
dev / when Firebase isn't configured — mirroring auth.py's dev-auth guard so
behaviour matches — so the suite and the local SPA run without credentials.
"""
from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from app.config import Settings


class ProvisionError(Exception):
    """A user's Firebase auth account could not be provisioned (e.g. the email
    already exists in Firebase). The route maps this to 409."""


class UserProvisioner(Protocol):
    """The seam the service depends on. Implementations NEVER handle a password."""

    def create_auth_user(self, email: str) -> str:
        """Create the auth account for `email`, returning its firebase uid."""
        ...

    def delete_auth_user(self, uid: str) -> None:
        """Delete the auth account `uid` (compensating rollback for a failed create).
        Best-effort + idempotent: a not-found account is not an error."""
        ...

    def password_setup_link(self, email: str) -> str | None:
        """A link the user follows to set their own password (None if unsupported)."""
        ...


class LocalProvisioner:
    """No-network stub: mints a `local:<uuid>` uid and issues no setup link.

    Used in local dev / when Firebase isn't configured, so nothing touches the
    real SDK. The `local:` uid prefix is how the API flags a user as not yet
    backed by a real Firebase account (`is_provisioned`).
    """

    def create_auth_user(self, email: str) -> str:
        return f"local:{uuid4().hex}"

    def delete_auth_user(self, uid: str) -> None:
        return None  # no real account was created

    def password_setup_link(self, email: str) -> str | None:
        return None


class FirebaseProvisioner:
    """Real provisioner — creates a PASSWORD-LESS Firebase account and returns its uid."""

    def create_auth_user(self, email: str) -> str:
        from app.platform import auth as platform_auth

        platform_auth._ensure_firebase()  # same credential init as request auth
        from firebase_admin import auth as fb_auth

        try:
            record = fb_auth.create_user(email=email)  # NEVER a password
        except Exception as exc:  # noqa: BLE001 - narrowed by name below
            # firebase_admin has no type stubs; match the already-exists error by
            # class name so this stays mypy-clean and robust across SDK versions.
            if type(exc).__name__ == "EmailAlreadyExistsError":
                raise ProvisionError(
                    f"a Firebase account already exists for {email}"
                ) from exc
            raise
        return str(record.uid)

    def delete_auth_user(self, uid: str) -> None:
        """Delete a provisioned account (compensating rollback). Idempotent: a
        UserNotFoundError is swallowed so a double-compensation can't raise."""
        from app.platform import auth as platform_auth

        platform_auth._ensure_firebase()
        from firebase_admin import auth as fb_auth

        try:
            fb_auth.delete_user(uid)
        except Exception as exc:  # noqa: BLE001 - not-found is a successful no-op
            if type(exc).__name__ == "UserNotFoundError":
                return
            raise

    def password_setup_link(self, email: str) -> str | None:
        from app.platform import auth as platform_auth

        platform_auth._ensure_firebase()
        from firebase_admin import auth as fb_auth

        return str(fb_auth.generate_password_reset_link(email))


def get_provisioner(settings: Settings) -> UserProvisioner:
    """Select the provisioner.

    The `LocalProvisioner` stub is used ONLY in a **local** env — either with the
    dev-auth shim active (`dev_auth`, matching `app.platform.auth`) or when Firebase
    isn't configured locally — so the suite and local dev never touch the real SDK.

    A **non-local** env (staging/prod) ALWAYS uses the real `FirebaseProvisioner`,
    even if `firebase_project_id` is unset: a missing config there is a deploy error
    that must fail LOUD, never silently mint un-loginable `local:` users (which would
    diverge from `auth`, that uses real Firebase whenever env != local).
    """
    if settings.env == "local" and (settings.dev_auth or not settings.firebase_project_id):
        return LocalProvisioner()
    return FirebaseProvisioner()
