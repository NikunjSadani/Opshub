# Gifsy OpsHub — Build State (RESUME)
Read this first to continue the build cold. Design: `DESIGN.md` / `design-doc.html`. Plan + governance: `BUILD-PLAN.md`.

## Where we are
**Phase 0 — Foundation & platform spine · IN PROGRESS.** First increment landed + gate-green.

### Done + verified (this increment)
- Repo initialised; structure: `backend/` (FastAPI) · `frontend/` · `infra/terraform/` · `.github/workflows/`.
- **Backend spine, gate-green** (`ruff` 0 · `mypy --strict` 0 · `pytest` 4/4):
  - `app/config.py` — pydantic-settings, env-driven.
  - `app/db.py` — SQLAlchemy engine/session (sqlite local bootstrap; Postgres in staging/prod).
  - `app/platform/module_registry.py` — the `register_module(ModuleSpec)` convention (no plugin loader).
  - `app/platform/models.py` — platform tables: `user`, `user_module_access` (**explicit per-user grants**), `audit_log` (hash-chained), `setting`.
  - `app/platform/rbac.py` — `can(user, action, resource?)` + `can_access_module(user, module)`; ADMIN_ONLY action set.
  - `app/platform/audit.py` — append-only **hash-chained** audit (`log`, `verify_chain`); tamper-evident (test-proven).
  - `app/platform/auth.py` — Firebase token verification dependency (`current_user`); valid-token-but-no-active-row → denied.
  - `app/modules/health/routes.py` — public `/api/v1/health` + `/api/v1/modules` registry echo.
  - `app/main.py` — composes registered modules under `/api/v1/<key>`.
  - `tests/test_smoke.py` — health, registry, audit-chain tamper-evidence, RBAC two-axes.
- Ops: `requirements.txt`, `pyproject.toml` (ruff/mypy/pytest), `Dockerfile` (python:3.12-slim), `.github/workflows/ci.yml` (the `test` gate), `.gitignore`, `.env.example`.

### Gotchas already hit (don't re-hit)
- **Python 3.14 + SQLAlchemy:** 2.0.36 crashes on `str | None` Mapped columns (`make_union_type` / changed `typing.Union`). **Pinned `sqlalchemy==2.0.51`.** Prod container uses **python:3.12-slim** to avoid the edge entirely.
- Models module must NOT use `from __future__ import annotations` (SQLAlchemy de-stringify path).
- FastAPI deps use the **`Annotated[...]`** style (avoids ruff B008).

### How to run the gate
```
cd backend && python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m ruff check app tests   # 0
./.venv/Scripts/python.exe -m mypy app               # 0
./.venv/Scripts/python.exe -m pytest                 # all pass
```

## Next (Phase 0 remaining) — orchestratable in parallel
Each is a build stream against the established contracts; I gate + runtime-verify + audit each.
1. **User Management** module (Admin) — create/invite via Firebase Admin SDK, assign role + explicit module access, enable/disable; audited. *(auth path → dual audit.)*
2. **RBAC/auth wired to DB + Firebase** end-to-end (seed an admin, real token → `current_user`, role/module gating on a protected route). *(runtime-verify per role.)*
3. **Alembic** baselined; the platform tables migration; the in-VPC migration Cloud Run Job.
4. **File upload + streamed authed download** primitive.
5. **Jobs contract + polling** primitive; **settings** CRUD; **guarded seed** (fail-closed for prod).
6. **Frontend shell** — Vite + React + TS + Tailwind + shadcn; login (Firebase), app shell, module nav from `/api/v1/modules`, User Management screen; served same-origin.
7. **E2E harness skeleton** (Playwright, real Firebase login per role, prod build) + first green run.
8. **Infra (Terraform)** — separate GCP project, Option-B Cloud SQL, Cloud Run, GCS, budget alert (WRITE now; APPLY needs owner GCP access).

## Needed from owner
- **GCP + Firebase access** to provision/deploy (Terraform written but not applied without it).
- The 4 module inputs (Phase 1/2): second use case · challan `L`-seed · master data · GST retention.

## Conventions
- Branch `develop` (staging), `main` (prod, gated). Keep `develop` unpushed until locally gate-green. No push/deploy/prod action without owner go (WoW #8).
