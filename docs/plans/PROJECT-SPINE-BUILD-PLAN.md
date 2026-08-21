# Project Spine — Build Plan & Live Tracker

**Companion to `PROJECT-SPINE-DESIGN.md` (the frozen contract).** This is the living orchestration tracker — updated as waves land. Owner approved the plan + Wave 1 go on 2026-08-20.

## Orchestration model — the 6-stage pipeline every wave runs

| Stage | Who | What |
|---|---|---|
| 1. Foundation & contract | **Me** | Shared schema + Alembic migration + numbering/RBAC wiring; freeze the API contract so FE+BE agents build to a fixed shape |
| 2. Parallel build | **Agents** | BE-service and FE-screen agents run concurrently against the frozen contract (**agents told: NO git commands**) |
| 3. Integrate & gate | **Me** | Wire together, run the FULL gate (ruff · mypy · pytest · alembic · tsc · vite build · vitest), fix breakages |
| 4. Runtime verify | **Me** | Exercise every role/path through the real SPA+API (dev-auth switcher); show evidence |
| 5. Independent audit | **Agents** | Adversarial correctness (**DUAL for money/auth/destructive**) + a UI/UX lane, each finding a concrete failure scenario |
| 6. Fix + E2E + docs | **Me + agent** | Fold fixes, E2E agent adds Playwright specs, sweep RESUME/memory, checkpoint with owner |

Standing rules honoured every wave: full gate (never a piped exit code), runtime = definition of done, `develop` stays unpushed until green, **no push/deploy/cloud without owner go**.

## Master plan — the four build waves (wave 5 = owner-gated cloud)

| Wave | Build agents | Verify agents | Audit rigor | Active orchestration | Status |
|---|---|---|---|---|---|
| **1 — Foundation** (Product · Client master · PO+line items · numbering prefix · quote search · `sales_orders` RBAC) | 8 (4 BE + 4 FE) | 3 | Single + UI/UX | ~3–5 hrs | ✅ DONE (`471b007`) |
| **2 — Revenue+AR ∥ Finance ∥ Vendor CN** (`billing` · `finance` · expense `doc_type`) | 5 BE + 4 FE | DUAL money + UI/UX + eval | **DUAL (money)** | done | ✅ BE+FE+DUAL-audit done; E2E 13/13. Reframed to UPLOAD-and-tally (accounting-software invoices) + line-level PO match. `1226c21` (Stage 0) → `1572072` (BE) → `a695621` (FE+fixes) |
| **3 — Fulfilment** (logistics · POD) | 1 BE + 1 FE | audit + E2E | Single + UI/UX | done | ✅ DONE. Owner: challan already carries invoice#/PO#/project (inc 15) → link IN PLACE, statutory challan generation UNTOUCHED; Wave 3 = logistics only. Gate BE pytest 539 · FE vitest 128 · **E2E 15/15**. Audit+E2E caught a mount-prefix 404 + BE↔FE shape drift (nested vs flat, message vs reason) + a bulk-upsert SAVEPOINT gap — all fixed. `8e8ff49`→`b073ba7`→`0faa96e`. migr `86fd55b35152` |
| **4 — Action Center** (computed reminders: procurement T-15 · invoicing-due · AR-overdue) | 1 BE + 1 FE | audit + E2E | Single + UI/UX | done | ✅ DONE. Read-only `action_center` module, computed on read (no tables/scheduler); reuses invoiced_qty rollup + ar_register(overdue). Audit: no HIGH (math/RBAC/shape clean); fixed 2 copy inaccuracies + defensive CLOSED exclusion; VERIFIED DRAFT POs correctly included (no PO-confirm lifecycle). Gate BE **pytest 561** · FE **vitest 140** · **E2E 18/18**. `1242101`→`97af0ae`→`7cd451f` |
| **5 — Cloud** (Sheet sync · push reminders/scheduler · e-invoice/e-way) | — | — | — | blocked on GCP/Firebase | ☐ owner-gated |

