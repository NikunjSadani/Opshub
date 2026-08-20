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
| **1 — Foundation** (Product · Client master · PO+line items · numbering prefix · quote search · `sales_orders` RBAC) | 8 (4 BE + 4 FE) | 3 | Single + UI/UX | ~3–5 hrs | ⏳ IN PROGRESS |
| **2 — Revenue+AR ∥ Finance ∥ Vendor CN** (`billing` · `finance` · expense `doc_type`) | 7 (4 BE + 3 FE) | 4 | **DUAL (money)** | ~4–6 hrs | ☐ pending |
| **3 — Fulfilment** (challan↔invoice linkage · logistics · POD) | 4 (2 BE + 2 FE) | 3 | Single + UI/UX | ~2.5–4 hrs | ☐ pending |
| **4 — Action Center** (computed reminders: procurement T-15 · invoicing-due · AR aging) | 2 (1 BE + 1 FE) | 3 | Single + UI/UX | ~1.5–2.5 hrs | ☐ pending |
| **5 — Cloud** (Sheet sync · push reminders/scheduler · e-invoice/e-way) | — | — | — | blocked on GCP/Firebase | ☐ owner-gated |

**Totals:** ~21 build + ~13 verify ≈ **34 agent invocations**; ~4–6 concurrent per wave (under the 15 guideline). **Active orchestration ≈ 11–17 hrs** wall-clock, agent-paced. Calendar time is longer — waves are sequential with an owner checkpoint between each.

## Wave 1 — Foundation (submodule tracker)

Critical path: Stage 0 (me) → PO+line-items BE (#3, heaviest) → integrate/gate → audit → fix → E2E. Product/Client/Quote lanes finish earlier and wait at the integration gate.

| # | Submodule | Builder | Deliverable | Status |
|---|---|---|---|---|
| 0 | Schema + migration + numbering prefix + RBAC wiring + contract freeze | Me | new `sales_orders` module tables (`product`, `purchase_order`, `po_line_item`, `po_amendment`), extended `project_client` + `client_gstin/address/contact`, per-series numbering prefix, RBAC module + action catalog | ✅ migration `6bb6cbb06e4c`; gate BE ruff 0 · mypy 0 · pytest 414 · no-drift |
| 1 | Product Master BE | Agent | CRUD service+routes, `lower()` unique index, autocomplete | ☐ |
| 2 | Client Master BE | Agent | client + child GSTIN/address/contact + credit-terms, defaults | ☐ |
| 3 | PO + line items BE | Agent | service (numbering, amendments), bulk Excel + manual entry (resume-aware, dedup), soft-copy upload, over-order guards | ☐ |
| 4 | Quote/price-book search BE | Agent | search endpoint: multi-keyword + facets + price-trend + qty context | ☐ |
| 5 | Product Master FE | Agent | list + add/edit screen | ☐ |
| 6 | Client Master FE | Agent | multi-GSTIN/address/contact editor | ☐ |
| 7 | PO entry FE | Agent | PO header + line-item grid + Excel upload + soft-copy upload | ☐ |
| 8 | Quote search FE | Agent | search screen (filters, price history, trend) | ☐ |
| A1 | Correctness audit | Agent | adversarial defects + failure scenarios | ☐ |
| A2 | UI/UX audit | Agent | states/empty/async/copy/a11y | ☐ |
| A3 | E2E | Agent | Playwright: product→client→PO→search | ☐ |

## Known long poles / risks

| Risk | Mitigation |
|---|---|
| Audit-fix loops (esp. Wave 2 money DUAL) | Budgeted into ranges; money DUAL non-negotiable |
| **Postgres migration verify is owner-gated** (no Docker/psql locally) | Gate on SQLite + round-trips; owner runs the PG leg before deploy |
| E2E flakiness (async races) | Same harness already at 9/9 |
| Challan-generation inversion (Wave 3 touches built code) | Settled at the Wave-2→3 checkpoint before code |

---

*Update the status cells + `docs/RESUME.md` as each submodule/wave lands.*
