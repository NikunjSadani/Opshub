# OpsHub Go-Live — Owner Action Plan

**The one thing to understand:** almost everything left is a **one-time cloud setup** that you
do **once**, and it unlocks *all* the parked work at once — going live, real logins, durable
file storage, AND the challan-QR feature. It is not per-feature.

> **UPDATE (superseding the paragraph below):** OpsHub is now **LIVE in production** at
> https://opshub.gifsy.in — deployed, real Firebase logins, durable GCS storage, and pushed to
> the `github.com/NikunjSadani/Opshub` repo (`develop`). The historical "nothing is deployed
> today" framing is kept below only for the record; see the **✅ LIVE STATUS** section for the
> real current state. The only owner-gated item remaining is the Phase-4 challan-QR flip.

The plan is 4 phases. **You only truly block Phase 1** (three accounts). After that I do most
of the work; you approve/configure a few things.

Legend: 🧑 = only you can do it · 🤝 = we do together (I write, you provide access/approve) ·
🤖 = I do it.

---

## ✅ LIVE STATUS (updated 2026-08-27)

**OpsHub is LIVE in production.** Access it at **https://opshub.gifsy.in** (custom domain, verified
end-to-end) — or the direct Cloud Run URL `https://opshub-api-2aadkzwkua-el.a.run.app`.

Live + verified:
- **App on Cloud Run** (`opshub-api`, asia-south1), Cloud SQL Postgres 16 (`opshub-db`, db-f1-micro,
  ~$11–13/mo), GCS file storage, real **Firebase** email/password login, first Administrator
  bootstrapped.
- **Auth polish (deployed):** invite links now work (accounts are created WITH a password provider —
  the password-less bug that caused "link expired/used" is fixed); in-app **Change password** (user
  menu); **Forgot password?** on the login screen.
- **Custom domain `opshub.gifsy.in`:** Cloud Run domain-mapping is unavailable in asia-south1, and
  the gifsy.in zone runs a loyalty **wildcard** worker (`*.gifsy.in/*`). Solved with a dedicated
  Cloudflare Worker `opshub-proxy` on the more-specific route `opshub.gifsy.in/*` (in
  `cloudflare-worker/`) → forwards to the single `opshub-api` origin (SPA + API on one service).
  Verified: `/` + assets + `/api/v1/*` all proxy correctly and auth still returns 401 JSON.
  Deploy is owner-run (`npx wrangler login` then `npx wrangler deploy` from `cloudflare-worker/`,
  under the gifsy.in Cloudflare account) — git-bash and cmd.exe do NOT share the wrangler token, so
  the deploy must run in the same shell as the login.
- **In-app Help & Guides** (`/help`, ungated for all staff) with the "How to create a Delivery
  Challan" walkthrough (prerequisites → bulk Excel template → validate → number & generate → print).

