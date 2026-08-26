# Least-privilege service accounts. The API SA can reach Cloud SQL, its own bucket,
# and its secrets — and NOTHING in any other project (project isolation does the rest).
resource "google_service_account" "api" {
  project      = data.google_project.opshub.project_id
  account_id   = "opshub-api"
  display_name = "OpsHub API (Cloud Run)"
}

resource "google_service_account" "migrate" {
  project      = data.google_project.opshub.project_id
  account_id   = "opshub-migrate"
  display_name = "OpsHub Alembic migrate job"
}

resource "google_project_iam_member" "api_sql" {
  project = data.google_project.opshub.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "migrate_sql" {
  project = data.google_project.opshub.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_storage_bucket_iam_member" "api_bucket" {
  bucket = google_storage_bucket.files.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_db_prod" {
  project   = data.google_project.opshub.project_id
  secret_id = google_secret_manager_secret.db_url_prod.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate_db_prod" {
  project   = data.google_project.opshub.project_id
  secret_id = google_secret_manager_secret.db_url_prod.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_secret_manager_secret_iam_member" "api_sweep_secret" {
  project   = data.google_project.opshub.project_id
  secret_id = google_secret_manager_secret.sweep_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# The API serves a PUBLIC web app that does its OWN auth (Firebase in-app + the
# backend's per-request checks), so the Cloud Run service must be publicly
# invokable at the IAM layer. Without this the SPA can't load AND Cloud Scheduler's
# secret-gated sweep call (scheduler.tf) would 403 before the app's X-Sweep-Secret
# check ever runs. In-app auth — not Cloud Run IAM — is the gate here.
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = data.google_project.opshub.project_id
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
