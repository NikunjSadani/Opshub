"""Admin-managed expense payment methods (Bank Transfer, UPI, Cheque, ...).

An invoice is tagged with a payment method at upload for cost allocation. The list
is curated on the Expense module's Payment Methods tab (Manage level). Names are
UNIQUE case-insensitively; a method is never hard-deleted (historical invoices keep
their method) — it is retired by toggling ``active`` off (soft-delete).

Errors reuse the expense ``ExpenseError`` hierarchy so the route maps them to HTTP
status exactly like the invoice flow (``ExpenseBadRequest`` -> 400,
``ExpenseNotFound`` -> 404, ``ExpenseConflict`` -> 409). Every mutation is audited
through the module's ``_audit`` helper.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.expense.models import ExpensePaymentMethod
from app.modules.expense.service import (
    ExpenseBadRequest,
    ExpenseConflict,
    ExpenseNotFound,
    _audit,
)

_MAX_NAME_LEN = 120


def _clean_name(name: str) -> str:
    """Trim + collapse internal whitespace; 400 on empty or over the column cap."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ExpenseBadRequest("name is required")
    if len(cleaned) > _MAX_NAME_LEN:
        raise ExpenseBadRequest(f"name must be at most {_MAX_NAME_LEN} characters")
    return cleaned


def _duplicate_name(
    db: Session, name: str, *, exclude_id: int | None = None
) -> bool:
    """True if another payment method already has this name, case-insensitively."""
    stmt = select(func.count()).select_from(ExpensePaymentMethod).where(
        func.lower(ExpensePaymentMethod.name) == name.lower()
    )
    if exclude_id is not None:
        stmt = stmt.where(ExpensePaymentMethod.id != exclude_id)
    return bool(db.execute(stmt).scalar_one())


def list_payment_methods(
    db: Session, *, active_only: bool = True
) -> list[ExpensePaymentMethod]:
    """Every payment method (active-only by default), name-sorted for a stable list."""
    stmt = select(ExpensePaymentMethod)
    if active_only:
        stmt = stmt.where(ExpensePaymentMethod.active.is_(True))
    stmt = stmt.order_by(func.lower(ExpensePaymentMethod.name))
    return list(db.execute(stmt).scalars())


def get_active_payment_method(
    db: Session, payment_method_id: int
) -> ExpensePaymentMethod | None:
    """The ACTIVE payment method with this id, else None (upload-time validation)."""
    return db.execute(
        select(ExpensePaymentMethod).where(
            ExpensePaymentMethod.id == payment_method_id,
            ExpensePaymentMethod.active.is_(True),
        )
    ).scalar_one_or_none()


def create_payment_method(
    db: Session, *, name: str, actor_uid: str | None
) -> ExpensePaymentMethod:
    """Create a payment method. 400 on empty name; 409 on a case-insensitive dup."""
    clean = _clean_name(name)
    if _duplicate_name(db, clean):
        raise ExpenseConflict(f"a payment method named '{clean}' already exists")
    method = ExpensePaymentMethod(name=clean, active=True, created_by=actor_uid)
    try:
        with db.begin_nested():
            db.add(method)
            db.flush()
    except IntegrityError as exc:  # UNIQUE(name) backstop for a racing create
        raise ExpenseConflict(
            f"a payment method named '{clean}' already exists"
        ) from exc
    _audit(db, "expense.payment_method_created", actor_uid, method.id, {"name": clean})
    db.commit()
    db.refresh(method)
    return method


def update_payment_method(
    db: Session,
    payment_method_id: int,
    *,
    name: str | None = None,
    active: bool | None = None,
    actor_uid: str | None,
) -> ExpensePaymentMethod:
    """Rename and/or (de)activate a payment method (Manage). 404 if missing, 409 dup.

    Toggling ``active=False`` is the soft-delete — the row is retained so historical
    invoices keep resolving their method. Audited.
    """
    method = db.get(ExpensePaymentMethod, payment_method_id)
    if method is None:
        raise ExpenseNotFound("payment method not found")

    detail: dict[str, object] = {}
    if name is not None:
        clean = _clean_name(name)
        if clean.lower() != method.name.lower() and _duplicate_name(
            db, clean, exclude_id=method.id
        ):
            raise ExpenseConflict(f"a payment method named '{clean}' already exists")
        detail["name"] = clean
        method.name = clean
    if active is not None:
        detail["active"] = active
        method.active = active

    try:
        db.flush()
    except IntegrityError as exc:  # UNIQUE(name) backstop
        raise ExpenseConflict("a payment method with that name already exists") from exc
    _audit(db, "expense.payment_method_updated", actor_uid, method.id, detail)
    db.commit()
    db.refresh(method)
    return method
