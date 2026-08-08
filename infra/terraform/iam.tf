# Least-privilege service accounts. The API SA can reach Cloud SQL, its own bucket,
# and its secrets — and NOTHING in any other project (project isolation does the rest).
resource "google_service_account" "api" {
  project      = google_project.opshub.project_id
  account_id   = "opshub-api"
  display_name = "OpsHub API (Cloud Run)"
}

resource "google_service_account" "migrate" {
  project      = google_project.opshub.project_id
  account_id   = "opshub-migrate"
  display_name = "OpsHub Alembic migrate job"
}

resource "google_project_iam_member" "api_sql" {
  project = google_project.opshub.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "migrate_sql" {
  project = google_project.opshub.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.migrate.email}"
}

resource "google_storage_bucket_iam_member" "api_bucket" {
  bucket = google_storage_bucket.files.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_db_prod" {
  project   = google_project.opshub.project_id
  secret_id = google_secret_manager_secret.db_url_prod.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

resource "google_secret_manager_secret_iam_member" "migrate_db_prod" {
  project   = google_project.opshub.project_id
  secret_id = google_secret_manager_secret.db_url_prod.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.migrate.email}"
}
