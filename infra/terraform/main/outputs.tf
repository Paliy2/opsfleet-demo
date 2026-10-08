output "ingress_ip" {
  value = google_compute_global_address.ingress.address
}

output "base_domain" {
  value = local.base_domain
}

output "cluster_name" {
  value = google_container_cluster.autopilot.name
}

output "cluster_location" {
  value = google_container_cluster.autopilot.location
}

output "artifact_repo" {
  value = "${var.region}-docker.pkg.dev/${var.project}/${google_artifact_registry_repository.images.repository_id}"
}

output "sql_private_ip" {
  value = google_sql_database_instance.postgres.private_ip_address
}

output "app_gsa_email" {
  value = google_service_account.app.email
}

output "eso_gsa_email" {
  value = google_service_account.eso.email
}

output "langfuse_bucket" {
  value = google_storage_bucket.langfuse.name
}

output "artifacts_bucket" {
  value = google_storage_bucket.artifacts.name
}
