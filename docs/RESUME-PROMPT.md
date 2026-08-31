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

**STATE — 🚀 LIVE IN PRODUCTION** at https://opshub.gifsy.in (Cloud Run `opshub-api`, GCP
`opshub-506704`, asia-south1, rev `opshub-api-00015-8cz`; Cloud SQL `opshub-db` db-f1-micro
~$11–13/mo; GCS storage; real Firebase auth; first admin bootstrapped). **`develop` @ `5a635ac`,
pushed to `github.com/NikunjSadani/Opshub`.** Gate-green:
- Backend: `.venv/Scripts/python.exe -m ruff check app tests` · `mypy app` · `pytest` ·
  `alembic check` → **ruff 0 · mypy clean · pytest 708 · no-drift** (+ migration round-trips)
- Frontend: `npm run typecheck` · `npm run build` · `npm test` → **vitest 175**
- E2E: `npm run e2e` (Playwright). Deploys build → in-VPC migrate job → deploy → verify serving revision.

Stack: **single Cloud Run service** = FastAPI backend (`backend/`) that serves the built React SPA
(`frontend/`) same-origin; the API is under `/api/v1`. Real **Firebase** email/password auth is
LIVE (local dev uses the double-guarded `DEV_AUTH` + `X-Dev-Uid` shim over seeded dev users).
Postgres prod / sqlite local, dedicated GCP project. Fronted by the Cloudflare worker
`opshub-proxy` (`cloudflare-worker/`) on the custom domain `opshub.gifsy.in`.

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
- **Go-live + post-launch (inc 28–38), all LIVE:** production deploy (GcsStorage, Firebase auth,
  first admin) · custom domain via the Cloudflare worker · **Project Spine** Waves 1–4 (`sales_orders`/PO
  DRAFT→CONFIRMED→IN_PROGRESS · `billing`/AR upload→match-to-PO→confirm + credit notes · `finance`
  project + consolidated P&L · `logistics` · `action_center`) · **confirm-invoice-without-a-PO +
  invoice project attribution** (finance "Unattributed" bucket, always reconciles with AR) · **Tally
  invoice extractor** (`get_extractor()` default → `TallyAwareExtractor`, `app/modules/expense/tally.py`)
  · challan-QR viewer (built, dormant) + **Invoice Access dashboard** · **auto-emailed invites** (MSG91
  SMTP, `app/platform/email.py`) · Change/Forgot password · **Help** page · client PAN/credit-terms ·
  **Audit & Access** report (`app/modules/audit_report/`, `/admin/audit`, login tracking).

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
  lane. Runtime = definition of done. Run the FULL gate before every push (never trust a piped
  exit code); **"pushed" ≠ "deployed"** — verify the serving Cloud Run revision; **no prod DB op /
  prod deploy without owner go.**

**REMAINING (owner-gated; nothing blocking use — the whole pre-launch deploy list is DONE + LIVE):**
- Flip the **challan-QR** public viewer when ready: per-client Access PINs + `PUBLIC_BASE_URL` +
  `QR_INVOICE_ACCESS_ENABLED=true` + a Cloudflare `/d/*` rate-limit rule (checklist:
  `plans/GO-LIVE-OWNER-PLAN.md` Phase 4). Built + dormant.
- Provide a real **IGST/inter-state** + a **multi-line/multi-page** Tally invoice to fully pin the
  extractor fixtures (verified on one single-page invoice + synthetic).
- **Project-Spine Wave 5** (live Google-Sheet sync · push email/WhatsApp reminders + scheduler ·
  e-invoice/e-way) — UNBLOCKED (GCP/Firebase live) but unbuilt.
- Optional: full mobile hamburger nav (desktop-first internal tool).

**Run locally:** backend — migrate + `python -m app.seed`, then
`DEV_AUTH=true STUB_RENDER=true uvicorn app.main:app --port 8000`; FE —
`npm --prefix frontend run dev` (proxies `/api`→8000).

---
