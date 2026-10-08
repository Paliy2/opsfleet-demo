terraform {
  required_version = ">= 1.9"

  backend "gcs" {
    bucket = "opsfleet-demo-56d0-tfstate"
    prefix = "bootstrap"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}