- **Auto-emailed invites — LIVE (2026-08-27).** Staff invite + re-issue setup-link now email the
  link over the **MSG91 SMTP relay** from `opshub@notify.gifsy.in` (reuses loyalty's verified
  `notify.gifsy.in` domain — zero new DNS). Secret `opshub-msg91-smtp-pass-prod` (copied from
  loyalty's `MSG91_SMTP_PASS`) bound on Cloud Run; fail-closed no-op if unset. Independent audit
  clean. ⚠️ the send is synchronous in the request (≤20s under a hung relay; admin-only/rare) — a
  known accepted limitation, not a bug.
- **Client PAN + credit terms — LIVE.** Capturable at client registration (New-client modal +
  `POST /projects/clients`) as well as editable on the client detail page.

- **Invoice Access dashboard — LIVE (2026-08-28, rev opshub-api-00010-rl2).** Every PIN
  submission on the public challan-QR viewer is logged (`challan_invoice_access` table, migration
  `b7f3c2a19d84`) and shown on a MANAGE-gated "Invoice Access" tab in the Delivery Challan module
  (totals, per-client, daily trend, recent list; approx viewers via salted-hashed IP). Best-effort
  logging (never breaks the viewer); only resolved tokens log. Hardened post-audit: 60s write
  coalescing, SQL aggregation, UTC trend, 180-day retention prune (on the hourly numbering sweep).
  The worker now forwards the client IP. The dashboard is live but empty until the QR feature is on.

▶ Remaining (not blocking use):
- **Challan-QR activation** — built + dormant. Owner-gated flip checklist:
  1. Owner sets per-client **Access PIN** (Projects › Clients › Edit client), 8–32 chars.
  2. Set `PUBLIC_BASE_URL=https://opshub.gifsy.in` + `QR_INVOICE_ACCESS_ENABLED=true` on Cloud Run + redeploy.
  3. Owner re-runs `wrangler deploy` (client-IP forwarding is already in the worker source).
  4. Owner adds a Cloudflare rate-limit rule on `opshub.gifsy.in/d/*` (~30 req/min/IP).
  5. Verify: a NEW challan (PIN + confirmed matching invoice) → QR → PIN+challan-number → invoice;
     accesses appear on the Invoice Access dashboard.
  ⚠️ QRs print only on challans generated AFTER enabling; invoice must be uploaded + CONFIRMED.

---

## ▶ PHASE 1 — Set up three accounts (do this FIRST; only you can)

These three are the true prerequisites. Until they exist, nothing can deploy. They can be
created in parallel, but **start with the GitHub repo** — it's quick and lets me push all the
work (also your off-machine backup) and lets the automated checks run.

1. ✅ **GitHub repository — DONE.** Repo exists at **github.com/NikunjSadani/Opshub**; all the
   built code is pushed (branch `develop`). This unblocked pushing the code + CI running; the
   original "no git remote — code is local-only" note no longer applies.
2. ✅ **Google Cloud project — DONE (owner created 2026-08-24):** dedicated project **`opshub-506704`**
   (name "OpsHub", project number 310561620535), fresh/empty, separate from the loyalty platform.
   → **Terraform adjustment (my Phase-2 task):** the code currently *creates* the project
   (`google_project.opshub`); since the owner made it by hand, I switch that to *reference the
   existing* `opshub-506704` and drop the project-create + billing-link resources. ▶ Owner to
   confirm **billing is enabled** on it (Terraform + all resources need an active billing account).
   - **DB decision (owner, 2026-08-24):** OpsHub gets its **own dedicated Postgres instance**,
     NOT merged with the loyalty database. That instance also becomes the future home for other
     small internal apps. (Already how `infra/terraform/database.tf` is written — one dedicated
     instance, `opshub_prod` + `opshub_nonprod` databases on it.)
3. 🧑 **Firebase project** — create one (it can live inside the same GCP project). → *Real user
   login (staff sign-in). Today the app uses a local dev-login stand-in.*

**When Phase 1 is done, tell me — that's the trigger for everything else.**

---

## PHASE 2 — Stand up the infrastructure (mostly me, needs your GCP access)

**✅ INFRASTRUCTURE LIVE (2026-08-24, `opshub-506704`, asia-south1):** Terraform applied — all 37
resources up. Cloud SQL `opshub-db` (private, Enterprise/db-f1-micro) + `opshub_prod`/`opshub_nonprod`;
Cloud Run `opshub-api` (URL `https://opshub-api-2aadkzwkua-el.a.run.app`, currently the placeholder
image); GCS bucket `opshub-506704-files`; private VPC; 2 service accounts + IAM; Secret Manager
(DB URL + sweep secret); scheduler; **billing-budget alert** emailing on spend. `terraform plan` clean.
Auth: owner ADC (`nikunj.sadani28@gmail.com`), quota project set. Remaining Phase-2 code below.

4. 🤝 **Apply the infrastructure** — the Terraform is already written and validated; applying it
   creates the database, the file-storage bucket, Cloud Run, service accounts, and secrets.
   Needs your GCP org/billing access — you run the apply (I guide you step by step) or grant me
   scoped access.
5. ✅ **`GcsStorage` — DONE (`7be3475`, 2026-08-24):** durable file storage live (`GCS_BUCKET`
   env on Cloud Run selects it; api SA has objectAdmin). Round-trip-verified against the real
   `opshub-506704-files` bucket. Terraform now `ignore_changes` the image (config vs image split).
6. 🤖 **Real login (`FirebaseAuthProvider`)** — I wire the app's login to your Firebase project.
7. ✅ **GitHub deploy wiring — DONE + VERIFIED (2026-08-31).** WIF pool + OIDC provider
   (locked to `NikunjSadani/Opshub`) + deploy SA `github-deployer` (least-priv) created; GitHub
   secrets `WIF_PROVIDER`/`DEPLOY_SA` + var `GCP_PROJECT_ID_PROD` set; AR repo `opshub` already
   existed. `deploy-prod.yml` runs on **`workflow_dispatch`** — a test dispatch deployed clean
   (rev `opshub-api-00016-wdq`, health 200). Prod deploys are **manual** (Actions → Deploy prod →
   Run workflow — the click is your approval); a required-reviewer *pause* needs GitHub Pro/Team,
   which a free private repo doesn't have (the loyalty repo's `production` env has no reviewer
   rule either), so the deliberate trigger is the gate. ▶ residual: `terraform import` the WIF/SA
   into `infra/terraform/` (created out-of-band to avoid a risky full apply on the live project).

