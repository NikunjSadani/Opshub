variable "project_id" {
  type        = string
  description = "The (separate) GCP project id for OpsHub, e.g. gifsy-opshub."
}

variable "project_name" {
  type    = string
  default = "Gifsy OpsHub"
}

variable "billing_account" {
  type        = string
  description = "Billing account id (SAME account as Loyaltybase — one invoice, isolated project)."
}

variable "org_id" {
  type    = string
  default = ""
}

variable "folder_id" {
  type    = string
  default = ""
}

variable "region" {
  type    = string
  default = "asia-south1"
}

variable "db_tier" {
  type        = string
  default     = "db-f1-micro" # smallest / single-zone (Option B)
  description = "Cloud SQL tier — keep small; verify against current Enterprise-edition tiers."
}

variable "db_user" {
  type    = string
  default = "opshub"
}

variable "image" {
  type        = string
  default     = "gcr.io/cloudrun/hello"
  description = "Container image for the API + migrate job (set by CI/CD after build)."
}

variable "budget_amount" {
  type        = number
  default     = 50
  description = "Monthly budget in the billing account's currency (spike-alert threshold)."
}

variable "budget_alert_emails" {
  type    = list(string)
  default = []
}

variable "sweep_secret" {
  type        = string
  sensitive   = true
  description = "Shared secret for POST /api/v1/numbering/sweep (sent as X-Sweep-Secret; compared against the app's SWEEP_SECRET setting, fail-closed). Set via TF_VAR_sweep_secret or a *.tfvars file — never commit."
}
