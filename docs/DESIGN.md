# Gifsy OpsHub — Architecture (FINAL, v4)
### Internal operations platform · Delivery Challan + Expense/Invoice modules
Finalised 2026-07-31. Styled version: `design-doc.html` (published artifact). This is the text source of record.

> **Status:** architecture finalised for build. Only *inputs* remain (see §12) — not decisions.
>
> **UPDATE (2026-08):** the system is now **BUILT and LIVE in production** at
> https://opshub.gifsy.in. This doc is the v4 architecture baseline; the real current state
> (many increments past v4 — see §14 and `RESUME.md`) has moved well beyond it.

---

## 1. Posture
Platform spine + first modules. Build shared **conventions and services**, not a speculative plugin framework. Single-tenant (internal Gifsy staff). OpsHub is fundamentally a **document-processing platform** (generate documents → extract from documents), which drives the stack choice.

## 2. Stack (finalised)
- **Backend:** Python / FastAPI, modular monolith. Chosen because the platform's core work is document intelligence (OCR/CV/parsing) built in-house — Python's home turf. A deliberate divergence from Loyaltybase's Node/NestJS.
- **Frontend:** React SPA, served same-origin from the API (one Cloud Run service, cookie auth, no CORS).
- **Rendering (challans):** HTML→PDF via **WeasyPrint**, self-hosted, egress-locked.
- **DB:** PostgreSQL (Cloud SQL), one schema, table-prefixed + shared platform tables.

## 3. Identity & access
- **AuthN:** Firebase Authentication, email/password. Token verified server-side on every request; provider is the sole authority for "active"; documented offboarding runbook.
- **AuthZ:** *(This fixed-role model is superseded by RBAC v2 — inc 26; see §14. Now: named custom ROLE entities granting per-module LEVELS + platform permissions.)* Postgres RBAC on two axes — a **role** (Admin/MIS/Operations/Finance) governs *actions* via `can(user, action, resource?)`; a per-user **module-access list** (explicit grants, not role-derived) governs *which modules* via `can_access_module(user, module)`. `resource` arg shaped in now for later per-consignor/brand scoping. Default: all staff, all entities. A full per-*action* permission matrix stays out of scope.
- **User Management (Admin screen):** create/invite users by email, assign role + module access, enable/disable. Users provisioned via **Firebase Admin SDK**; they set their own password via an invite/reset link (admin never handles passwords). Every user/role/access change audited. This is a **Phase 0 platform capability** (shared across modules), and — being an auth path — gets the **dual audit + UI/UX audit**.

