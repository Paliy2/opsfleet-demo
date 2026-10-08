resource "google_artifact_registry_repository" "images" {
  repository_id = "opsfleet"
  location      = var.region
  format        = "DOCKER"
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

resource "google_storage_bucket" "langfuse" {
  name                        = "${var.project}-langfuse"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
}

resource "google_storage_hmac_key" "langfuse" {
  service_account_email = google_service_account.langfuse.email
}
