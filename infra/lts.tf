# ---------------------------------------------------------------------------
# Log sink shared by the orchestration functions.
# ---------------------------------------------------------------------------

resource "huaweicloud_lts_group" "batch" {
  group_name  = local.full_name
  ttl_in_days = var.log_retention_days
  tags        = local.common_tags
}

resource "huaweicloud_lts_stream" "orchestrator" {
  group_id    = huaweicloud_lts_group.batch.id
  stream_name = "${local.full_name}-orchestrator"
  ttl_in_days = var.log_retention_days
  tags        = local.common_tags
}

resource "huaweicloud_lts_stream" "validation" {
  group_id    = huaweicloud_lts_group.batch.id
  stream_name = "${local.full_name}-validation"
  ttl_in_days = var.log_retention_days
  tags        = local.common_tags
}
