# Gifsy OpsHub — Build Plan
Companion to `DESIGN.md` (v4, finalised). Created 2026-07-31.

This plan carries the Loyaltybase build learnings forward. **Principles retained; mechanisms translated** to OpsHub's stack (Python/FastAPI · React SPA · Firebase Auth · Alembic · single-tenant).

---

## Part 1 — Build governance (non-negotiable, from Loyaltybase learnings)

| Learning (Loyaltybase) | How it governs the OpsHub build |
|---|---|
| **Runtime is the Definition of Done** (`verify-flows-at-runtime`) | A unit is done only when a **real user, in the correct role, completes the flow end-to-end at runtime** against seeded data, with the **honest unhappy path** working. `mypy`/`pytest`/`tsc` green is necessary, never sufficient. "Backend complete" is a hypothesis to test. |
| **Role matrix** | Every role — **Admin / MIS / Operations / Finance** — succeeds or refuses *honestly* for each action. (Cross-tenant is N/A now — single-tenant — but the `resource`-scope hook is exercised where present.) |
| **Independent adversarial audit on every BUILD item** (`audit-every-build-item`) | After gate + runtime-verify, **before "done": spawn an independent audit** (read-only, cite file:line, try to break it). **Dual audit MANDATORY** on the money/auth/destructive paths: the **numbering engine**, **auth/RBAC**, **void**, the **extraction→register write**, and the **hash-chained audit**. When two auditors disagree, I **read the code myself** — never average. |
| **UI/UX audit is a standard lane** | For **every user-facing surface** (upload, validation report, preview, progress, review queue, dashboards): an independent UI/UX audit covering **all states** (loading/busy/empty/error + the specific failure the feature exists for), **async races** (does a control re-enable before the op finishes?), **copy honesty** (does a ✓ imply an approval that isn't real?), **consistency**, **responsiveness/tap targets**, **a11y** (not colour-only), **jargon**. A gate proves it compiles, not that it's usable. |
| **Orchestrate by default** (`default-to-orchestration`) | Delegate build streams to sub-agents with precise specs; **I** run the gates + runtime-verify + audit and stay the integrator. (Sub-agents can't run shell here → they **write** code, **I** gate.) Run independent streams in parallel; combine into one full gate. |
| **No caveated partials / no unilateral deferral** (`clarify-before-imperfect-build`, `no-unilateral-deferral`) | If a unit isn't the complete/ideal solution, **surface it and ask** — don't ship "one caveat remains" and iterate. Anything I'd defer, I **present as a decision**; the owner decides what waits. |
| **Don't fake in the FE what the backend doesn't serve** (`runtime-audit-p0.5`) | No mock/demo fallback in the SPA. Every UI action hits a **real wired endpoint** and its write is **verified to persist**. (Loyaltybase's "dead write paths" were routes silently shadowed by a proxy exclusion — the OpsHub analog is a silent FE stub; forbid it.) Recurring backend gap classes to check: role scoping too narrow, uncoerced query params → 500, a UI button with no endpoint. |
| **Deploy discipline** (`staging-deploy-gate`) | Run the **FULL** `pytest` + `vitest` suites before **every** push — a red spec silently **skips** the gated deploy. **Never trust a piped command's exit code** (capture raw `$?` or grep the summary). **"Pushed" ≠ "deployed"** — verify the **serving Cloud Run image SHA** matches the commit + curl `/health`. |
| **Migration discipline** (`migration-model`, translated Prisma→Alembic) | `alembic upgrade head` runs as an **in-VPC Cloud Run Job** (prod Cloud SQL is private-IP — unreachable from a laptop/CI runner). DB secret uses the **`@localhost/…` host form** (an empty host breaks the migrate engine). Seed is **compiled into the image** and **guarded fail-closed for prod**. The **numbering partial-unique index** (`WHERE status <> 'VOID'`) must be **hand-added** to the migration — the ORM won't model it, and it's the money-critical dedup. Staging auto-migrates with `--wait` (a failed migration fails the deploy before it serves); prod is a **gated cutover**. |
| **The E2E harness is the executable go-live gate** (`e2e-harness`) | Playwright, **real Firebase login per role**, run against a **production build** of the SPA (a dev server may not exercise the real auth path), **truncate + reseed guarded to the dev DB** before each run. Asserts: real role-scoped data renders · **no fabricated values** · RBAC scoping holds · **writes persist** (reload re-fetches from the server). **Re-run the FULL harness after ANY auth/RBAC change** — a green-but-stale harness hides exactly these bugs. |
| **Reconcile-fit / topology-first** (`reconcile-fit-before-build`) | Greenfield, so no inherited scaffolding to reconcile — the analog is **Phase 0 gets the project/infra/spine right first**, and I **verify assumptions against reality**, not against docs. Build only what fits the confirmed real need; don't scaffold ahead of confirmed inputs. |
| **Thin FE, backend is authority** (`architecture-backend-split`) | The React SPA renders UI + calls APIs — **no business logic client-side** (it may mirror validation for UX; the FastAPI backend is the sole authority on numbering, validation, extraction, RBAC). Clean module boundaries; defer per-consignor customisation until real (YAGNI). |
| **Relative dates in tests** (`date-relative-tests`) | Never hardcode a future date/month — compute relative to `now` (build via `date(y, m+1, 1)` to avoid overflow). Critical for the **FY-numbering + challan-date** tests, or they rot into false failures at the next FY/month boundary. |
| **Env-parameterised harness; unpushed-until-verified** (`environments-topology`) | The E2E harness runs local (merge gate) **and** staging (pre-prod gate) — no `localhost`/fixed-auth assumptions baked in. **Pushing `develop` auto-deploys staging**, so keep commits unpushed until locally verified; a green local run must be thorough enough to expect staging→prod to pass. |
| **Single-source docs + own consistency** (`own-consistency-no-micromanage`) | One owner doc per fact; others reference it. On any change, sweep every doc in the same pass (grep, not recall) + a doc-consistency check in CI. Correctness/consistency is mine to own end-to-end — not the owner's to catch. |
| **Trace every consumer + alternate path + scale case** (WoW #2) | A unit is done only when I've grepped **all consumers** of what changed, checked the **alternate entry path** (in-app upload vs API/bulk; single challan vs 1000-batch), and both **scale extremes** (1 row vs 1000). "It compiles / the one path works" is not done. |

### Gate & deploy commands — the two `[ADAPT]` blanks from `WAYS-OF-WORKING.md`, filled for OpsHub
- **Full gates (run complete suites, never piped — capture the raw exit code):** backend `ruff check` + `mypy` + `pytest` (whole suite); frontend `tsc --noEmit` + `vitest run`; **E2E** `playwright test` (real login per role, against a production build). All green before every push.
- **Deploy rules:** dev / staging / prod. Staging **auto-deploys on `develop`**, gated by the CI `test` job (full `pytest`+`vitest`) — a red spec silently skips it, so keep `develop` unpushed until locally verified. Prod = **gated cutover from `main`** (required reviewer). Migrations run via an **in-VPC Alembic Cloud Run Job** (staging auto `--wait`; prod gated). After any deploy, verify the **serving Cloud Run image SHA** matches the commit + `/health` → 200.
- **No push to `develop`, and no prod / shared-infra / irreversible action, without your explicit go** (WoW #8).

---

## Part 2 — Phases

### Phase 0 — Foundation & platform spine  ·  *needs NO module inputs — can start immediately*
- **Infra (Terraform):** separate GCP project (`gifsy-opshub`, same billing account) · budget alert · Cloud SQL **Option B** (one small single-zone instance, `opshub_prod` + `opshub_nonprod` DBs) · Cloud Run (API+SPA one service, scale-to-zero) + OCR worker · GCS buckets (isolated) · Secret Manager · Direct VPC egress · **Alembic migration Cloud Run Job** (in-VPC).
- **CI/CD:** GitHub Actions dev/staging/prod, `needs: test` gate, image-SHA verification, staging auto-migrate `--wait`.
- **Repo scaffold:** FastAPI modular monolith + React SPA served same-origin.
- **Platform primitives:** Firebase token-verification middleware · Postgres RBAC `can(user, action, resource?)` + `can_access_module(user, module)` · **User Management** (Admin screen: create/invite users via Firebase Admin SDK, assign role + module access, enable/disable; users self-set password) · **append-only hash-chained audit** · file upload + streamed authed download · settings · jobs contract + polling hook · `register_module` convention · shared UI kit · guarded seed.
- **E2E harness skeleton** (real login per role) + first green run.
- **DoD:** each role logs in, RBAC gates correctly, audit writes, download streams — all runtime-verified; harness green. *Dual audit on auth/RBAC + the audit chain.*

### Phase 1 — Module 1: Delivery Challan  ·  *needs the challan seed + master data*
- Master-data CRUD (consignor · brand→state-GSTIN registry · HSN rates · series · settings).
- **Numbering engine** — reserve-before-generate, FY-reset (IST), unique partial index, idempotency key, counter lock, reconcile sweeper, audited seed. **Dual audit MANDATORY.**
- Upload → validate (+ downloadable error report, inline-fix, dup-dispatch/amount-sanity/HSN warnings) → preview → reserve → **HTML→PDF generate** → ZIP + merged PDF → register.
- Void (admin, audited) · Challan Register / Job History / e-way / Void / Audit reports · dashboard.
- **DoD:** full flow end-to-end per role at runtime; harness coverage; **dual audit on numbering + void**; UI/UX audit on upload/validate/preview.

### Phase 2 — Module 2: Expense/Invoice + Document Intelligence  ·  *needs the second use case*
- **Canonical schema + evaluation harness FIRST** (before the engine) — per-field envelope, BigInt-paise, line items, calibrated confidence, labelled gold set + field-level scoring.
- **Document Intelligence service** (`SelfBuiltEngine` primary, `DocumentAIEngine` backup) — pipeline **Vision/LLM primary → rules validate**, HEIC + quality-gate (fail-closed), dedup key, **review queue (HITL)** feeding the eval set.
- Register · search/filter · export (Excel/CSV) · dashboard.
- **DoD:** end-to-end per role; **dual audit on the extraction→register write + dedup**; UI/UX audit on upload/review-queue; the eval harness proves accuracy before the engine is trusted.

### Phase 3 — Hardening & go-live
- Full E2E harness green · security review · retention/lifecycle config · budget alerts verified · **gated prod cutover** (verify serving SHA, migrations reconciled).

---

## Part 3 — To start
Phase 0 needs **none** of the four outstanding inputs, so it can begin immediately. The module inputs gate Phases 1–2:
1. Second use case (Phase 2 schema) · 2. Challan `L`-series seed (Phase 1) · 3. Master data (Phase 1) · 4. GST retention period (Phase 3).

Proposed: **start Phase 0 now** while those inputs are gathered in parallel.
