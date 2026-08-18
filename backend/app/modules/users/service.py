"""Users service — admin-managed OpsHub accounts + per-user module grants.

Every OpsHub user has a backing Firebase auth account (Firebase is the sole auth
authority); account creation goes through a `UserProvisioner` seam so the real
SDK call is isolated and stubbed locally. A password is **NEVER** accepted or
stored — the user sets their own via a Firebase password-setup link.

Two lockout guards protect the platform, both computed from DB truth:
  (a) the LAST active ADMIN can neither be disabled nor demoted; and
  (b) no admin may self-disable or self-demote (self-lockout).

Every mutation is audited inside the caller's transaction, so the trail commits
atomically with the change (one audit entry per KIND of change on update).
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.modules.users.provisioner import UserProvisioner
from app.platform import audit
from app.platform.models import Role, User, UserModuleAccess
from app.platform.module_registry import REGISTRY

logger = logging.getLogger(__name__)

# Deliberately simple (no email-validator dependency / EmailStr): non-space runs
# either side of a single '@', with a dotted domain. Good enough as an edge guard;
# Firebase is the real authority on deliverability.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_NAME_LEN = 200


class UserError(Exception):
    """Invalid users operation. The route maps this to 409 by default."""


class ValidationError(UserError):
    """Bad input — invalid email/role/module_key. The route maps this to 400."""


class DuplicateEmail(UserError):
    """The email is already registered (route -> 409)."""


class LockoutGuard(UserError):
    """Refused: would remove the last active admin, or self-lockout (route -> 409)."""


def assignable_module_keys() -> set[str]:
    """Module keys an admin may grant: real user-facing modules only.

    Excludes the `_system` nav group, the health module, AND any `coming_soon`
    placeholder (a not-yet-live module must not be grantable — an early grant would
    let a user reach it the moment it ships a real router, before it's meant to be
    live; this matches the frontend's own filter). Read from the live REGISTRY so a
    newly-registered module becomes grantable with no change here.
    """
    return {
        m.key for m in REGISTRY
        if m.nav_group != "_system" and m.key != "health" and not m.coming_soon
    }


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


def _coerce_role(role: Role | str) -> Role:
    if isinstance(role, Role):
        return role
    try:
        return Role(role)
    except ValueError as exc:
        raise ValidationError(
            f"role must be one of {[r.value for r in Role]}"
        ) from exc


def _validate_module_keys(module_keys: list[str]) -> list[str]:
    keys = list(dict.fromkeys(module_keys))  # dedupe, preserve order
    assignable = assignable_module_keys()
    bad = sorted(k for k in keys if k not in assignable)
    if bad:
        raise ValidationError(f"unknown module keys: {bad}")
    return keys


def _other_active_admins(db: Session, exclude_user_id: int) -> int:
    """Count active ADMINs other than `exclude_user_id`, LOCKING the active-admin set.

    A plain `COUNT` is TOCTOU-racy: two concurrent PATCHes each demoting/disabling a
    DIFFERENT one of the last two admins would both read the other as still active
    (count==1 → PASS) and both commit → ZERO active admins → the org is locked out of
    `user.manage` with no API recovery. Selecting the active-admin rows `FOR UPDATE`
    (ordered, to avoid deadlock) serializes those transactions: the second blocks,
    then re-reads count==0 and is correctly rejected. On sqlite `FOR UPDATE` is a
    no-op (single-writer already serializes); the race is Postgres-only.
    """
    ids = db.execute(
        select(User.id)
        .where(User.role == Role.ADMIN, User.active.is_(True))
        .order_by(User.id)
        .with_for_update()
    ).scalars().all()
    return sum(1 for i in ids if i != exclude_user_id)


def list_users(db: Session) -> list[User]:
    """All users, oldest first, with module grants eager-loaded."""
    return list(
        db.execute(
            select(User).options(selectinload(User.module_access)).order_by(User.id)
        ).scalars()
    )


def create_user(
    db: Session,
    *,
    email: str,
    name: str,
    role: Role | str,
    module_keys: list[str],
    actor_uid: str | None,
    provisioner: UserProvisioner,
) -> tuple[User, str | None]:
    """Provision a Firebase account and create the app user + module grants.

    Validates email/name/role and that every module_key is assignable, rejects a
    duplicate email (409), provisions a PASSWORD-LESS auth account via the seam
    (uid), inserts the `User` (active) + one `UserModuleAccess` per key, audits
    `user.created`, and returns `(user, password_setup_link | None)`.
    """
    email = _clean_email(email)
    name = _clean_name(name)
    role = _coerce_role(role)
    keys = _validate_module_keys(module_keys)

    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise DuplicateEmail(f"email {email} is already registered")

    # Provision the (password-less) Firebase account, then persist the app user in a
    # SAGA: if ANYTHING after provisioning fails (DB error, a lost commit, audit-chain
    # contention, the unique-email race), COMPENSATE by deleting the just-created auth
    # account — otherwise a Firebase account survives with no app row and, because the
    # email now already exists in Firebase, that address can NEVER be created again.
    uid = provisioner.create_auth_user(email)  # may raise ProvisionError (route -> 409)
    try:
        user = User(
            firebase_uid=uid,
            email=email,
            name=name,
            role=role,
            active=True,
            module_access=[UserModuleAccess(module_key=k) for k in keys],
        )
        db.add(user)
        db.flush()  # trips the unique email/uid backstop on a race -> IntegrityError
        audit.log(
            db,
            action="user.created",
            actor_uid=actor_uid,
            entity="user",
            entity_id=str(user.id),
            detail={"email": email, "role": role.value, "modules": keys},
        )
        db.commit()  # own the commit so the saga covers the FULL create window
    except Exception as exc:
        # A FULL rollback (not a savepoint) reliably undoes the flushed insert on both
        # Postgres and SQLite — a released `begin_nested` savepoint is NOT undone by a
        # later rollback on pysqlite, so it must not guard this compensation path.
        db.rollback()
        _compensate_provisioning(provisioner, uid, email)
        if isinstance(exc, IntegrityError):
            raise DuplicateEmail(f"email {email} is already registered") from exc
        raise
    # The setup link is best-effort AFTER commit: the user is already durable, so a
    # transient link-generation failure must NOT roll them back (they can re-issue).
    return user, _safe_setup_link(provisioner, email)


def _compensate_provisioning(provisioner: UserProvisioner, uid: str, email: str) -> None:
    """Delete a Firebase account whose app user failed to persist (never leak an
    orphan). Best-effort: a failed compensation is LOGGED, not raised, so the original
    error still surfaces to the caller."""
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
    role: Role | str | None = None,
    active: bool | None = None,
    module_keys: list[str] | None = None,
    actor_uid: str | None,
    acting_user: User,
) -> User:
    """Apply the provided fields to `user`; audit ONE entry per kind of change.

    Guards (raise `LockoutGuard` -> 409):
      (a) cannot disable or demote-from-ADMIN the LAST remaining active ADMIN; and
      (b) `acting_user` cannot self-disable or self-demote from ADMIN.
    "Last active admin" is computed from the DB, not from the caller's view.
    """
    # --- validate up front (no partial mutation on bad input) ---
    new_name = _clean_name(name) if name is not None else None
    new_role = _coerce_role(role) if role is not None else None
    new_keys = _validate_module_keys(module_keys) if module_keys is not None else None

    is_self = acting_user.id == user.id
    is_active_admin_now = user.role == Role.ADMIN and user.active
    final_role = new_role if new_role is not None else user.role
    final_active = active if active is not None else user.active
    is_active_admin_after = final_role == Role.ADMIN and final_active

    # (b) self-lockout — an admin may not remove their own access.
    if is_self:
        if active is False:
            raise LockoutGuard("you cannot disable your own account")
        if new_role is not None and user.role == Role.ADMIN and new_role != Role.ADMIN:
            raise LockoutGuard("you cannot remove your own admin role")

    # (a) last active admin — the system must always retain one.
    if (
        is_active_admin_now
        and not is_active_admin_after
        and _other_active_admins(db, exclude_user_id=user.id) == 0
    ):
        raise LockoutGuard("cannot disable or demote the last active admin")

    # --- apply + audit (one entry per kind of change) ---
    if new_name is not None and new_name != user.name:
        user.name = new_name
        audit.log(
            db,
            action="user.updated",
            actor_uid=actor_uid,
            entity="user",
            entity_id=str(user.id),
            detail={"name": new_name},
        )

    if new_role is not None and new_role != user.role:
        old_role = user.role
        user.role = new_role
        audit.log(
            db,
            action="user.role_changed",
            actor_uid=actor_uid,
            entity="user",
            entity_id=str(user.id),
            detail={"from": old_role.value, "to": new_role.value},
        )

    if active is not None and active != user.active:
        user.active = active
        audit.log(
            db,
            action="user.status_changed",
            actor_uid=actor_uid,
            entity="user",
            entity_id=str(user.id),
            detail={"active": active},
        )

    if new_keys is not None:
        desired = set(new_keys)
        current = {m.module_key for m in user.module_access}
        to_add = desired - current
        to_remove = current - desired
        if to_add or to_remove:
            for grant in list(user.module_access):
                if grant.module_key in to_remove:
                    user.module_access.remove(grant)  # cascade delete-orphan
            for key in new_keys:
                if key in to_add:
                    user.module_access.append(UserModuleAccess(module_key=key))
            audit.log(
                db,
                action="user.module_access_changed",
                actor_uid=actor_uid,
                entity="user",
                entity_id=str(user.id),
                detail={"added": sorted(to_add), "removed": sorted(to_remove)},
            )

    db.flush()
    return user


def setup_link(
    db: Session, user: User, *, provisioner: UserProvisioner, actor_uid: str | None = None
) -> str | None:
    """(Re)issue a password-setup link for an existing user (None if unsupported).

    Mints no persistent state in OUR DB (the link is minted by Firebase), but
    (re)issuing a set-password link is an account-takeover-capable admin action, so
    it IS audited. The link itself (a sensitive credential-setting URL) is never
    written to the trail — only that it was issued and whether one was produced.
    """
    link = provisioner.password_setup_link(user.email)
    audit.log(
        db,
        action="user.setup_link_issued",
        actor_uid=actor_uid,
        entity="user",
        entity_id=str(user.id),
        detail={"delivered": link is not None},
    )
    return link
