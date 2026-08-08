# API service — scale-to-zero, Direct VPC egress (private ranges), Cloud SQL mounted.
resource "google_cloud_run_v2_service" "api" {
  project  = google_project.opshub.project_id
  name     = "opshub-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.api.email

    scaling {
      min_instance_count = 0 # scale to zero
      max_instance_count = 4
    }

    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.subnet.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = var.image
      env {
        name  = "ENV"
        value = "prod"
      }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url_prod.secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.opshub.connection_name]
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# Alembic migrate — runs IN-VPC (the prod DB is private-IP; unreachable from a laptop/CI).
resource "google_cloud_run_v2_job" "migrate" {
  project  = google_project.opshub.project_id
  name     = "opshub-migrate"
  location = var.region

  template {
    template {
      service_account = google_service_account.migrate.email

      vpc_access {
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.subnet.id
        }
        egress = "PRIVATE_RANGES_ONLY"
      }

      containers {
        image   = var.image
        command = ["alembic", "upgrade", "head"]
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_url_prod.secret_id
              version = "latest"
            }
          }
        }
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.opshub.connection_name]
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}
