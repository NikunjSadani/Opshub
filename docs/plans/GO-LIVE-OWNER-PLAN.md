# OpsHub Go-Live — Owner Action Plan

**The one thing to understand:** almost everything left is a **one-time cloud setup** that you
do **once**, and it unlocks *all* the parked work at once — going live, real logins, durable
file storage, AND the challan-QR feature. It is not per-feature. Nothing is deployed today;
all the code is built, tested, and sitting on the local `develop` branch.

The plan is 4 phases. **You only truly block Phase 1** (three accounts). After that I do most
of the work; you approve/configure a few things.

Legend: 🧑 = only you can do it · 🤝 = we do together (I write, you provide access/approve) ·
🤖 = I do it.

---

## ▶ PHASE 1 — Set up three accounts (do this FIRST; only you can)

These three are the true prerequisites. Until they exist, nothing can deploy. They can be
created in parallel, but **start with the GitHub repo** — it's quick and lets me push all the
work (also your off-machine backup) and lets the automated checks run.

1. 🧑 **GitHub repository** — create a private repo (e.g. `gifsy-opshub`). Send me the URL and
   give push access (or add me as a collaborator). → *Unblocks: pushing all the built code +
   CI running.*  Today there is **no git remote** — the code is local-only.
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
7. 🤝 **GitHub deploy wiring** — a few GitHub secrets/vars (`WIF_PROVIDER`, `DEPLOY_SA`,
   `GCP_PROJECT_ID`), an Artifact Registry repo, and a "production" approval gate with a
   required reviewer (so no prod deploy happens without a click from you). I prepare the exact
   values; you paste them into GitHub settings.

---

## PHASE 3 — First deploy + first admin (together)

**✅ FIRST DEPLOY DONE (2026-08-24):** built via Cloud Build → AR (`asia-south1-docker.pkg.dev/opshub-506704/opshub/api:64c1c0e`); migrated `opshub_prod` (`alembic upgrade head` via the `opshub-migrate` job — succeeded, schema created); deployed to Cloud Run `opshub-api`. **Health = 200 `{"status":"ok","env":"prod"}`** at https://opshub-api-2aadkzwkua-el.a.run.app. AR repo + Cloud Build API created during this pass. ⚠️ Fixed a self-inflicted `.gcloudignore` bug (unanchored `platform/` dropped `backend/app/platform` → migrate crash) — now anchored. **NOT yet usable by staff:** login needs Firebase wired (dev-auth is off in prod) + file uploads need `GcsStorage` (both below).

8. 🤝 **Deploy to staging** — pushing `develop` auto-deploys; the database migration runs
   automatically first, and we **verify it on Postgres** (some issues only show on the real DB).
9. 🤝 **Bootstrap the first admin** — create the very first Administrator user (the system starts
   with no one holding the admin role). Then verify real login works for each role.
10. 🤝 **UAT on staging**, then promote to production (the required-reviewer gate = your click).

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
