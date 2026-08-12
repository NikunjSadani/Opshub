# Hourly numbering-sweep trigger. Cloud Scheduler POSTs the secret-gated sweep endpoint,
# which voids orphaned RESERVED numbering allocations. It takes NO user login.
#
# Auth = shared-secret header (X-Sweep-Secret), NOT OIDC: the endpoint is gated by the app's
# SWEEP_SECRET setting, not by Cloud Run IAM. This assumes the opshub-api service is invokable
# without Cloud Run IAM auth (allow-unauthenticated — the same reachability the browser/Firebase
# clients rely on). If the service is ever locked to IAM invokers, add an `oidc_token` block here
# plus a scheduler service account granted roles/run.invoker; the X-Sweep-Secret header still
# provides the app-level gate on top of that.
resource "google_cloud_scheduler_job" "numbering_sweep" {
  project     = google_project.opshub.project_id
  region      = var.region
  name        = "opshub-numbering-sweep"
  description = "Hourly: void orphaned RESERVED numbering allocations via the secret-gated sweep endpoint."
  schedule    = "0 * * * *" # top of every hour
  time_zone   = "Asia/Kolkata"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.api.uri}/api/v1/numbering/sweep"
    headers = {
      "X-Sweep-Secret" = var.sweep_secret
      "Content-Type"   = "application/json"
    }
    body = base64encode("{}") # Cloud Scheduler requires a base64-encoded body; empty JSON object.
  }

  depends_on = [
    google_cloud_run_v2_service.api,
    google_project_service.apis,
  ]
}
