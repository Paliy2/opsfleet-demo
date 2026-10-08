resource "random_password" "generated" {
  for_each = toset([
    "bifrost-virtual-key",
    "pii-hash-salt",
    "keycloak-admin-password",
    "chainlit-auth-secret",
    "chainlit-oauth-client-secret",
    "langfuse-nextauth-secret",
    "langfuse-salt",
    "langfuse-admin-password",
    "langfuse-clickhouse-password",
    "langfuse-redis-password",
  ])
  length  = 40
  special = false
}

resource "random_password" "demo_user" {
  for_each = var.demo_users
  length   = 20
  special  = false
}

resource "random_id" "langfuse_encryption_key" {
  byte_length = 32
}

resource "random_id" "langfuse_public_key" {
  byte_length = 16
}

resource "random_id" "langfuse_secret_key" {
  byte_length = 24
}

locals {
  demo_passwords = { for user, _ in var.demo_users : user => random_password.demo_user[user].result }

  keycloak_realm = templatefile("${path.module}/templates/realm.json.tftpl", {
    base_domain     = local.base_domain
    chainlit_secret = random_password.generated["chainlit-oauth-client-secret"].result
    users           = var.demo_users
    passwords       = local.demo_passwords
  })

  secret_values = {
    "bifrost-virtual-key"          = "sk-bf-${random_password.generated["bifrost-virtual-key"].result}"
    "pii-hash-salt"                = random_password.generated["pii-hash-salt"].result
    "db-opsfleet-password"         = random_password.db["opsfleet"].result
    "db-temporal-password"         = random_password.db["temporal"].result
    "db-keycloak-password"         = random_password.db["keycloak"].result
    "db-langfuse-password"         = random_password.db["langfuse"].result
    "keycloak-admin-password"      = random_password.generated["keycloak-admin-password"].result
    "keycloak-realm"               = local.keycloak_realm
    "demo-credentials"             = jsonencode(local.demo_passwords)
    "chainlit-auth-secret"         = random_password.generated["chainlit-auth-secret"].result
    "chainlit-oauth-client-secret" = random_password.generated["chainlit-oauth-client-secret"].result
    "langfuse-nextauth-secret"     = random_password.generated["langfuse-nextauth-secret"].result
    "langfuse-salt"                = random_password.generated["langfuse-salt"].result
    "langfuse-encryption-key"      = random_id.langfuse_encryption_key.hex
    "langfuse-public-key"          = "pk-lf-${random_id.langfuse_public_key.hex}"
    "langfuse-secret-key"          = "sk-lf-${random_id.langfuse_secret_key.hex}"
    "langfuse-admin-password"      = random_password.generated["langfuse-admin-password"].result
    "langfuse-clickhouse-password" = random_password.generated["langfuse-clickhouse-password"].result
    "langfuse-redis-password"      = random_password.generated["langfuse-redis-password"].result
    "langfuse-s3-access-key"       = google_storage_hmac_key.langfuse.access_id
    "langfuse-s3-secret-key"       = google_storage_hmac_key.langfuse.secret
  }
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "gemini-api-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "generated" {
  for_each  = nonsensitive(toset(keys(local.secret_values)))
  secret_id = each.value

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = nonsensitive(toset(keys(local.secret_values)))
  secret      = google_secret_manager_secret.generated[each.value].id
  secret_data = local.secret_values[each.value]
}
