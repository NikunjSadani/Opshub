# Compact resume prompt — Gifsy OpsHub

Paste the block below into a fresh session to continue the build cold.

---

Continue building **Gifsy OpsHub** (internal ops platform). Repo: `C:\Users\nikun\gifsy-opshub`.

**First, read (in order):** `docs/RESUME.md` (live build state — authoritative) → `docs/DESIGN.md` (v4 architecture) → `docs/BUILD-PLAN.md` (governance + phases). Also load the `opshub-platform` memory and the WAYS-OF-WORKING standing agreements.

**State — `develop` @ `0e252d7`, UNPUSHED, gate-green** (BE: ruff 0 · mypy --strict 39 files · pytest 172 · `alembic check` no drift · FE: `tsc --noEmit` · `vite build` · vitest 24 · `terraform validate` clean). Stack: Python/FastAPI + React SPA (same-origin) · Firebase Auth (email/pw, not yet wired) · Postgres (sqlite local) · separate GCP project · DB Option-B · Cloud Run scale-to-zero · ~₹2k/mo.

**Phase 1 (Delivery Challan) is essentially CODE-COMPLETE end-to-end** (inc 5–8, each dual/UI-UX-audited + runtime-verified):
- **Numbering engine** (`app/modules/numbering`): reserve→issue→void, FY in Asia/Kolkata, `GIF/DC/26-27/L/000189`, unique partial index on non-void `(series,fy,number)` + status CHECK, idempotency-resume scoped to series/fy, configured-series guard, in-txn audit, reconcile sweeper + secret-gated `POST /numbering/sweep`.
- **Master data** (`app/modules/masterdata`): single Consignor · Brand→State→{GSTIN,address} consignee registry (case-insensitive-unique) · HSN(+rate) · series; admin CRUD; full GSTIN checksum + state cross-check.
- **Challan generator** (`app/modules/challan`): upload→validate (structural+master-data+**tax-inclusive** amount `Rate×Qty×(1+GST)`, supplied-GST==HSN, CSV-guarded English error report)→**reserve ALL + COMMIT before render (lock-window CLOSED, proven)**→WeasyPrint HTML→PDF (**faithful L/433 layout**, escaped, `data:`-only url_fetcher)→issue→ZIP+merged→register; snapshots; amount OPTIONAL (value-free); invoice-no prints above challan-no; admin void; per-batch caps; crash/resume-safe.
- **React frontend** (`frontend/src`): UI kit `ui/*` + `pages/challan/*` — **Overview** (metrics: issued/void/eway counts + total value + by-series, from `GET /challan/summary`), New Challan (upload→generate→poll→download), Batches, Register (filters/e-way/admin Void + **load-more pagination**, no silent 100-row cap), Master-Data CRUD. Reprint already exists (Register per-challan PDF + Batches ZIP/merged). Built on `useApi()` (get/post/put/del/download/postForm) + react-query. **Auth is a MOCK admin** + a LOCAL-ONLY dev-auth shim (`DEV_AUTH=1`+`env=local`, double-guarded) so the SPA drives the real backend pre-Firebase.
- **2nd adversarial-audit pass (inc 9) done** — money/void/numbering core confirmed hardened (no HIGH); 6 MED/LOW fixed + regression-tested (`tests/test_audit_fixes.py`): generate never wedges in GENERATING · `eway_threshold`≤0→statutory default · Excel-serial date overflow→row error · numbering-void refuses an ISSUED number bound to a live challan · atomic single-worker generate claim · upload-cap tightened.
- **Inc 10 (`e66a0c8`) — register reports + numbering view + auth scaffolding + sweep IaC (all solo, audited):** `GET /challan/challans.csv` + `date_from`/`date_to` on the register (injection-guarded, RFC-4180 quote-faithful, 50k cap with `X-Truncated` signal — never silent); a Numbering register tab (counters + allocations); `RequireRole` guard + local dev role-switcher (mock-only, `AuthContextValue` unchanged) with **prod builds FAIL CLOSED** (mock auto-admin disabled in a prod build); and `infra/terraform/scheduler.tf` wiring hourly Cloud Scheduler → secret-gated `/numbering/sweep` (+ the missing `allUsers` run.invoker binding). Audit fixed 1 HIGH/2 MED/3 LOW; module access is by per-user grant NOT role (a wrong role-allowlist route guard was caught + removed). Reports tests in `tests/test_challan_reports.py`.
- **Inc 11 (`0e252d7`) — self-documenting upload template:** `GET /challan/template.xlsx` (`challan/template.py`, module-gated) replaces the header-only CSV — a **Challans** data sheet (header hover-comments + 2 worked example rows) + an **Instructions** sheet (per-column Required?/what-to-enter DERIVED from the schema + Dos & Don'ts). Round-trip test proves the examples parse+validate. The downloadable **English validation error report** (`Row,Column,Problem`, all errors, injection-guarded) already existed on the upload/Batches path.

**Known platform trap (fixed, don't reintroduce):** a bare `db.begin_nested()` that never releases leaks a SAVEPOINT → `RecursionError` at ~250 ops/txn. Always use `with db.begin_nested():`. (Was in numbering/audit/jobs.)

**How to work (WAYS-OF-WORKING + BUILD-PLAN Part 1):** orchestrate build streams to sub-agents; I own the shared foundation + run the FULL gate (never trust a piped exit code) + runtime-verify + an INDEPENDENT adversarial audit per build item (DUAL on money/auth/void; UI/UX lane for user-facing). Runtime = definition of done. Keep `develop` unpushed until locally green; commit gate-green increments. **No push / no cloud / no irreversible action without owner go.**

**▶ NEXT (Phase 1 finish):** verify the real **WeasyPrint PDF render in the Linux container** (can't run on Windows) + byte-match `L/433`; wire `POST /numbering/sweep` to **Cloud Scheduler**; challan **reprint**; **real Firebase auth** end-to-end (replace mock AuthProvider + dev-auth shim) → per-role gating; dashboard + reports.

**Blocked on owner** (GCP project + Firebase + deploy secrets `WIF_PROVIDER`/`DEPLOY_SA`/`GCP_PROJECT_ID` + `production` GitHub Environment): real auth per role, User-Management module, deploy, container PDF render, E2E, Postgres migration verify. **Owner inputs/decisions:** real `L`-seed number · real master data (consignor + brand→state-GSTIN registry) · GST retention · **source-file download scoping** (intra-module IDOR is by-design single-tenant — scope to uploader/admin if source spreadsheets are sensitive) · the Expense-module (Phase 2) second use case.

**Run locally:** backend `DATABASE_URL=sqlite:///./_demo.db ENV=local DEV_AUTH=true uvicorn app.main:app --port 8000` (migrate + `python -m app.seed` first); FE `npm --prefix frontend run dev` (proxies /api→8000). Gates: BE `./.venv/Scripts/python.exe -m {ruff check app tests, mypy app, pytest}`; FE `npm --prefix frontend run {typecheck,build,test}`.

---
