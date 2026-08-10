# Compact resume prompt — Gifsy OpsHub

Paste the block below into a fresh session to continue the build cold.

---

Continue building **Gifsy OpsHub** (internal ops platform). Repo: `C:\Users\nikun\gifsy-opshub`.

**First, read (in order):** `docs/RESUME.md` (live build state) → `docs/DESIGN.md` (v4 architecture) → `docs/BUILD-PLAN.md` (governance + phases). Also load the `opshub-platform` memory and the WAYS-OF-WORKING standing agreements.

**State:** Phase 0 access-independent runway COMPLETE. Branch `develop` @ `0f15b2a`, gate-green (backend ruff 0 / mypy --strict 0 / pytest 31/31; frontend tsc + vitest + build; `terraform validate`; 4 CI/CD workflows valid). Stack: Python/FastAPI + React SPA (served same-origin) · Firebase Auth (email/pw) · Postgres · separate GCP project · DB Option-B (one instance, prod+non-prod DBs) · Cloud Run scale-to-zero · ~₹2k/mo. Platform primitives done: RBAC (role actions + explicit per-user module access) · hash-chained audit · files/jobs/settings · Alembic · guarded seed · Terraform · CI/CD.

**NEXT — Phase 1: Delivery Challan. Start with the NUMBERING ENGINE (dual-audit mandatory, fully local-verifiable):**
- Number format CONFIRMED: `GIF/DC/26-27/L/000189` — FY (Apr–Mar, IST) embedded, series `L`, 6-wide zero-pad, resets each FY. Modes 1–2 (continue-from-last, custom-start) in v1; read-from-Excel deferred. Seed with a placeholder `L`-number (owner fills real value later).
- Integrity: reserve-before-generate under counter row-lock · unique **partial index on non-void `(series, fy, number)`** (HAND-ADD to the migration) · idempotency key · retry resumes the reservation · FY in `Asia/Kolkata` · reconcile sweeper · audited seed.
- Then: master-data CRUD (consignor · brand→state-GSTIN registry · HSN · series) · upload→validate→downloadable English error report · **HTML→PDF render** · ZIP + merged PDF · register + reports.
- Document layout = FAITHFUL reproduction of the existing `L/433` `.docx` (consignor/consignee/ship-to + line-item table + totals) — NOT a redesign. ⚠️ WeasyPrint native deps fiddly on Windows → verify PDF byte-render in the Linux container.

**How to work (WAYS-OF-WORKING + BUILD-PLAN Part 1):** orchestrate build streams to sub-agents; I run the FULL gate (never trust a piped exit code), runtime-verify, and an INDEPENDENT adversarial audit on every build item (DUAL on numbering/auth/void/extraction; UI/UX lane for user-facing). Runtime = definition of done. Keep `develop` unpushed until locally green; commit gate-green increments to `develop`. **No push / no cloud / no irreversible action without owner go.**

**Blocked on owner** (GCP project + Firebase project + deploy secrets `WIF_PROVIDER`/`DEPLOY_SA`/`GCP_PROJECT_ID` + `production` GitHub Environment): User Management (Firebase Admin SDK), real auth per role, E2E harness, actual `apply`/deploy, Postgres migration verify. **Owner inputs still needed:** real `L`-seed number · master data (consignor entities + brand→state-GSTIN registry) · GST retention period · the Expense-module second use case.

---
