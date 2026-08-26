# DATABASE_URL for prod, in Secret Manager. NOTE the `@localhost/...?host=/cloudsql/...`
# form — an EMPTY host breaks the Alembic/psycopg migrate engine (a Loyaltybase scar).
resource "google_secret_manager_secret" "db_url_prod" {
  project   = data.google_project.opshub.project_id
  secret_id = "opshub-database-url-prod"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_url_prod" {
  secret = google_secret_manager_secret.db_url_prod.id
  secret_data = format(
    "postgresql+psycopg://%s:%s@localhost/opshub_prod?host=/cloudsql/%s",
    var.db_user,
    random_password.db.result,
    google_sql_database_instance.opshub.connection_name,
  )
}

# SWEEP_SECRET — shared secret gating POST /api/v1/numbering/sweep. The API service reads it
# as the SWEEP_SECRET env (mounted in cloudrun.tf); Cloud Scheduler sends the same value in the
# X-Sweep-Secret header (scheduler.tf). Value comes from the sensitive var.sweep_secret.
resource "google_secret_manager_secret" "sweep_secret" {
  project   = data.google_project.opshub.project_id
  secret_id = "opshub-sweep-secret-prod"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "sweep_secret" {
  secret      = google_secret_manager_secret.sweep_secret.id
  secret_data = var.sweep_secret
}
