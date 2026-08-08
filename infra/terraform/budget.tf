# Per-project budget + threshold alerts — catches a cost spike (runaway OCR/LLM,
# storage blowup) in isolation, the whole reason for a separate project.
resource "google_billing_budget" "opshub" {
  billing_account = var.billing_account
  display_name    = "OpsHub monthly budget"

  budget_filter {
    projects = ["projects/${google_project.opshub.number}"]
  }

  amount {
    specified_amount {
      units = tostring(var.budget_amount)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
