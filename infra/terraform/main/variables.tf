variable "project" {
  type    = string
  default = "opsfleet-demo-56d0"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "billing_account" {
  type    = string
  default = "01AF2E-93D359-4B1AD9"
}

variable "budget_amount" {
  type    = number
  default = 100
}

variable "github_repo" {
  type    = string
  default = "Paliy2/opsfleet-demo"
}

variable "namespace" {
  type    = string
  default = "opsfleet"
}

variable "demo_users" {
  type = map(object({
    role       = string
    first_name = string
    last_name  = string
  }))
  default = {
    ceo       = { role = "ceo", first_name = "Chief", last_name = "Executive" }
    mgr_a     = { role = "manager", first_name = "Manager", last_name = "A" }
    mgr_b     = { role = "manager", first_name = "Manager", last_name = "B" }
    mgr_empty = { role = "manager", first_name = "Manager", last_name = "Low Data" }
  }
}
