"""Users service — admin-managed OpsHub accounts, each assigned ONE role.

Every OpsHub user has a backing Firebase auth account (Firebase is the sole auth
authority); account creation goes through a `UserProvisioner` seam so the real
SDK call is isolated and stubbed locally. A password is **NEVER** accepted or
stored — the user sets their own via a Firebase password-setup link.

Access is governed by the assigned ROLE (see `app/platform/rbac.py`); this service
only assigns a role, it does not define permissions. Two lockout guards protect the
platform, both computed from DB truth:
  (a) the LAST active holder of the Administrator role can't be disabled or moved
      off it; and
  (b) no admin may self-disable or self-demote off Administrator (self-lockout).

Every mutation is audited inside the caller's transaction.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.modules.users.provisioner import UserProvisioner
from app.platform import audit
from app.platform.models import Role, User

logger = logging.getLogger(__name__)

# Deliberately simple (no email-validator dependency / EmailStr): non-space runs
# either side of a single '@', with a dotted domain. Good enough as an edge guard;
# Firebase is the real authority on deliverability.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_NAME_LEN = 200


class UserError(Exception):
    """Invalid users operation. The route maps this to 409 by default."""


class ValidationError(UserError):
    """Bad input — invalid email/name/role. The route maps this to 400."""


class DuplicateEmail(UserError):
    """The email is already registered (route -> 409)."""


class LockoutGuard(UserError):
    """Refused: would remove the last active admin, or self-lockout (route -> 409)."""


def _clean_email(email: str) -> str:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise ValidationError("a valid email is required")
    return email


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValidationError("name is required")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ValidationError(f"name must be at most {_MAX_NAME_LEN} characters")
    return cleaned


def _resolve_role(db: Session, role_id: int) -> Role:
    role = db.get(Role, role_id)
    if role is None:
        raise ValidationError(f"role {role_id} does not exist")
    return role


def _is_admin_role(role: Role | None) -> bool:
    """True if `role` is the protected built-in Administrator role."""
    return role is not None and role.is_system


def _other_active_admins(db: Session, exclude_user_id: int) -> int:
    """Count active users holding the Administrator role OTHER than `exclude_user_id`,
    LOCKING that row set so two concurrent demotions can't both pass the "one left"
    check and lock the org out. Selecting the rows `FOR UPDATE` (ordered) serializes
    them; on sqlite it's a no-op (single-writer already serializes)."""
    admin_role_ids = select(Role.id).where(Role.is_system.is_(True)).scalar_subquery()
    ids = db.execute(
        select(User.id)
        .where(User.role_id.in_(admin_role_ids), User.active.is_(True))
        .order_by(User.id)
        .with_for_update()
    ).scalars().all()
    return sum(1 for i in ids if i != exclude_user_id)


def list_users(db: Session) -> list[User]:
    """All users, oldest first, with their role eager-loaded."""
    return list(
        db.execute(
            select(User).options(selectinload(User.role)).order_by(User.id)
        ).scalars()
    )


def create_user(
    db: Session,
    *,
    email: str,
    name: str,
    role_id: int,
    actor_uid: str | None,
    provisioner: UserProvisioner,
) -> tuple[User, str | None]:
    """Provision a Firebase account and create the app user with the given role.

    Validates email/name and that the role exists, rejects a duplicate email (409),
    provisions a PASSWORD-LESS auth account via the seam, inserts the `User` (active),
    audits `user.created`, and returns `(user, password_setup_link | None)`.
    """
    email = _clean_email(email)
    name = _clean_name(name)
    role = _resolve_role(db, role_id)

    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise DuplicateEmail(f"email {email} is already registered")

    # Provision the (password-less) Firebase account, then persist the app user in a
    # SAGA: if ANYTHING after provisioning fails, COMPENSATE by deleting the just-created
    # auth account — otherwise a Firebase account survives with no app row and that email
    # can never be created again.
    uid = provisioner.create_auth_user(email)  # may raise ProvisionError (route -> 409)
    try:
        user = User(
            firebase_uid=uid,
            email=email,
            name=name,
            role_id=role.id,
            active=True,
        )
        db.add(user)
        db.flush()  # trips the unique email/uid backstop on a race -> IntegrityError
        audit.log(
            db,
            action="user.created",
            actor_uid=actor_uid,
            entity="user",
            entity_id=str(user.id),
            detail={"email": email, "role": role.name},
        )
        db.commit()  # own the commit so the saga covers the FULL create window
    except Exception as exc:
        # A FULL rollback (not a savepoint) reliably undoes the flushed insert on both
        # Postgres and SQLite.
        db.rollback()
        _compensate_provisioning(provisioner, uid, email)
        if isinstance(exc, IntegrityError):
            raise DuplicateEmail(f"email {email} is already registered") from exc
        raise
    # The setup link is best-effort AFTER commit: the user is already durable.
    return user, _safe_setup_link(provisioner, email)


