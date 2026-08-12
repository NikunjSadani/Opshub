# Gifsy OpsHub — Build State (RESUME)
Read this first to continue the build cold. Design: `DESIGN.md` / `design-doc.html`. Plan + governance: `BUILD-PLAN.md`.

## Where we are
**Phase 1 — Delivery Challan · CODE-COMPLETE end-to-end.** `develop` @ `0e252d7` (UNPUSHED). Increments 1–11 landed + gate-green (BE ruff 0 · mypy --strict 39 · pytest 172 · alembic no-drift · FE tsc + vite build + vitest 24 · `terraform validate` clean). Numbering engine → master data → generator → faithful L/433 → React frontend → Overview metrics + register pagination → register reports (date + CSV) · Numbering view · auth scaffolding · sweep IaC → **self-documenting `.xlsx` upload template**, each independently audited + runtime-verified. Real Firebase auth + deploy + container PDF render remain (owner GCP/Firebase). **Newest work first below; increments 5–11 are the current build, 1–4 are Phase 0.** ▶ see the **IMMEDIATE NEXT** section further down.

### Increment 11 — Self-documenting upload template (solo)
- **`GET /challan/template.xlsx`** (module-gated; `challan/template.py`) replaces the old header-only client-side CSV — which also mismatched the `.xlsx`-only uploader. Two sheets: **Challans** (data sheet = `worksheets[0]`; styled+frozen header with a hover-comment per column + 2 worked example rows — one single-line priced, one multi-line value-free) and **Instructions** (per-column Required?/Applies-to/what-to-enter **derived from `schema.CHALLAN_COLUMNS`/`REQUIRED_COLUMNS`/`GROUP_CONSISTENT_FIELDS` so it can't drift**, plus Dos & Don'ts). FE **Download template** button uses `downloadUrl`.
- **Round-trip test** (`tests/test_challan_template.py`): the template's OWN example rows parse (`parse_workbook`) + validate (`service.validate`) against seeded master data — C1 tax-inclusive = 105000 paise, C2 value-free (None) — so the examples can never silently rot. + structure + route + module-gate tests.
- Note: the **downloadable English validation error report already existed** (`_error_report_bytes`: `Row,Column,Problem`, every error collected, CSV-injection-guarded; surfaced on New Challan + Batches). Gate: BE ruff 0 · mypy 39 · pytest 172 · FE tsc + build + vitest 24. Commit `0e252d7`.

### Increment 10 — Register reports + Numbering view + auth scaffolding + sweep scheduler (orchestrated: 4 build agents + 1 audit → I integrated/gated/runtime-verified/fixed)
- **Register reports:** `date_from`/`date_to` (inclusive) filter on `GET /challan/challans`; `GET /challan/challans.csv` export — shared `_register_query`, `service._csv_field` injection guard (RFC-4180 quote-**doubling** for fidelity), money as plain 2dp decimals, 50k cap that is **never silent** (trailing marker row + `X-Truncated` header). FE: From/To date inputs + Download CSV button (`downloadUrl`, parses friendly detail errors).
- **Numbering register view:** new module tab — counters (per-series high-water mark) + allocations register (status filter, load-more, bound-to entity, void reason), consuming the module-gated `/numbering/counters` + `/numbering/allocations`.
- **Auth role-guard scaffolding:** `RequireRole` (fails CLOSED) + `useHasRole` + a LOCAL dev role-switcher kept OUT of `AuthContextValue` (mock-only `MockDevContext`; the real FirebaseAuthProvider contract is intact). **Prod builds FAIL CLOSED** — the mock auto-admin provider is disabled in a production build (renders a hard "auth not configured" block). Route access stays governed by per-user module-access, NOT a role allowlist.
- **Sweep scheduler IaC:** `infra/terraform/scheduler.tf` — hourly (Asia/Kolkata) Cloud Scheduler → `POST /numbering/sweep` with `X-Sweep-Secret`; secret wired via secrets.tf/cloudrun.tf/iam.tf; added the **missing `allUsers` run.invoker** binding the public SPA + scheduler both need. `terraform validate` Success. NOT applied (owner GCP).
- **Independent audit (1 HIGH/2 MED/5 LOW) — money/authz/auth-contract core CLEAN; fixed:** H1 prod-auth fail-closed · M1 invoker binding · M2 CSV silent-truncation signal · L2 quote fidelity · L4 friendly download errors · L5 copy. **Not changed (surfaced, not silently deferred):** L1 offset-paging boundary duplicate (inherent to offset paging; low) · L3 CSV UTF-8 BOM (parser tradeoffs; data is mostly ASCII). **Caught + corrected during integration:** a build agent gated the challan route with a role allowlist `['ADMIN','OPERATIONS']` — that contradicts the per-user module-access model (would block a granted MIS user, admit an un-granted OPERATIONS user); removed.
- **Gate:** BE ruff 0 · mypy 38 · pytest 168 · alembic no-drift · FE tsc + vite build + vitest 24 · terraform validate Success. Commit `e66a0c8` (code+tests); docs in the follow-up commit.

