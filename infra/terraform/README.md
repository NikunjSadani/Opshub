# OpsHub infrastructure (Terraform)

Provisions the **separate `gifsy-opshub` GCP project** and everything in it. Validated (`terraform validate`) but **not applied** — apply needs owner GCP org/billing access.

## What it creates
- **Separate project** (same billing account) + required APIs.
- **VPC + Private Service Access** → Cloud SQL private IP, Cloud Run **Direct VPC egress** (no connector).
- **Cloud SQL — Option B**: one small **single-zone** Postgres instance (no HA), backups + PITR, hosting **both** `opshub_prod` and `opshub_nonprod` databases.
- **GCS** bucket (isolated, SA-scoped) with a lifecycle rule.
- **Least-privilege service accounts** (api, migrate) — reach only OpsHub's SQL/bucket/secrets.
- **Secret Manager** `opshub-database-url-prod` (uses the `@localhost/…?host=/cloudsql/…` form — an empty host breaks the migrate engine).
- **Cloud Run**: `opshub-api` service (**scale-to-zero**) + `opshub-migrate` **in-VPC Alembic job** (`alembic upgrade head`).
- **Billing budget** + 50/90/100% threshold alerts (per-project spike detection).

## Apply (owner)
```bash
cp terraform.tfvars.example terraform.tfvars   # fill project_id, billing_account, org/folder
terraform init
terraform plan
terraform apply
```
> `terraform.tfvars` and state are gitignored. Use a remote state backend (GCS) before real use.

## Notes
- `var.image` defaults to a placeholder; CI/CD sets the real image after building.
- `db_tier` = smallest; verify against current Enterprise-edition tiers at apply time.
- Verify the Alembic migration on Postgres (enum DDL) before the first real `apply` of the migrate job.