---

## PHASE 3 — First deploy + first admin (together)

**✅ FIRST DEPLOY DONE (2026-08-24):** built via Cloud Build → AR (`asia-south1-docker.pkg.dev/opshub-506704/opshub/api:64c1c0e`); migrated `opshub_prod` (`alembic upgrade head` via the `opshub-migrate` job — succeeded, schema created); deployed to Cloud Run `opshub-api`. **Health = 200 `{"status":"ok","env":"prod"}`** at https://opshub-api-2aadkzwkua-el.a.run.app. AR repo + Cloud Build API created during this pass. ⚠️ Fixed a self-inflicted `.gcloudignore` bug (unanchored `platform/` dropped `backend/app/platform` → migrate crash) — now anchored. **NOT yet usable by staff:** login needs Firebase wired (dev-auth is off in prod) + file uploads need `GcsStorage` (both below).

**✅ PHASE 3 ACCOMPLISHED (see the LIVE STATUS section):** the app is deployed to Cloud Run
(`opshub-api`, asia-south1), the first Administrator was bootstrapped, real Firebase login works,
and it is promoted to production at https://opshub.gifsy.in. The steps below are kept for the record.

8. ✅ **Deploy to staging** — pushing `develop` auto-deploys; the database migration runs
   automatically first, and we **verify it on Postgres** (some issues only show on the real DB).
9. ✅ **Bootstrap the first admin** — the very first Administrator user was created and real login
   verified per role.
10. ✅ **UAT**, then promoted to production. **Live at https://opshub.gifsy.in.**

---

## PHASE 4 — Turn on the challan-QR feature (last, after the app is live)

Do this only once the app is deployed and you've set up client PINs. The feature ships **off**
by a master switch, so deploying does NOT expose invoices until you deliberately turn it on.

11. 🧑 **Set the public URL** (`public_base_url`) so the QR points at your real domain.
12. 🧑 **Set a strong PIN per client** (ideally system-generated) — the security audit flagged
    that a short/weak PIN is the only real risk, so use long/random ones.
13. 🤝 **Enable edge rate-limiting** (Cloud Armor per-IP at the load balancer) — the audit's
    prerequisite before public invoice access is turned on.
14. 🧑 **Flip the switch** (`qr_invoice_access_enabled = true`).
15. 🤝 **Verify live** — one real scan → password → invoice on the deployed URL.

---

## Also parked (not blocking go-live; decide when you get to them)
- **Scanned-invoice OCR** (paid engine) — scanned PDFs currently park as "needs OCR". Zero-cost
  text extraction already works for normal PDFs.
- **Your entity GSTIN** — needed only if you want automatic buyer-GSTIN validation on invoices.
- **Google-Sheet sync / push email + WhatsApp reminders / e-invoice + e-way** — the rest of the
  cloud wave; sequence them after go-live.

---

## TL;DR — what to do first
**Create the GitHub repo, the GCP project (with billing), and the Firebase project — in that
order of ease — then tell me.** That single step unblocks everything; I take it from there and
walk you through each following step.

_Living doc — see `RESUME.md` for the technical state. Created 2026-08-24._