### Increment 9 — Overview metrics + register pagination + 2nd audit pass (orchestrated: 3 agents → I integrated/gated/runtime-verified/fixed)
- **Overview tab** (module landing at `/m/document_automation`): `GET /challan/summary` aggregation — issued/void/eway/valued counts + total value + by-series breakdown; **VOID excluded from value**, a value-free ISSUED challan counted but not valued. FE stat tiles + breakdown table + FY/series filters. *(runtime-verified: a VOID challan's ₹ value is excluded from Total value live.)*
- **Register load-more**: offset paging replaces the silent 100-row cap — "Showing N" footer, load-more only while a full page returns (`useChallansInfiniteQuery`). *(closes a silent-truncation gap.)*
- **Independent adversarial audit (money/void/numbering) — NO HIGH; core confirmed hardened** (SAVEPOINT trap closed, GSTIN mod-36 checksum, CSV-injection guard, e-way off-by-one, audit hash-chain all CLEAN). **6 MED/LOW findings fixed + regression-tested** (`tests/test_audit_fixes.py`): **MED-1** generate-wedge — any parse/reserve-phase error now marks the batch FAILED, not stuck in committed GENERATING · **MED-2** `eway_threshold` ≤0 → statutory default (never silently disables e-way) · **MED-3** Excel-serial date overflow → clean row error, not a 500 · **MED-4** numbering-void route refuses an ISSUED number bound to a live challan (would orphan it + permanently 409 its proper void) · **LOW-1** atomic single-worker generate claim (conditional UPDATE) · **LOW-3** upload cap rejects before the buffer can exceed the limit.
- **Deliberately NOT changed (surfaced, not silently deferred):** LOW-2 series-scoping the idempotency key would reintroduce a partial-retry duplicate risk — current fail-closed behavior kept; LOW-4 the single float→str→Decimal touch is safe under Python's round-tripping repr.
- **Reprint was found ALREADY built** (Register per-challan PDF + Batches ZIP/merged/error re-download) — not rebuilt (reconciled against reality before building).
- **Gate: BE ruff 0 · mypy --strict 38 · pytest 161 · alembic no-drift · FE tsc + vite build + vitest 14.** Commit `6c38d2b` (code+tests); docs in the follow-up commit.

### Increment 2 — platform primitives + frontend shell (orchestrated: 4 agents → I integrated/gated/audited)
- **Backend primitives (gate-green: ruff 0 · mypy --strict 0 · pytest 23/23):**
  - `app/modules/files/` — upload + **streamed authed download** (storage abstraction `platform/storage.py`, LocalStorage path-safe, GcsStorage TODO). Table `files_stored_file` (+ `module_key`).
  - `app/platform/jobs.py` + `app/modules/jobs/` — idempotent async jobs (`job` table), polling `GET /api/v1/jobs/{id}`.
  - `app/modules/settings/` — admin-gated settings CRUD (audited).
  - Wired into `main.py`: files at `/api/v1/files`; jobs + settings at `/api/v1`.
  - **Alembic** baselined: `alembic.ini` + `env.py` (imports all model modules) + migration `e215e02932b5_baseline_platform_tables` — applies cleanly to sqlite (7 tables).
- **Frontend shell** (`frontend/`, gate: `tsc` + `vitest` + `build` all green): Vite+React+TS+Tailwind+React-Query; app shell, nav from `/api/v1/modules`, dashboard tiles, mock auth behind a swappable `AuthProvider` (FirebaseAuthProvider TODO). Build output → `frontend/dist`.
- **Independent adversarial audit run + ALL findings fixed** (this is why the audit is mandatory):
  - CRITICAL **download IDOR** → download authz gate (uploader OR admin OR `can_access_module(module_key)`), 404 (not 403) on miss. *(proven: `test_download_denied_for_other_user`)*
  - HIGH **jobs IDOR** → `get_job` scoped to creator/admin, 404 on miss.
  - HIGH **idempotency ran work twice** → `create_job` returns `(job, created)`; worker scheduled only when `created`.
  - HIGH **upload DoS** → `max_upload_bytes` cap → 413. *(proven: `test_upload_rejects_oversized_file`)*
  - MED **job transitions** → state-machine guards (no double-complete). *(proven: `test_terminal_job_cannot_be_recompleted`)*
  - MED **idempotency rollback nuked session** → SAVEPOINT (`begin_nested`).
  - MED **audit-chain concurrent fork** → `prev_hash` UNIQUE + retry-against-head.
  - MED **RBAC fail-open** → `can()` now DEFAULT-DENY (unknown action → False). *(proven in `test_smoke`)*
  - LOW **PII in audit / header injection** → filenames out of audit `detail`; RFC5987 disposition + control-char strip.

### Decisions / flags (from increment 2)
- **Settings hold CONFIG only, never secrets** (secrets → Secret Manager). Reads are open to authenticated users (modules need config); writes are Admin-only + audited. Documented policy, not a code gate.
- ⚠️ **Migration was autogenerated on sqlite — VERIFY it on Postgres before staging** (Enum types render differently; hand-check the `user.role` / `job.status` enum DDL on a real Postgres).

### Increment 1 — repo + spine

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

### Increment 3 — seed + Terraform (done + gate-green)
- **`app/seed.py`** — guarded (fail-closed for prod), idempotent; seeds an Admin + an MIS user (explicit `document_automation` grant) + `eway_threshold` setting. *(pytest 26/26 incl. prod-refusal + idempotency.)*
- **`infra/terraform/`** — full separate-project infra, **`terraform validate` Success** (fmt clean): project + APIs · VPC + Private Service Access · **Cloud SQL Option-B** (one small single-zone instance, prod+non-prod DBs, backups/PITR, private IP) · GCS (isolated) · least-priv SAs · Secret Manager (`@localhost/…?host=/cloudsql/…` form) · **Cloud Run scale-to-zero** + **in-VPC Alembic migrate job** · billing budget + threshold alerts. **APPLY needs owner GCP org/billing access.**

### Increment 4 — same-origin SPA serving + CI/CD (done + gate-green)
- **FastAPI serves the built SPA same-origin** (`config.static_dir` + `_mount_spa` in `main.py`): real files served, client routes fall back to `index.html`, `/api/*` still resolves (and 404s as API). *(pytest now 31/31; 5 SPA tests.)* Local dev leaves `static_dir` empty (Vite dev server + proxy).
- **Root multi-stage `Dockerfile`** — builds the SPA then bakes it into the FastAPI image (`STATIC_DIR=/app/static`). One image = whole product; used by the Cloud Run service AND the migrate job. (`backend/Dockerfile` removed.)
- **CI/CD** (all 4 workflows YAML-valid): `_test.yml` (reusable full gate — backend ruff/mypy/pytest + frontend tsc/vitest/build) · `ci.yml` (PRs/pushes) · `deploy-staging.yml` (auto on `develop`, `deploy` **needs `test`**, build→push→**in-VPC migrate `--wait`**→deploy→**verify serving SHA**) · `deploy-prod.yml` (on `main`, `environment: production` **required-reviewer gate**).

### Increment 5 — Delivery Challan: NUMBERING ENGINE (done + gate-green + triple-audited)
The load-bearing, statutory numbering core. New module `app/modules/numbering/` (models · service · routes) + migration `ad8a9c93c1ac` + `tests/test_numbering.py` (34) + `tests/test_numbering_routes.py` (7). **Gate: ruff 0 · mypy --strict 27 files · pytest 72 · `alembic check` no drift · fresh migrate clean.**
- **Number format LIVE:** `GIF/DC/{FY}/{series}/{NNNNNN}` e.g. `GIF/DC/26-27/L/000189` — FY (Apr–Mar) computed in **Asia/Kolkata** (`tzdata` pinned), series upper-alnum, 6-wide, **resets each FY**. Modes 1–2 (continue-from-last, custom-start via `seed_series`); read-from-Excel deferred.
- **Integrity (§B3) built + PROVEN at runtime:** reserve→issue→void lifecycle · unique **partial index on non-void `(series, fy, number)`** (modelled in `__table_args__`, hand-applied in migration, `status` CHECK) · idempotency-resume (scoped to series/fy) · progress-making retry against the backstop · counter high-water monotonic/no-reuse · reconcile sweeper (atomic guarded UPDATE) · every mutation audited in-txn · **allocate refuses an unconfigured series** (no accidental `000001`).
- **DUAL adversarial audit + FIX re-audit (money-path mandate):** caught + fixed real defects a green gate hid — platform `verify_chain` false-positive (sqlite tz round-trip, **fixed in `audit.py`**), sweeper lost-update clobbering an ISSUED challan, issue-after-void TOCTOU, unaudited issuance, cross-sequence idempotency key returning the wrong number, unconfigured-counter footgun, seed/fy/width bounds. All reproduced + fixed + regression-tested. See [[audit-every-build-item]].
- ⚠️ **Not yet wired to a consumer:** `service.allocate`/`issue`/`sweep` have no live caller until the challan generator (below). The transaction contract (reserve→commit-promptly→generate→issue) is documented in the service; enforce it in the consumer.

### Increment 6 — Master data + Challan GENERATOR (done + gate-green + dual-audited)
The numbering engine's first real consumer + its reference data. New modules `app/modules/masterdata/` (models · routes · normalize) + `app/modules/challan/` (schema · models · parsing · render · service · routes) + migrations `e313362febfe` (master data) + `c57f9ac224ca` (challan) + deps `openpyxl`/`pypdf`/`weasyprint`. **Gate: ruff 0 · mypy --strict 38 files · pytest 125 · `alembic check` no drift.** Orchestrated: parsing + render built by parallel sub-agents against frozen `schema.py` contracts; I built models/service/routes and integrated.
- **Master data:** Consignor · **Brand→State→{GSTIN,address} consignee registry** · HSN(+GST rate) · series. Admin CRUD (`masterdata.edit`) + module-gated reads, all audited. Consignee **case-insensitive-unique** (normalized on write + `lower(trim())` unique index) so resolution/snapshot is never ambiguous.
- **Generator flow:** upload (chunked-capped, uuid key) → **validate** (structural + master-data + amount-sanity + HSN-rate; downloadable English error report, CSV-injection-guarded) → **reserve ALL numbers + commit BEFORE render** (closes the numbering lock-window) → HTML→PDF (WeasyPrint, escaped/bind-as-data, `data:`-only url_fetcher) → **issue** → ZIP + merged PDF → register. Consignor/consignee re-resolved + **snapshotted** onto each challan. e-way flag from `eway_threshold` setting. Admin void (challan + its number). Per-batch caps. **Crash-safe + resume-safe**: a render failure marks the batch FAILED + voids orphaned reservations; retry resumes without double-issuing.
- **DUAL adversarial audit (2 lanes: correctness/money + security) — both found real bugs a green gate hid, ALL fixed + regression-tested:** ⚠️ **a SAVEPOINT leak in `numbering.allocate` + `audit.log` + `jobs.create_job`** (bare `begin_nested()` never released → `RecursionError` at ~250 ops in one txn) — **fixed platform-wide** with `with db.begin_nested():`, proven by a 500-in-one-txn test; plus partial-generation wedge, unsafe idempotent resume (dup `allocation_id`), unbounded-batch statutory-number burn, WeasyPrint SSRF (url_fetcher), whole-file-read DoS, filename→500, CSV formula injection, float money display, e-way parse crash. Master-data audit's HIGH (consignee case/space duplication → wrong GSTIN) fixed too.
- ✅ **Increment 7 — FAITHFUL L/433 LAYOUT (`fdbdb83`, gate-green pytest 129):** reworked to the real doc — single fixed consignor, 5-field ship-to (name/address/enterprise/number/contact), line table `Sl.No|Product|HSN|Qty|Rate|Amt (incl Tax)` (UOM/GST% dropped from print), (Not for Sale) + Total(qty,amt) + Authorized Signatory, ordinal date, **Indian-grouped rupees**. **Amount is OPTIONAL (value-free challan) and TAX-INCLUSIVE** when present: validated `Rate×Qty×(1+GST)` + supplied-GST must equal HSN GST (owner-confirmed). Phones added to master data; consignee resolved case-insensitively. Render template rewritten (escaping/url_fetcher intact). **Focused money re-audit DONE (`f856d46`) — fixed 2 HIGH (value-free challan 500'd the list API; mixed priced/value-free lines under-reported total→wrong e-way) + amount-tolerance abs-cap + e-way strict-`>` + negative-rate reject; all regression-tested.**
- ⚠️ **SURFACED (owner calls):** (a) GSTIN full **checksum + state-name↔code cross-check DONE** (`ea23539`); (b) **intra-module IDOR is by-design** (single-tenant): any `document_automation` user can read all batches/challans + download others' uploaded **source spreadsheets** (may hold pricing) — scope to uploader/admin if that's sensitive; (c) layout now matches `L/433` (inc 7) using the real doc; **real WeasyPrint PDF render still to verify in the Linux container** (not runnable on Windows) + owner eyeball of the browser-preview sample.

### Increment 8 — FRONTEND (Delivery Challan UI) + supporting backend (done + gate-green + UI/UX-audited + runtime-verified)
The whole challan flow is now clickable. **FE gate: tsc --noEmit · vite build · vitest 8; Backend gate: ruff 0 · mypy 38 · pytest 143 · no drift.** Orchestrated: I owned the shared foundation (UI kit `src/ui/*` · api-client put/del/download/postForm · `ChallanModule` shell + routing · backend nav registration); 3 parallel agents built master-data screens, challan screens, and the sweeper endpoint; I integrated + ran a mandatory UI/UX audit + fixed.
- **Screens** (`frontend/src/pages/challan/*`): **New Challan** (upload→validate→error-report / counts→generate→poll→ZIP+merged download + blank-template), **Batches** (status + artifacts), **Register** (filters · e-way badge · "—" for value-free · **admin Void** via confirm+reason), **Master Data** (Consignor/Consignee/HSN/Series CRUD, soft-disable, field-level 422 errors). Invoice number now prints on the challan **just above the Delivery Challan No.** (owner ask).
- **Supporting backend:** invoice line in render; `document_automation` nav ModuleSpec; **secret-gated `POST /numbering/sweep`** (`X-Sweep-Secret`, 503 if unconfigured) for the reconcile scheduler; **LOCAL-ONLY dev-auth shim** (`DEV_AUTH=1` + `env=local`, double-guarded, inert in staging/prod) so the mock-admin SPA drives the real backend before Firebase. Fixed a **pre-existing tsconfig bug** (FE typecheck gate was never actually green).
- **UI/UX adversarial audit — found real defects a green gate hid, ALL fixed + verified live:** 422 array `detail`→"[object Object]" (root-caused in `client.ts` + unit-tested), Modal had no focus trap/restore, Modal dismissable mid-submit (now `busy`-guarded), infinite generate-poll with no escape (Start-over + stall hint), query-client recreated per render, TZ-off-by-one date, unassociated file label.
- **Runtime-verified in the browser** (backend+Vite, seeded demo DB): register renders real challans w/ correct ₹ + e-way; the **void destructive path proven end-to-end** (UI→API→DB: challan + its number VOID with reason); master-data + upload screens render; no console errors.

## ▶ IMMEDIATE NEXT — Phase 1 finish
- **Verify the real PDF render in the Linux container** (WeasyPrint) against a sample batch; byte-match `L/433`.
- Wire the reconcile **sweeper `POST /numbering/sweep` to Cloud Scheduler** (secret in Secret Manager); add challan **reprint**.
- **Real Firebase auth** end-to-end (replace the mock `AuthProvider` + dev-auth shim) — needs owner GCP/Firebase; then the FE role-gating (Void, Master Data tab) is exercised per-role.
- Owner inputs to go live: real master data (consignor + brand→state-GSTIN registry) · real `L`-seed number · GST retention.
- Phase 1 tail: dashboard + broader reports; decide the IDOR/source-file scoping (owner call above). LOW UI/UX residuals left: consignee filter debounce, gstin↔state error on form-banner vs field.

## Later (Phase 0 remaining)
Access-independent (buildable now): **Settings-backed config surface** (thin, e.g. e-way threshold).

Blocked on owner GCP/Firebase access (needed for runtime-verification):
5. **User Management** module (Admin) — create/invite via Firebase Admin SDK, assign role + explicit module access, enable/disable; audited. *(auth path → dual audit; needs Firebase.)*
6. **Real Firebase auth** end-to-end — `FirebaseAuthProvider` in the FE + seeded admin → real token → `current_user` → role/module gating runtime-verified per role.
7. **E2E harness** (Playwright, real Firebase login per role, prod build) + first green run.
8. **Verify the migration on Postgres** (enum DDL) before staging.

## Needed from owner
- **GCP + Firebase access** to provision/deploy (Terraform written + validate-clean, not applied without it).
- **Deploy wiring after GCP** (for the CI/CD workflows): GitHub secrets `WIF_PROVIDER`, `DEPLOY_SA`; vars `GCP_PROJECT_ID` (staging) + `GCP_PROJECT_ID_PROD`; an Artifact Registry repo `opshub`; and a `production` GitHub Environment with required reviewers (the prod gate).
- The 4 module inputs (Phase 1/2): second use case · challan `L`-seed · master data · GST retention.

## Conventions
- Branch `develop` (staging), `main` (prod, gated). Keep `develop` unpushed until locally gate-green. No push/deploy/prod action without owner go (WoW #8).
