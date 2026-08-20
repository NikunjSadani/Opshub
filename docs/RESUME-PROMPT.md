# Compact resume prompt — Gifsy OpsHub

Paste the block below into a fresh session to continue the build cold. This is deliberately
compact — **`docs/RESUME.md` is the authoritative, detailed source of truth** (full
increment-by-increment history + open decisions + traps). Don't duplicate it here; update
this block's state line + pending list when they change.

---

Continue building **Gifsy OpsHub** — an internal ops platform for Gifsy staff.
Repo: `C:\Users\nikun\gifsy-opshub`

**READ FIRST (in order):** `docs/RESUME.md` (authoritative live state) → `docs/DESIGN.md`
(v4 architecture + evolved-state log) → `docs/BUILD-PLAN.md` (governance/phases). Load the
`opshub-platform` memory and the WAYS-OF-WORKING standing agreements.

**STATE — `develop` @ `4309a48`** (code; docs commits ride on top — `git log --oneline -1`
for the true tip), **UNPUSHED, NO git remote.** Gate-green:
- Backend: `.venv/Scripts/python.exe -m ruff check app tests` · `mypy app` · `pytest` ·
  `alembic check` → **ruff 0 · mypy 70 · pytest 411 · no-drift** (+ migration round-trips)
- Frontend: `npm run typecheck` · `npm run build` · `npm test` → **vitest 73**
- E2E: `npm run e2e` (Playwright) → **9/9** · `terraform validate` clean

Stack: API-first FastAPI backend (`api/`) + thin React SPA (`platform/`, same-origin, proxies
`/api`→`/v1`), Firebase auth **NOT wired** (mock AuthProvider + double-guarded dev-auth shim;
`X-Dev-Uid` switches seeded dev users locally), Postgres prod / sqlite local, separate GCP
project, DB Option-B, Cloud Run scale-to-zero (~₹2k/mo).

**DONE** (feature-complete; every item dual/adversarial + UI/UX audited + E2E + runtime-verified):
- **Phase 1 Delivery Challan** — numbering engine · master data (+ GSTIN-keyed `ConsigneeParty`)
  · generator (upload→validate→reserve-then-render→issue→ZIP/2-up-merged, faithful `L/433`) ·
  contradiction review · resume-aware generate · reports/dashboard · whole-module + whole-app
  audits · **inc 25 = range/list bulk 2-up download** ("never silently drop a statutory doc").
- **Phase 2 v1 Expense/Invoice (inc 24)** — vendor GST-invoice PDFs → deterministic **pdfplumber
  text-layer extraction** (zero-cost, `Extractor` Protocol; paid/DocAI+OCR deferred) →
  canonical schema (BigInt-paise) → per-field review → confirm → register. Eval-harness-before-
  the-engine + money-critical re-audit.
- **inc 26 = RBAC v2** — a user holds ONE named **Role** granting per-module **View<Operate<Manage**
  levels + platform perms (`iam`, `settings`); `app/platform/rbac.py` action-catalog, default-deny;
  **`GET /me`** drives FE `usePermissions()` gating; admin **Roles editor** (`/admin/roles`) + Users
  role-picker; built-in **Administrator** protected. Design `docs/plans/RBAC-ROLES-DESIGN.md`.
- **inc 27 = expense cost-allocation** — every invoice tagged at upload with a **REQUIRED Project**
  + admin-managed **Payment method** (soft-delete, case-insensitive-unique), confirm-blocked
  without them; register cols/filters + **`GET /expense/summary`** → an **Overview** spend
  dashboard (by project + payment); catch-all **GEN-001 "General/Overhead"** project seeded.

**KEY TRAPS / DURABLES:**
- SQLAlchemy model files must **NOT** `from __future__ import annotations` (py3.14 crash).
- SQLite migrations need `batch_alter_table` for FK/constraint changes; Postgres enum handling
  (`DROP/CREATE TYPE`) is **invisible to the SQLite test suite** → **verify migrations on a real
  Postgres before deploy**. A cross-module FK needs its target model imported in every
  `create_all` test harness (else `NoReferencedTableError`).
- A case-insensitive UNIQUE needs a `lower(col)` **functional index** (an app `func.lower` check
  alone races). `iam` platform perm = **de-facto Administrator** (owner-accepted).
- Bare `db.begin_nested()` that never releases leaks a SAVEPOINT → `RecursionError` ~250 ops/txn:
  always `with db.begin_nested():`.
- **Orchestrate** substantial work across parallel sub-agents but tell them **NO git commands**
  (an agent-run `git stash` once clobbered a parallel tree). YOU own the shared foundation, run
  the FULL gate (never trust a piped exit code), runtime-verify through the real interface, and
  run an INDEPENDENT adversarial audit per build item (DUAL for money/auth/destructive) + a UI/UX
  lane. Runtime = definition of done. Keep `develop` unpushed until green; **no push / cloud /
  irreversible action without owner go.**

**PENDING = all owner-gated deploy (NO code queued):** provision **GCP + Firebase** + apply
Terraform; add a **git remote + CI deploy secrets** (`WIF_PROVIDER`/`DEPLOY_SA`/`GCP_PROJECT_ID`
(+`_PROD`), an Artifact Registry repo `opshub`, a `production` env with a reviewer); implement
**`GcsStorage`** (+ the folded-in bulk-download streaming + 413 orphan-blob cleanup); wire **real
Firebase auth**; **verify the RBAC + allocation migrations on Postgres**; **prod bootstrap**
(`ensure_builtin_roles` + create the first Administrator user + the overhead project); container
**WeasyPrint render** + `L/433` byte-match; real data (**`L`-series seed**, master data, GST
retention). Critical path = standing up a GCP project → then run the deploy-enablement pass as
one orchestrated increment.

**Run locally:** backend — migrate + `python -m app.seed`, then
`DEV_AUTH=true STUB_RENDER=true uvicorn app.main:app --port 8000`; FE —
`npm --prefix frontend run dev` (proxies `/api`→8000).

---