**Totals:** ~21 build + ~13 verify ≈ **34 agent invocations**; ~4–6 concurrent per wave (under the 15 guideline). **Active orchestration ≈ 11–17 hrs** wall-clock, agent-paced. Calendar time is longer — waves are sequential with an owner checkpoint between each.

## Wave 1 — Foundation (submodule tracker)

Critical path: Stage 0 (me) → PO+line-items BE (#3, heaviest) → integrate/gate → audit → fix → E2E. Product/Client/Quote lanes finish earlier and wait at the integration gate.

| # | Submodule | Builder | Deliverable | Status |
|---|---|---|---|---|
| 0 | Schema + migration + numbering prefix + RBAC wiring + contract freeze | Me | new `sales_orders` module tables (`product`, `purchase_order`, `po_line_item`, `po_amendment`), extended `project_client` + `client_gstin/address/contact`, per-series numbering prefix, RBAC module + action catalog | ✅ migration `6bb6cbb06e4c`; gate BE ruff 0 · mypy 0 · pytest 414 · no-drift |
| 1 | Product Master BE | Agent | CRUD service+routes, `lower()` unique index, autocomplete | ✅ 8 tests |
| 2 | Client Master BE | Agent | client + child GSTIN/address/contact + credit-terms, defaults | ✅ 12 tests |
| 3 | PO + line items BE | Agent | service (numbering, amendments), bulk Excel + manual entry (resume-aware, dedup), soft-copy upload, over-order guards | ✅ 15 tests |
| 4 | Quote/price-book search BE | Agent | search endpoint: multi-keyword + facets + price-trend + qty context | ✅ 10 tests |
| 5 | Product Master FE | Agent | list + add/edit screen | ✅ 5 tests |
| 6 | Client Master FE | Agent | multi-GSTIN/address/contact editor | ✅ 5 tests |
| 7 | PO entry FE | Agent | PO header + line-item grid + Excel upload (+ soft-copy on detail) | ✅ 7 tests |
| 8 | Quote search FE | Agent | search screen (filters, price history, trend) | ✅ 4 tests |
| A1 | Correctness audit | Agent | found H1 (cross-client project) + M1/M2/L2/L3 — **all fixed** + 4 regression tests | ✅ |
| A2 | UI/UX audit | Agent | found M1–M4 + L2/L3 (incl. false-empty projects error, VIEW dead-end, missing soft-copy upload) — **all fixed** | ✅ |
| A3 | E2E | Agent | Playwright product→client→PO→short-close/void→quote→GSTIN + RBAC matrix; found E1/E2 (add-first-child, stale-detail) — **fixed** | ✅ 11/11 |

**Wave 1 COMPLETE (2026-08-20).** Gate: BE ruff·mypy·**pytest 463**·no-drift · FE typecheck·build·**vitest 100** · **E2E 11/11** · runtime-verified in-browser (live data, 0 console errors). Commits `44482d9` (BE), `b631c83` (FE + BE audit fixes), `471b007` (UI/UX + E2E fixes). Audits found + fixed **1 HIGH backend** (cross-client project linkage) + **2 HIGH frontend** (add-first-child, stale detail) + 8 MED/LOW.

**Owner-decision residual (surfaced, NOT auto-deferred):** L1 — concurrent "add default GSTIN/address/contact" can leave two defaults (no partial-unique DB constraint; `_unset_sibling_defaults` demotes after insert). Cosmetic, internal-tool low-concurrency. Recommended follow-up: a partial unique index `WHERE is_default` (a small migration). Owner to decide fix-now vs accept.

## Known long poles / risks

| Risk | Mitigation |
|---|---|
| Audit-fix loops (esp. Wave 2 money DUAL) | Budgeted into ranges; money DUAL non-negotiable |
| **Postgres migration verify is owner-gated** (no Docker/psql locally) | Gate on SQLite + round-trips; owner runs the PG leg before deploy |
| E2E flakiness (async races) | Same harness already at 9/9 |
| Challan-generation inversion (Wave 3 touches built code) | Settled at the Wave-2→3 checkpoint before code |

---

*Update the status cells + `docs/RESUME.md` as each submodule/wave lands.*
