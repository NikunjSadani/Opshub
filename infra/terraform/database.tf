# DB Option B: ONE small single-zone Postgres instance hosting BOTH the prod and
# non-prod databases. Private IP only. Backups + PITR on. NEVER co-located with
# Loyaltybase (this is its own project/instance).
resource "google_sql_database_instance" "opshub" {
  project             = data.google_project.opshub.project_id
  name                = "opshub-db"
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = true
  depends_on          = [google_service_networking_connection.psa]

  settings {
    tier              = var.db_tier
    edition           = "ENTERPRISE" # shared-core db-f1-micro is Enterprise-only (Plus needs db-perf-*)
    availability_type = "ZONAL"      # single-zone, no HA (Option B)
    disk_size         = 10
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled    = false # private IP only
      private_network = google_compute_network.vpc.id
    }
  }
}

resource "google_sql_database" "prod" {
  project  = data.google_project.opshub.project_id
  name     = "opshub_prod"
  instance = google_sql_database_instance.opshub.name
}

resource "google_sql_database" "nonprod" {
  project  = data.google_project.opshub.project_id
  name     = "opshub_nonprod"
  instance = google_sql_database_instance.opshub.name
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "app" {
  project  = data.google_project.opshub.project_id
  name     = var.db_user
  instance = google_sql_database_instance.opshub.name
  password = random_password.db.result
}
