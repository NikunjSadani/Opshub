# VPC + Private Service Access so Cloud SQL has a PRIVATE IP (no public exposure)
# and Cloud Run reaches it via Direct VPC egress (no Serverless VPC connector cost).
resource "google_compute_network" "vpc" {
  project                 = google_project.opshub.project_id
  name                    = "opshub-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  project       = google_project.opshub.project_id
  name          = "opshub-subnet"
  ip_cidr_range = "10.20.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id
}

resource "google_compute_global_address" "psa" {
  project       = google_project.opshub.project_id
  name          = "opshub-psa-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "psa" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa.name]
}
