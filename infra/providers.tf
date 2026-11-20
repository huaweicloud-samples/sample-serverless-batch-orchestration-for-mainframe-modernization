# Authentication is resolved from the operator environment
# (HW_ACCESS_KEY / HW_SECRET_KEY, optionally HW_REGION_NAME) or from the
# variables declared in variables.tf. No credential is ever stored in this
# repository.
provider "huaweicloud" {
  region         = local.region
  access_key     = var.access_key == "" ? null : var.access_key
  secret_key     = var.secret_key == "" ? null : var.secret_key
  security_token = var.security_token == "" ? null : var.security_token
  cloud          = var.cloud
  max_retries    = 3
}
