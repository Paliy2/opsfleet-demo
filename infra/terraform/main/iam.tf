resource "google_service_account" "app" {
  account_id   = "opsfleet-app"
  display_name = "OpsFleet app workloads"
}

resource "google_service_account" "eso" {
  account_id   = "opsfleet-eso"
  display_name = "External Secrets Operator"
}

resource "google_service_account" "langfuse" {
  account_id   = "opsfleet-langfuse"
  display_name = "Langfuse blob storage"
}

resource "google_service_account" "bifrost" {
  account_id   = "opsfleet-bifrost"
  display_name = "Bifrost LLM gateway (Vertex AI)"
}

resource "google_project_iam_member" "app_bigquery" {
  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_dlp" {
  project = var.project
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "app_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.app.email}"
}

resource "google_project_iam_member" "eso_secrets" {
  project = var.project
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.eso.email}"
}

resource "google_project_iam_member" "bifrost_vertex" {
  project = var.project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.bifrost.email}"
}

resource "google_storage_bucket_iam_member" "app_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.app.email}"
}

resource "google_storage_bucket_iam_member" "langfuse_blobs" {
  bucket = google_storage_bucket.langfuse.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.langfuse.email}"
}

resource "google_service_account_iam_member" "app_workload_identity" {
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[${var.namespace}/opsfleet-app]"

  depends_on = [google_container_cluster.autopilot]
}

resource "google_service_account_iam_member" "eso_workload_identity" {
  service_account_id = google_service_account.eso.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[external-secrets/external-secrets]"

  depends_on = [google_container_cluster.autopilot]
}

resource "google_service_account_iam_member" "bifrost_workload_identity" {
  service_account_id = google_service_account.bifrost.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project}.svc.id.goog[${var.namespace}/bifrost]"

  depends_on = [google_container_cluster.autopilot]
}
