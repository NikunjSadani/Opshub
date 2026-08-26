# API service — scale-to-zero, Direct VPC egress (private ranges), Cloud SQL mounted.
resource "google_cloud_run_v2_service" "api" {
  project  = data.google_project.opshub.project_id
  name     = "opshub-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Service-level scaling defaults Cloud Run sets server-side (declare to avoid a perpetual diff).
  scaling {
    min_instance_count = 0
  }

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
      # Selects the GcsStorage backend (durable blobs) — the api SA has objectAdmin on this bucket.
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.files.name
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
      # SWEEP_SECRET — gates POST /api/v1/numbering/sweep (fail-closed: 503 if unset).
      # Cloud Scheduler (scheduler.tf) sends the same value as the X-Sweep-Secret header.
      env {
        name = "SWEEP_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.sweep_secret.secret_id
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

  # The running image is deployed by CI / `gcloud run deploy` (build → push → deploy), NOT
  # Terraform — var.image is only the initial placeholder. Ignore image drift so `terraform
  # apply` manages config (env, scaling, VPC, secrets) without reverting the deployed build.
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# Alembic migrate — runs IN-VPC (the prod DB is private-IP; unreachable from a laptop/CI).
resource "google_cloud_run_v2_job" "migrate" {
  project  = data.google_project.opshub.project_id
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

  # Image deployed by CI (job update → execute), not Terraform — ignore image drift.
  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].image]
  }
}
