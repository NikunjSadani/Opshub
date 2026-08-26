output "project_id" {
  value = data.google_project.opshub.project_id
}

output "sql_connection_name" {
  value = google_sql_database_instance.opshub.connection_name
}

output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "files_bucket" {
  value = google_storage_bucket.files.name
}

output "api_service_account" {
  value = google_service_account.api.email
}

output "numbering_sweep_job" {
  value = google_cloud_scheduler_job.numbering_sweep.name
}
