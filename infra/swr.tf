# ---------------------------------------------------------------------------
# Image registry: the organization and the private repository that hold the
# batch container image built by src/job/build-and-push.sh.
# ---------------------------------------------------------------------------

resource "huaweicloud_swr_organization" "main" {
  name = var.swr_organization
}

resource "huaweicloud_swr_repository" "batch_stub" {
  name         = var.swr_repository
  organization = huaweicloud_swr_organization.main.name
  is_public    = false
  description  = "Batch job container: reads a dataset from object storage, transforms it and writes the result back"
}
