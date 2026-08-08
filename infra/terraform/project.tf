# Separate GCP project (isolated IAM/quota/billing blast radius), same billing account.
resource "google_project" "opshub" {
  name            = var.project_name
  project_id      = var.project_id
  billing_account = var.billing_account
  org_id          = var.org_id != "" ? var.org_id : null
  folder_id       = var.folder_id != "" ? var.folder_id : null
}

locals {
  apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "compute.googleapis.com",
    "servicenetworking.googleapis.com",
    "iam.googleapis.com",
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = google_project.opshub.project_id
  service            = each.value
  disable_on_destroy = false
}
