# Isolated OpsHub bucket (uploaded blobs + generated docs). SA-scoped in iam.tf —
# OpsHub SAs can reach this and NOT any Loyaltybase bucket.
resource "google_storage_bucket" "files" {
  project                     = data.google_project.opshub.project_id
  name                        = "${var.project_id}-files"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30 # transient uploads; the DB register is the record of truth
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
}
