# The OpsHub project is created + billing-linked by the owner in the console; Terraform
# REFERENCES it (isolated IAM/quota/billing blast radius, same billing account). We only
# manage the resources INSIDE it, never the project lifecycle.
data "google_project" "opshub" {
  project_id = var.project_id
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
    "cloudscheduler.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = data.google_project.opshub.project_id
  service            = each.value
  disable_on_destroy = false
}
