"""Admin-only "Audit & Access" report + access-tracking wiring (inc 38).

In-memory sqlite (StaticPool). `current_user` is resolved from the request session by
firebase_uid (mirroring production, where current_user shares the route's db session) so
the login-event / last-seen writes actually persist. Covers:
  * IAM gate — every /admin/audit/* endpoint 403s without the IAM permission, 200s with it.
  * events — an audit.log row appears newest-first, actor resolved, filters + pagination +
    has_more, and `created_at` is present (mapped from the model's `ts`).
  * login-event — POST records a login_event with the forwarded IP + stamps last_login_at;
    the logins report returns it with the resolved user.
  * /me — last_seen_at is stamped once, then throttled; a backdated value re-stamps.
  * integrity — an untampered chain verifies intact.
  * CSV — spreadsheet-injection guard on the export.
  * migration — the new revision upgrades/downgrades round-trip with no drift.

Importing app.main registers every table on Base + wires the routers under /api/v1.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session, selectinload, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

import app.main  # noqa: F401 - registers all tables + routers
from app.db import Base, get_db
from app.modules.audit_report import service
from app.modules.audit_report.routes import router as audit_router
from app.modules.me.routes import router as me_router
from app.modules.users.routes import router as users_router
from app.platform import audit
from app.platform.auth import current_user
from app.platform.models import AuditLog, LoginEvent, User
from app.platform.roles_builtin import ensure_builtin_roles

BACKEND_DIR = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- fixture

@pytest.fixture
def env() -> Iterator[dict[str, Any]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    seed = TestSession()
    roles = ensure_builtin_roles(seed)
    seed.commit()
    admin_role = roles["Administrator"].id  # carries the IAM platform permission
    fin_role = roles["Finance"].id           # a real role WITHOUT IAM
    users = {
        "admin": User(firebase_uid="admin-uid", email="admin@ops.local", name="Admin One",
                      role_id=admin_role),
        "fin": User(firebase_uid="fin-uid", email="fin@ops.local", name="Fin User",
                    role_id=fin_role),
        "alice": User(firebase_uid="alice", email="alice@example.com", name="Alice Smith",
                      role_id=fin_role),
    }
    seed.add_all(list(users.values()))
    seed.commit()
    ids = {k: u.id for k, u in users.items()}
    seed.close()

    def _override_db() -> Iterator[Session]:
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(audit_router, prefix="/api/v1")
    app.include_router(me_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.state.TestSession = TestSession
    app.state.ids = ids
    client = TestClient(app)
    _as(client, "admin")
    yield {"client": client, "TestSession": TestSession, "ids": ids}
    Base.metadata.drop_all(engine)
    engine.dispose()


# Friendly key -> seeded firebase_uid (the uid actor_uid/join resolution keys on).
_UIDS = {"admin": "admin-uid", "fin": "fin-uid", "alice": "alice"}


def _as(client: TestClient, key: str) -> None:
    """Point current_user at the seeded user `key`, loaded from the REQUEST session (so its
    last_login_at / last_seen_at writes persist, exactly as in production)."""
    fuid = _UIDS[key]

    def _cur(db: Session = Depends(get_db)) -> User:  # noqa: B008 - FastAPI dependency
        return db.execute(
            select(User).options(selectinload(User.role)).where(User.firebase_uid == fuid)
        ).scalar_one()

    client.app.dependency_overrides[current_user] = _cur


def _write_event(env: dict[str, Any], **kw: Any) -> None:  # noqa: ANN401
    db = env["TestSession"]()
    audit.log(db, **kw)
    db.commit()
    db.close()


def _count_logins(env: dict[str, Any]) -> int:
    db = env["TestSession"]()
    n = int(db.execute(select(func.count()).select_from(LoginEvent)).scalar() or 0)
    db.close()
    return n


def _seed_login(
    env: dict[str, Any], *, uid_key: str, ip: str | None, user_agent: str | None,
    occurred_at: datetime,
) -> None:
    """Insert a login_event directly (used to plant a backdated prior row)."""
    db = env["TestSession"]()
    db.add(
        LoginEvent(
            user_id=env["ids"][uid_key], occurred_at=occurred_at, ip=ip,
            user_agent=user_agent,
        )
    )
    db.commit()
    db.close()


def _reload(env: dict[str, Any], uid_key: str) -> User:
    db = env["TestSession"]()
    u = db.execute(
        select(User).where(User.id == env["ids"][uid_key])
    ).scalar_one()
    db.expunge(u)
    db.close()
    return u


# ------------------------------------------------------------------- IAM gate

def test_iam_gate_blocks_non_iam_allows_admin(env: dict[str, Any]) -> None:
    client = env["client"]
    endpoints = [
        "/api/v1/admin/audit/events",
        "/api/v1/admin/audit/logins",
        "/api/v1/admin/audit/integrity",
    ]
    _as(client, "fin")  # Finance role -> no IAM permission
    for ep in endpoints:
        assert client.get(ep).status_code == 403, ep
    _as(client, "admin")
    for ep in endpoints:
        assert client.get(ep).status_code == 200, ep


# --------------------------------------------------------------------- events

def test_events_lists_resolves_actor_and_maps_created_at(env: dict[str, Any]) -> None:
    _write_event(
        env, action="user.created", actor_uid="alice", entity="user",
        entity_id="42", detail={"role": "Finance"},
    )
    r = env["client"].get("/api/v1/admin/audit/events")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_more"] is False
    item = body["items"][0]
    assert item["action"] == "user.created"
    assert item["entity"] == "user" and item["entity_id"] == "42"
    assert item["detail"] == {"role": "Finance"}
    assert item["created_at"]  # mapped from the model's `ts`
    assert item["actor"] == {"email": "alice@example.com", "name": "Alice Smith"}


def test_events_unknown_actor_is_null(env: dict[str, Any]) -> None:
    _write_event(env, action="thing.did", actor_uid="ghost-uid")
    item = env["client"].get("/api/v1/admin/audit/events").json()["items"][0]
    assert item["actor"] is None


def test_events_filters_action_entity_and_actor(env: dict[str, Any]) -> None:
    _write_event(env, action="user.created", actor_uid="alice", entity="user", entity_id="1")
    _write_event(env, action="role.changed", actor_uid="admin-uid", entity="role", entity_id="2")
    client = env["client"]

    assert len(client.get("/api/v1/admin/audit/events?action=user.created").json()["items"]) == 1
    assert client.get("/api/v1/admin/audit/events?action=nope").json()["items"] == []
    assert len(client.get("/api/v1/admin/audit/events?entity=role").json()["items"]) == 1
    # actor matches the resolved email (case-insensitive contains)
    hits = client.get("/api/v1/admin/audit/events?actor=ALICE@example").json()["items"]
    assert len(hits) == 1 and hits[0]["action"] == "user.created"
    # actor also matches the raw actor_uid
    assert len(client.get("/api/v1/admin/audit/events?actor=admin-uid").json()["items"]) == 1


def test_actor_filter_escapes_like_metacharacters(env: dict[str, Any]) -> None:
    # A literal '%' must match literally, NOT act as a match-all wildcard.
    _write_event(env, action="x.one", actor_uid="alice")
    _write_event(env, action="x.two", actor_uid="pct%uid")
    hits = env["client"].get(
        "/api/v1/admin/audit/events", params={"actor": "%"}
    ).json()["items"]
    assert len(hits) == 1  # only the row whose actor literally contains '%'
    assert hits[0]["action"] == "x.two"


def test_events_date_range_filters_on_ts(env: dict[str, Any]) -> None:
    _write_event(env, action="today.event", actor_uid="alice")
    client = env["client"]
    today = datetime.now(UTC).date().isoformat()
    past = (datetime.now(UTC).date() - timedelta(days=30)).isoformat()
    assert len(client.get(f"/api/v1/admin/audit/events?date_from={today}").json()["items"]) == 1
    # a window entirely in the past excludes today's row
    r = client.get(f"/api/v1/admin/audit/events?date_from={past}&date_to={past}")
    assert r.json()["items"] == []


def test_events_pagination_and_has_more(env: dict[str, Any]) -> None:
    for i in range(5):
        _write_event(env, action=f"evt.{i}", actor_uid="alice")
    client = env["client"]
    page1 = client.get("/api/v1/admin/audit/events?limit=2&offset=0").json()
    assert len(page1["items"]) == 2 and page1["has_more"] is True
    page3 = client.get("/api/v1/admin/audit/events?limit=2&offset=4").json()
    assert len(page3["items"]) == 1 and page3["has_more"] is False
    # newest-first: the last-written event leads page 1
    assert page1["items"][0]["action"] == "evt.4"


# --------------------------------------------------------------------- logins

def test_login_event_records_ip_and_stamps_last_login(env: dict[str, Any]) -> None:
    client = env["client"]
    r = client.post(
        "/api/v1/auth/login-event",
        headers={"x-client-ip": "9.8.7.6", "user-agent": "TestUA/1.0"},
    )
    assert r.status_code == 204, r.text
    assert _count_logins(env) == 1
    assert _reload(env, "admin").last_login_at is not None

    logins = client.get("/api/v1/admin/audit/logins").json()
    assert logins["has_more"] is False
    row = logins["items"][0]
    assert row["ip"] == "9.8.7.6"
    assert row["user_agent"] == "TestUA/1.0"
    assert row["occurred_at"]
    assert row["user"] == {"email": "admin@ops.local", "name": "Admin One"}


def test_login_event_allowed_for_non_iam_user(env: dict[str, Any]) -> None:
    # ANY authenticated user may record their own sign-in (it is not IAM-gated).
    _as(env["client"], "fin")
    assert env["client"].post("/api/v1/auth/login-event").status_code == 204
    assert _count_logins(env) == 1


def test_login_event_coalesces_rapid_same_ip_and_ua(env: dict[str, Any]) -> None:
    # Two rapid POSTs with the SAME (ip, user_agent) collapse to a single login_event row.
    client = env["client"]
    headers = {"x-client-ip": "5.5.5.5", "user-agent": "SameUA/1.0"}
    assert client.post("/api/v1/auth/login-event", headers=headers).status_code == 204
    assert client.post("/api/v1/auth/login-event", headers=headers).status_code == 204
    assert _count_logins(env) == 1  # the second, identical sign-in was coalesced away


def test_login_event_not_coalesced_for_different_ip(env: dict[str, Any]) -> None:
    # A different ip (same UA) is a genuinely-distinct sign-in -> a second row is written.
    client = env["client"]
    assert client.post(
        "/api/v1/auth/login-event", headers={"x-client-ip": "1.1.1.1", "user-agent": "UA/1"}
    ).status_code == 204
    assert client.post(
        "/api/v1/auth/login-event", headers={"x-client-ip": "2.2.2.2", "user-agent": "UA/1"}
    ).status_code == 204
    assert _count_logins(env) == 2


def test_login_event_not_coalesced_when_prior_is_backdated(env: dict[str, Any]) -> None:
    # A same-(ip, ua) prior row older than the coalesce window does NOT suppress a new one.
    headers = {"x-client-ip": "7.7.7.7", "user-agent": "UA/backdate"}
    _seed_login(
        env, uid_key="admin", ip="7.7.7.7", user_agent="UA/backdate",
        occurred_at=datetime.now(UTC) - timedelta(hours=2),
    )
    assert _count_logins(env) == 1
    assert env["client"].post("/api/v1/auth/login-event", headers=headers).status_code == 204
    assert _count_logins(env) == 2  # the stale prior is outside the window -> a fresh row


def test_prune_login_events_deletes_only_older_than_cutoff(env: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    _seed_login(env, uid_key="admin", ip="old", user_agent=None,
                occurred_at=now - timedelta(days=200))
    _seed_login(env, uid_key="admin", ip="new", user_agent=None,
                occurred_at=now - timedelta(days=10))
    db = env["TestSession"]()
    deleted = service.prune_login_events(db, older_than_days=180)
    db.commit()
    assert deleted == 1  # only the 200-day-old row is past the 180-day cutoff
    remaining = db.execute(select(LoginEvent)).scalars().all()
    assert len(remaining) == 1 and remaining[0].ip == "new"
    db.close()


def test_logins_filter_by_user_id(env: dict[str, Any]) -> None:
    client = env["client"]
    _as(client, "admin")
    client.post("/api/v1/auth/login-event")
    _as(client, "fin")
    client.post("/api/v1/auth/login-event")
    _as(client, "admin")
    only_fin = client.get(f"/api/v1/admin/audit/logins?user_id={env['ids']['fin']}").json()
    assert len(only_fin["items"]) == 1
    assert only_fin["items"][0]["user"]["email"] == "fin@ops.local"


# ----------------------------------------------------------------- /me last_seen

def test_me_stamps_last_seen_once_then_throttles(env: dict[str, Any]) -> None:
    client = env["client"]
    assert client.get("/api/v1/me").status_code == 200
    first = _reload(env, "admin").last_seen_at
    assert first is not None
    # A second quick call is within the throttle window -> NO new write (value unchanged).
    assert client.get("/api/v1/me").status_code == 200
    second = _reload(env, "admin").last_seen_at
    assert second == first


def test_me_restamps_after_throttle_window(env: dict[str, Any]) -> None:
    # Backdate last_seen beyond the throttle horizon -> the next /me re-stamps it.
    db = env["TestSession"]()
    u = db.execute(select(User).where(User.id == env["ids"]["admin"])).scalar_one()
    stale = datetime.now(UTC) - timedelta(seconds=service.MAX_LIMIT * 100)  # comfortably old
    u.last_seen_at = stale
    db.commit()
    db.close()
    assert env["client"].get("/api/v1/me").status_code == 200
    refreshed = _reload(env, "admin").last_seen_at
    assert refreshed is not None
    assert refreshed.replace(tzinfo=UTC) > stale


def test_me_response_shape_unchanged(env: dict[str, Any]) -> None:
    body = env["client"].get("/api/v1/me").json()
    # The /me contract must be untouched by the last-seen stamping.
    assert set(body) == {
        "id", "email", "name", "role_id", "role_name",
        "is_administrator", "module_levels", "platform",
    }
    assert body["is_administrator"] is True


# ------------------------------------------------------------------- integrity

def test_integrity_reports_intact_and_detects_tamper(env: dict[str, Any]) -> None:
    client = env["client"]
    # An empty chain is trivially intact with nothing to check.
    empty = client.get("/api/v1/admin/audit/integrity").json()
    assert empty["intact"] is True
    assert empty["entries_checked"] == 0
    assert empty["broken_at_id"] is None

    # Three REAL hash-chained rows verify intact.
    _write_event(env, action="a.one", actor_uid="alice")
    _write_event(env, action="a.two", actor_uid="admin-uid")
    _write_event(env, action="a.three", actor_uid="alice")
    intact = client.get("/api/v1/admin/audit/integrity").json()
    assert intact["intact"] is True
    assert intact["entries_checked"] == 3
    assert intact["broken_at_id"] is None

    # Tamper with ONE row's detail via direct SQL -> its stored row_hash no longer
    # reproduces, so the chain is detected as broken AT that row's id.
    db = env["TestSession"]()
    rows = db.execute(select(AuditLog).order_by(AuditLog.id.asc())).scalars().all()
    target_id = rows[1].id  # the middle row
    db.execute(
        update(AuditLog).where(AuditLog.id == target_id).values(detail={"tampered": True})
    )
    db.commit()
    db.close()
    broken = client.get("/api/v1/admin/audit/integrity").json()
    assert broken["intact"] is False
    assert broken["broken_at_id"] == target_id


# --------------------------------------------------------------------- users

def test_users_list_exposes_last_active_fields(env: dict[str, Any]) -> None:
    env["client"].post(
        "/api/v1/auth/login-event", headers={"x-client-ip": "1.2.3.4"}
    )  # stamps admin.last_login_at
    users = env["client"].get("/api/v1/users").json()
    admin_row = next(u for u in users if u["email"] == "admin@ops.local")
    assert admin_row["last_login_at"] is not None      # ISO string after login-event
    assert "last_seen_at" in admin_row                 # present (null until first /me)
    fin_row = next(u for u in users if u["email"] == "fin@ops.local")
    assert fin_row["last_login_at"] is None


# --------------------------------------------------------------------- CSV guard

def test_csv_injection_guard(env: dict[str, Any]) -> None:
    # A free-text cell that opens with '=' (entity_id) must be neutralized with a leading
    # apostrophe; a detail VALUE that is a spreadsheet formula must never surface as a live
    # formula cell (detail is JSON-wrapped, so it can only ever open with '{').
    _write_event(
        env, action="user.updated", actor_uid="alice",
        entity="user", entity_id="=cmd()", detail={"note": "=1+1"},
    )
    r = env["client"].get("/api/v1/admin/audit/events?format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="audit-events.csv"' in r.headers["content-disposition"]
    text = r.text
    # entity_id leading '=' prefixed with an apostrophe (house-style guard)
    assert '"\'=cmd()"' in text
    # the dangerous detail value is preserved but wrapped in the quoted JSON cell — no CSV
    # cell begins with a formula trigger.
    assert "=1+1" in text
    for line in text.splitlines()[1:]:
        for cell in line.split(","):
            assert not cell.startswith(("=", "+", "-", "@"))
    # the shared guard prefixes a leading '=' with an apostrophe.
    assert service._csv_field("=1+1") == '"\'=1+1"'


# --------------------------------------------------------------------- migration

def test_migration_round_trips_with_no_drift(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'mig.db'}"
    proc_env = {**os.environ, "DATABASE_URL": db_url}

    def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=BACKEND_DIR, env=proc_env, capture_output=True, text=True,
        )

    assert _alembic("upgrade", "head").returncode == 0
    # no drift: the models match the migrated schema
    check = _alembic("check")
    assert check.returncode == 0, check.stdout + check.stderr
    # downgrade the new revision then re-apply it cleanly
    assert _alembic("downgrade", "-1").returncode == 0
    assert _alembic("upgrade", "head").returncode == 0
