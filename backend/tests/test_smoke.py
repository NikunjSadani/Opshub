"""Phase-0 boot smoke: the app composes, health serves, the registry echoes,
and the audit hash-chain + RBAC primitives behave. No DB/Firebase required.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.platform.audit import compute_row_hash
from app.platform.models import Level
from app.platform.rbac import can, can_access_module
from tests.rbac_util import make_role, make_user

client = TestClient(app)


def test_health_ok():
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_modules_registry():
    r = client.get("/api/v1/modules")
    assert r.status_code == 200
    assert any(m["key"] == "health" for m in r.json())


def test_audit_chain_is_tamper_evident():
    h1 = compute_row_hash("", {"action": "a"})
    h2 = compute_row_hash(h1, {"action": "b"})
    # recomputing with a mutated earlier payload breaks the chain
    assert compute_row_hash("", {"action": "a-tampered"}) != h1
    assert h2 == compute_row_hash(h1, {"action": "b"})


def test_rbac_axes():
    admin = make_user("u1", role=make_role("Administrator", is_system=True))
    ops = make_user("u2", role=make_role(module_levels={"document_automation": Level.OPERATE}))
    # admin-only action
    assert can(admin, "challan.void") is True
    assert can(ops, "challan.void") is False
    # default-deny: an unregistered/typo'd action authorizes NO ONE, not even admin
    assert can(admin, "settings.typo") is False
    # explicit per-user module access (admin sees all)
    assert can_access_module(admin, "expense_invoice") is True
    assert can_access_module(ops, "document_automation") is True
    assert can_access_module(ops, "expense_invoice") is False