## 4. Document Intelligence (the reusable capability being BUILT)
- Standalone service with a product-agnostic, **versioned** HTTP contract: `POST /extract {document, doc_type} → {fields, confidence_per_field, raw_text, review_needed}`.
- **Engine = your own `SelfBuiltEngine`, primary from day one** (justified by a real second use case). `DocumentAIEngine` wired in as the **backup / escape hatch**, not the initial engine. Swap = config flip because every engine maps to the canonical schema.
- **Build guardrails (non-negotiable, from review):**
  - Engine-neutral **canonical schema up front** — per-field envelope `{value_normalized, value_raw, confidence, source_engine, page, bbox|null, status}`; money = **BigInt-paise** + rounding line; **line items** first-class; document/page model; **calibrated** confidence; schema version.
  - **Evaluation harness before the engine** — labelled gold set + field-level scoring (exact / numeric-tolerance / date-normalized). Checksums alone validate only ~half the fields (not date/number/vendor).
  - **Invert the pipeline** — Vision/LLM primary for non-text-layer docs; rules as validator/normalizer.
  - **HEIC + quality gate** (fail-closed on unreadable) · **dedup key** (GSTIN+invoice#+date+total + perceptual hash) · every human correction feeds the eval set.
- Lives in OpsHub as first consumer; **graduates to its own platform-services home** when an external consumer (a client app) arrives.

## 5. Module 1 — Delivery Challan
- Excel upload → validate → **reserve numbers** → generate multi-line challan PDFs → ZIP + merged PDF → register.
- **Numbering (platform service):** `GIF/DC/{FY}/{series}/{NNNNNN}`; FY reset in **Asia/Kolkata**; reserve-before-generate under a counter row-lock; **unique partial index on non-void `(series, fy, number)`**; **idempotency key** on batch; retry resumes the reservation; reconcile sweeper for orphaned RESERVED; audited seed. Modes 1–2 in v1; **read-from-Excel (mode 3) deferred**.
- Consignee derived from a **Brand → State → {GSTIN, address}** registry keyed by ship-to state; snapshotted onto each document. *(Evolving — see §14: superseded by a GSTIN-keyed consignee master + inline consignee in the 26-column template, inc 14–15.)*

## 6. Jobs / async
- Module 1: in-process background + status polling.
- Module 2 (heavy OCR): **queue-backed worker** (Cloud Tasks + separately-scaled worker). Same jobs contract either way.

## 7. Data, audit, security
- One schema, table-prefixed (`challan_*`, `invoice_*`) + platform tables (`user`, `role`, `audit_log`, `file`, `setting`, `doc_sequence`).
- **Append-only, hash-chained audit**; app DB role has no UPDATE/DELETE on it.
- PII redaction in logs; GST retention (write-once, per §12); DPDP retention for uploaded files.
- Downloads **streamed through the authed API** (not long-lived signed URLs). GCS buckets isolated, SA-scoped.
- Extraction/render **egress-locked**; untrusted cell values bound as data, never concatenated into templates.

## 8. Project topology & isolation
Three GCP projects, each isolated by **risk class** (not one per app):

| Project | Risk class | Holds |
|---|---|---|
| Loyaltybase | External · money-critical | Multi-tenant loyalty (existing) |
| **OpsHub** | Internal staff | Challan · Expense · Document Intelligence |
| gifsy-clients | External · client-facing | Multi-tenant app + bespoke apps (future) |

- **Isolated per project:** project, DB instance, buckets, service accounts, auth. OpsHub's DB is never on the same instance as Loyaltybase's money path or the internet-exposed client apps.
- **Reused as versioned code:** Terraform modules, CI/CD template, auth/RBAC/audit libraries, log/money-unit conventions.
- **Reused as an authed API:** Document Intelligence (cross-project OIDC).
- **Cross-service hardening:** service-to-service auth (OIDC), versioned extraction API, W3C trace propagation, GCS/IAM isolation, idempotent ingest, conformance test-vectors so audit-chain + FY-numbering are byte-identical across Python and Node.
- **Rule:** audience/risk class = a project boundary. Know-how crosses as libraries; capabilities cross as authed API calls; databases and servers don't cross.

## 9. Infrastructure
- **Separate GCP project** (`gifsy-opshub`), same billing account, per-project budget alert.
- **DB — Option B:** one Cloud SQL instance, smallest tier, single-zone (no HA), hosting **separate prod + non-prod databases**. Always-on (~₹1k/mo). Backups + PITR kept.
- Cloud Run **scale-to-zero** (API + worker). Direct VPC egress (not a connector).
- Secret Manager · Cloud Logging · Sentry (free tier). Docker · GitHub Actions (dev/staging/prod, `needs: test`) · Terraform.

## 10. Cost (finalised)
~₹2,000/month typical (₹1,700–2,500), 100 invoices/mo, both modules.

> **ACTUAL (live 2026-08):** Cloud SQL `opshub-db` on **db-f1-micro ≈ $11–13/mo** is the dominant line, consistent with this estimate (DB ~half the bill).

| Component | Config | Monthly |
|---|---|---|
| Cloud SQL | 1 instance · smallest · single-zone · prod+non-prod DBs | ~₹1,000 |
| Cloud Run (API + worker) | scale-to-zero | ~₹200–500 |
| Cloud Storage | originals + PDFs | ~₹100–200 |
| Extraction | self-built; Vision free tier; fallback trivial | ~₹0–150 |
| Logging / Secrets / Registry | — | ~₹300–500 |
| Firebase Auth / error tracking | free tier | ₹0 |
| Networking (Direct VPC egress) | — | ~₹50–100 |
| **Total** | | **~₹1,700–2,500** |

Run cost only — the real investment is the one-time engineering to build the extraction engine. DB is ~half the bill (only always-on resource).

## 11. Out of scope
schema-per-module · Cloud Tasks queue for Module 1 · signed URLs · read-from-Excel numbering (v1) · permissions admin UI · plugin-loader/registries-as-frameworks · micro-frontends · template branching · e-way portal integration (flag only) · the 30-module framework.

## 12. Inputs still needed (facts, not decisions)
> **UPDATE:** the system is **built and live in production** — these inputs were gathered and the modules shipped. Kept as the historical build-time list.
1. **Second use case** for extraction — doc types + timeline (shapes the canonical schema/contract; decides when Doc-Intelligence graduates out of OpsHub).
2. **Challan seed** — current last `L`-series number for FY 26-27.
3. **Master data** — consignor entities + brand→state-GSTIN/address registry.
4. **GST retention period** required.

## 13. Phasing
- **Phase 1:** auth+RBAC · master data · Challan module end-to-end (upload→validate→reserve→generate→ZIP/merged PDF→register) · hardened numbering · void · reports · dashboard · audit.
- **Phase 2:** Expense/Invoice module (Document Intelligence: schema + eval harness first, then the engine) · read-from-Excel numbering · copies toggle · extended reports.
- **Later:** Doc-Intelligence graduation to shared service · additional doc types · additional modules.

---
### Decision log (why, in one line each)
- **Build extraction (not buy Document AI)** — a real second use case makes owning a reusable capability worth the build; DocAI kept as backup.
- **Python/FastAPI (not house Node)** — building document intelligence in-house is Python's core competency; consistency yields elsewhere (infra/CI/patterns).
- **Separate GCP project (not shared)** — clean cost attribution + IAM/quota isolation; same billing account keeps one invoice.
- **DB Option B (one instance, prod+non-prod DBs)** — data isolated in separate databases; saves the second instance; low-stakes internal tool accepts shared performance.
- **Never co-locate OpsHub DB with Loyaltybase (money path) or client apps (internet-exposed)** — isolate by risk class, not by data sensitivity; the neighbours are what's being protected.
- **Cost levers** — scale-to-zero compute, smallest single-zone DB, Direct VPC egress; keep backups/PITR and the eval/review quality scaffolding.

---
### 14. Evolutions since v4 (recorded during the build — `RESUME.md` is the live source of truth)
The v4 design above is the baseline; a few things evolved as the build progressed (owner-driven, from the real challan template). Authoritative current state lives in `RESUME.md` / `RESUME-PROMPT.md`.
- **Continuation since inc 27 (concise — `RESUME.md` is authoritative):**
  - **Project Spine (multi-wave):** a full sales/procurement/finance spine layered onto the platform — `sales_orders` module (Product Master, Client Master with multi-GSTIN/address/contact + PAN + credit-terms, **Purchase Orders** with a DRAFT→CONFIRMED→IN_PROGRESS lifecycle + line items + amendments), **billing/AR** (client invoices + credit notes, uploaded-and-tallied against PO lines), **finance** (per-project + consolidated **P&L**, Excel export, an **"Unattributed"** bucket for invoices confirmed without a PO), **logistics** tracking (POD), and a computed **Action Center** (procurement / invoicing-due / AR-overdue reminders, no infra). Confirm-invoice-**without-a-PO** + invoice **project attribution** shipped post-Wave-4. See `PROJECT-SPINE-DESIGN.md` / `PROJECT-SPINE-BUILD-PLAN.md`.
  - **Expense Tally-aware extractor:** `get_extractor()` now defaults to **"auto" → `TallyAwareExtractor`** (`app/modules/expense/tally.py`), routing Tally 'Tax Invoice' PDFs to a dedicated `TallyInvoiceExtractor` and delegating everything else to the zero-cost `TextLayerExtractor`.
  - **Challan-QR + Invoice Access:** public challan-QR invoice viewer (built, **DORMANT** — owner flip pending) + a MANAGE-gated **Invoice Access** dashboard (LIVE) logging every PIN submission.
  - **Auth/onboarding:** **auto-emailed invites** over the MSG91 SMTP relay; in-app **Change / Forgot password**; **real Firebase** email/password auth live (first admin bootstrapped).
  - **Ops:** in-app **Help & Guides** page; admin-only **Audit & Access** report (`app/modules/audit_report/`, `login_event` table, `/admin/audit/*`).
  - **Production deployment:** LIVE at **https://opshub.gifsy.in** (Cloud Run `opshub-api`, GCP `opshub-506704`, asia-south1, rev `opshub-api-00015-8cz`), fronted by the `cloudflare-worker/` proxy; pushed to `github.com/NikunjSadani/Opshub` (`develop`). Gate: BE pytest **708** · FE vitest **175**.
- **Expense cost-allocation (inc 27 — LANDED, `07e070e`→`4309a48`):** every expense invoice is tagged, **at upload for the whole batch**, with a required **Project** (from the Projects module, Active) + an admin-managed **Payment method** (soft-delete list, case-insensitive-unique); **confirm is blocked** without both. Registers/CSV gain Project + Payment columns + filters, and a new `GET /expense/summary` powers an **Overview dashboard** (confirmed spend by project + by payment method, BigInt paise, reconciling). Money-path + UI/UX audited (allocation stamped on every persist path; no join fan-out). A catch-all **"General / Overhead" project (GEN-001)** is seeded so overhead has a home. This realises the "per-project rollups" the Projects module (inc 13) was shaped for.
- **RBAC v2 — custom roles + per-module levels (inc 26 — LANDED, `df05894`→`9992ee8`):** the original fixed `Role` enum + per-user `UserModuleAccess` (§3) is superseded by a **named ROLE entity** granting **per-module LEVELS** (View<Operate<Manage) + **platform permissions** (`iam`, `settings`); a user holds one role. `app/platform/rbac.py` enforces via an action-catalog (default-deny); `GET /me` returns effective permissions that drive the FE `usePermissions()` gating; an admin **Roles editor** (`/admin/roles`) + a Users role-picker manage it; the built-in **Administrator** role is protected. DUAL-security + UI/UX audited. ⚠️ `iam` is de-facto Administrator (owner-accepted); prod bootstrap must `ensure_builtin_roles` + create the first admin.
- **Challan range/list bulk download (inc 25 — LANDED, `8f9b6c8`→`f196ba3`):** a Download tab — pick series + FY, enter a range and/or list of challan numbers → preview → download as separate PDFs (ZIP) or a paper-saving **2-up merged** PDF (2 challans/A4). Correctness + UI/UX audited to a "never silently drop a statutory doc" invariant (VOID / unrendered / vanished-blob challans are reported via `X-Skipped-Void` / `X-Skipped-Unavailable`, never dropped or 500-ing the batch).
- **Projects module (NEW, inc 13):** a top-level module — an admin registers a **Client** (unique 3-letter code) and users create **Projects** with a system-assigned `<CLIENT_CODE>-<per-client running no.>` id (e.g. `BRI-001`), reusing the numbering engine's discipline (not the statutory challan counter). Challans (inc 15) quote an existing, Active Project ID and print it; the future Expense module can share it for per-project rollups.
- **Consignee model change (inc 14–15):** the **Brand→State registry** (§5) is superseded by a **GSTIN-keyed golden-record master** (`ConsigneeParty`). Consignee details are typed inline in the upload; a new GSTIN **auto-creates** the record (no admin verify), and a known GSTIN with different details is **flagged as a deviation** (never silently overwritten). GSTIN↔state consistency enforced on create.
- **Upload template (inc 15 — LANDED, `28b627d`):** a wider, **structured** 26-column template — explicit `Challan Group`, split ship-to address (line1/line2/city/pincode, stored split & printed joined), inline consignee columns resolved by GSTIN, and the Project ID (validated Active + printed). **`brand` dropped.** Validation emits non-blocking WARNINGS (possible splits) alongside blocking errors.
- **Consignee contradiction review (inc 16 — LANDED, `4669341`):** an upload whose consignee details contradict the stored GSTIN golden record no longer silently "stored-wins" — the batch goes to a **NEEDS_REVIEW** state (BLOCKED from generation) and the operator resolves each differing field: **Update master** (permanently update the shared record + print), **This upload only** (print uploaded on this batch, master untouched), or **Reject** (keep + print stored). A PENDING field never prints an unvetted upload; the master write is deferred to batch success; the SAME GSTIN with inconsistent details in one upload is a blocking error. A downloadable **Excel** review report echoes every uploaded row + a contradictions sheet. This supersedes the inc-15 "consignee deviation = non-blocking warning" behaviour and resolves the sparse-golden-record decision.
- **Resume-aware generate (inc 17 — LANDED, `f53809d`):** a retry of a partially-generated batch now reconciles against what's already issued — re-validation ignores errors for already-ISSUED groups (done + immutable), so a master-data change to a completed portion no longer wedges finishing the rest; a change to a still-remaining group still blocks. Statutory numbers are never re-minted for an issued group (idempotent allocation); a VOIDED group is re-validated. This resolves the last parked decision (retry-after-partial auto-reconcile).
- **Phase 2 v1 — Expense/Invoice module (inc 24 — LANDED, `506f4fe`→`5cfdf7c`):** the second module (DESIGN §5 Module 2), invoice-first + ZERO infra/API cost. Ingests vendor GST-invoice PDFs → deterministic **pdfplumber text-layer extraction** (no OCR/paid API) → engine-neutral canonical schema (per-field envelope, money=BigInt-paise) → per-field human review → confirmed record + register. Honours the Document-Intelligence guardrails (§4): canonical schema first, **eval harness before the engine** (labelled synthetic gold + a real-extractor accuracy gate), one engine now behind an `Extractor` Protocol (paid/DocAI + OCR deferred). Hard dedup + delete-and-re-upload; corrections feed the gold set. Built via orchestrated waves (design → foundation → 4 build agents → integration → DUAL+UI/UX audit → fixes → money-critical re-audit → E2E). The eval harness + dual audit caught 5 HIGH defects a green suite hid (GSTIN swap, borderless money misfile, batch-loss race, 100× money-input, inert search) + a fix-that-regressed (delete FK-order) — all fixed + regression-guarded. Frozen design: `docs/plans/EXPENSE-MODULE-DESIGN.md`.
- **E2E contradiction-flow + numbering-sweep-floor (inc 23 — LANDED, `2706390`/`78eb131`):** the harness gained the inc-16 contradiction-review flow end-to-end (5/5 specs); the generic reservation sweep's window floor was raised to 6h so it can never void a live batch's not-yet-issued reservations.
- **Settings-store governance (inc 22 — LANDED, `5013212`):** the `Setting` key→value store is now governed by a code registry — each key declares a visibility (PUBLIC/ADMIN), writes are admin-only and restricted to declared keys (a secret can't be written here; it belongs in Secret Manager), reads are visibility-gated, and unknown/legacy keys fail closed to admin-only. Resolves the inc-21 `GET /settings` finding at the root; adversarially audited (no write-bypass/read-leak/enumeration).
- **Whole-app holistic audit + fixes + CI e2e (inc 21 — LANDED, `150a3ff`):** three independent APP-WIDE lanes (security/authz over every endpoint, UI/UX+a11y over every screen, correctness/perf over the backend) beyond the per-increment passes. Security lane found no HIGH ("unusually well-hardened"); correctness lane found no statutory/money defect. The one product-wide **HIGH** was a shared-`Modal` focus bug (an unstable `onClose` dep re-ran the focus effect on every keystroke, yanking focus to the first field) — fixed + regression-tested. Perf: added register indexes (`challan_date`, `(consignee_gstin, challan_date)`; migration `1e2c7d8a41d7`) so uploads/filters/summary no longer full-scan; bounded per-group totals at validation. UI/UX: `/modules`-error retry, a shared debounce hook on all filters, a11y polish, download double-click guards, logo-links-home. CI: the reusable test workflow gained an `e2e` job (Playwright), `serve-backend.mjs` made cross-platform. Owner-surfaced (not changed): `GET /settings` read-gating, `GcsStorage` for prod (blob storage is local-disk-only today — a deploy prerequisite), a full mobile nav.
- **Local Playwright E2E harness (inc 20 — LANDED, `492af50`):** `npm run e2e` drives the real SPA against the real backend (fresh sqlite, dev-auth shim, a local-only fail-closed stub PDF renderer standing in for container-only WeasyPrint) — covering the full challan lifecycle (upload→validate→generate→register→void), projects create, register filter/CSV, user management (invite + last-admin guard), and admin-surface role-gating. 4/4 specs green; wired into CI (inc 21).
- **User Management (inc 19 — LANDED, `eb05612`; Firebase-PENDING):** the Phase-0 platform capability of §3 is built — admin-only user CRUD + per-user module grants on the existing `User`/`Role`/`UserModuleAccess` model (no migration). Accounts are provisioned **password-less** via a `UserProvisioner` seam (real Firebase Admin SDK; a local stub for dev), and the user sets their own password via a Firebase setup link — a password is never accepted or stored. Lockout guards keep at least one active admin and block self-lockout; a compensating-delete saga prevents orphaned Firebase accounts; the last-admin guard is concurrency-safe (`FOR UPDATE`). Dual + UI/UX audited. The real invite→login flow is verifiable only once the owner wires Firebase (mock AuthProvider today).
- **Whole-module audit + hardening (inc 18 — LANDED, `4c5d200`):** a 5-lane independent adversarial + UI/UX audit of the inc 14–17 seams (plus a 6th lane auditing the fixes). The statutory-numbering core was verified sound (no duplicate *number* possible under any interleaving). Fixes: **wedged-generation recovery** — a crash/deploy that drops the in-process worker left a batch stuck in GENERATING with no exit but re-upload (which would mint duplicate numbers); added an admin `recover` endpoint + an unattended stale-batch sweep, both gated on a new per-challan progress *heartbeat* (`batch.updated_at`) so a live-but-slow render is never reset, and a FE Retry/Recover affordance on the Batches tab. **Consignee same-GSTIN consistency** now tracks first-non-empty per field (a blank first row could previously drop a later differing value and print a never-entered address). **Owner-chosen register duplicate WARN** (non-blocking) on re-issuing an already-ISSUED (consignee GSTIN, group, date). Plus PII-in-error-message scrub (`hide_parameters`), European-comma/non-ASCII money rejection + quantity bound, `/files/upload` authz, service-boundary UPDATE_MASTER gate, and assorted FE polish. No new migration.