def _compensate_provisioning(provisioner: UserProvisioner, uid: str, email: str) -> None:
    """Delete a Firebase account whose app user failed to persist (never leak an
    orphan). Best-effort: a failed compensation is LOGGED, not raised."""
    try:
        provisioner.delete_auth_user(uid)
    except Exception:  # noqa: BLE001 - compensation must not mask the real failure
        logger.exception(
            "orphaned Firebase account %s (%s) — app user failed to persist and "
            "rollback-delete also failed; manual cleanup may be required", uid, email)


def _safe_setup_link(provisioner: UserProvisioner, email: str) -> str | None:
    """Generate a password-setup link, swallowing a transient failure (-> None) so it
    can never roll back an already-committed user; the admin can re-issue the link."""
    try:
        return provisioner.password_setup_link(email)
    except Exception:  # noqa: BLE001 - link is best-effort, user is already durable
        logger.warning("password-setup link generation failed for %s", email, exc_info=True)
        return None


def update_user(
    db: Session,
    user: User,
    *,
    name: str | None = None,
    role_id: int | None = None,
    active: bool | None = None,
    actor_uid: str | None,
    acting_user: User,
) -> User:
    """Apply the provided fields to `user`; audit ONE entry per kind of change.

    Guards (raise `LockoutGuard` -> 409):
      (a) cannot disable or move-off-Administrator the LAST active Administrator; and
      (b) `acting_user` cannot self-disable or self-demote off Administrator.
    "Last active admin" is computed from the DB, not from the caller's view.
    """
    # --- validate up front (no partial mutation on bad input) ---
    new_name = _clean_name(name) if name is not None else None
    new_role = _resolve_role(db, role_id) if role_id is not None else None

    is_self = acting_user.id == user.id
    is_admin_now = _is_admin_role(user.role) and user.active
    final_role = new_role if new_role is not None else user.role
    final_active = active if active is not None else user.active
    is_admin_after = _is_admin_role(final_role) and final_active

    # (b) self-lockout — an admin may not remove their own access.
    if is_self:
        if active is False:
            raise LockoutGuard("you cannot disable your own account")
        if new_role is not None and _is_admin_role(user.role) and not _is_admin_role(new_role):
            raise LockoutGuard("you cannot remove your own Administrator role")

    # (a) last active admin — the system must always retain one Administrator.
    if (
        is_admin_now
        and not is_admin_after
        and _other_active_admins(db, exclude_user_id=user.id) == 0
    ):
        raise LockoutGuard("cannot disable or demote the last active Administrator")

    # --- apply + audit (one entry per kind of change) ---
    if new_name is not None and new_name != user.name:
        user.name = new_name
        audit.log(
            db, action="user.updated", actor_uid=actor_uid, entity="user",
            entity_id=str(user.id), detail={"name": new_name},
        )

    if new_role is not None and new_role.id != user.role_id:
        old_role_name = user.role.name if user.role is not None else None
        user.role_id = new_role.id
        user.role = new_role
        audit.log(
            db, action="user.role_changed", actor_uid=actor_uid, entity="user",
            entity_id=str(user.id), detail={"from": old_role_name, "to": new_role.name},
        )

    if active is not None and active != user.active:
        user.active = active
        audit.log(
            db, action="user.status_changed", actor_uid=actor_uid, entity="user",
            entity_id=str(user.id), detail={"active": active},
        )

    db.flush()
    return user


def setup_link(
    db: Session, user: User, *, provisioner: UserProvisioner, actor_uid: str | None = None
) -> str | None:
    """(Re)issue a password-setup link for an existing user (None if unsupported).

    Mints no persistent state in OUR DB, but (re)issuing a set-password link is an
    account-takeover-capable admin action, so it IS audited. The link itself is never
    written to the trail — only that it was issued and whether one was produced.
    """
    link = provisioner.password_setup_link(user.email)
    audit.log(
        db, action="user.setup_link_issued", actor_uid=actor_uid, entity="user",
        entity_id=str(user.id), detail={"delivered": link is not None},
    )
    return link
