locals {
  databases = toset(["opsfleet", "temporal", "temporal_visibility", "keycloak", "langfuse"])
  db_users  = toset(["opsfleet", "temporal", "keycloak", "langfuse"])
}

resource "google_sql_database_instance" "postgres" {
  name                = "opsfleet-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = false

  settings {
    tier              = "db-custom-1-3840"
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_size         = 20
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }

    backup_configuration {
      enabled = false
    }

    database_flags {
      name  = "max_connections"
      value = "200"
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

resource "google_sql_database" "databases" {
  for_each = local.databases
  name     = each.value
  instance = google_sql_database_instance.postgres.name
}

resource "random_password" "db" {
  for_each = local.db_users
  length   = 32
  special  = false
}

resource "google_sql_user" "users" {
  for_each = local.db_users
  name     = each.value
  instance = google_sql_database_instance.postgres.name
  password = random_password.db[each.value].result
}
