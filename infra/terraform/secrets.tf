# DATABASE_URL for prod, in Secret Manager. NOTE the `@localhost/...?host=/cloudsql/...`
# form — an EMPTY host breaks the Alembic/psycopg migrate engine (a Loyaltybase scar).
resource "google_secret_manager_secret" "db_url_prod" {
  project   = google_project.opshub.project_id
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
