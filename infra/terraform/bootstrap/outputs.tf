output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github_actions.name
}

output "deployer_sa_email" {
  value = google_service_account.deployer.email
}

output "state_bucket" {
  value = google_storage_bucket.state.name
}

output "project_number" {
  value = data.google_project.this.number
}
