locals {
  # Deployment region. Pinned on purpose: every endpoint, availability zone and
  # time zone in this stack is expressed for this region.
  region = "cn-north-4"

  account_id = data.huaweicloud_account.current.id
  project_id = data.huaweicloud_account.current.current_project_id

  full_name = "${var.name_prefix}-${var.environment}"

  # Object storage ---------------------------------------------------------
  data_bucket = "batch-data-${var.environment}-${local.account_id}"

  inbound_prefix    = "inbound/transactions/"
  processing_prefix = "processing/"
  outbound_prefix   = "outbound/"
  runs_prefix       = "runs/"
  code_prefix       = "functions/"

  # One JSON object holding the non-processing dates, read by the business day
  # check. Its absence is not an error: the check then falls back to the
  # weekend rule alone.
  calendar_object = "calendars/non-processing-days.json"

  # Prefixes that hold the intermediate datasets produced by the batch
  # containers. They are moved to the infrequent access class once idle.
  intermediate_prefix = local.processing_prefix

  # Container image --------------------------------------------------------
  batch_image = "${huaweicloud_swr_organization.main.login_server}/${var.swr_organization}/${var.swr_repository}:${var.swr_image_tag}"

  # FunctionGraph code packages. The build script writes both archives into
  # the .build directory next to this module. For code_type "obs" the function
  # service reads the package from a full object URL - a bare "<bucket>/<key>"
  # pair is rejected with FSS.1010 "Invalid URL", so the bucket domain has to be
  # part of the value.
  orchestrator_zip_key = "${local.code_prefix}batch-orchestrator.zip"
  checks_zip_key       = "${local.code_prefix}batch-checks.zip"
  build_dir            = "${path.module}/.build"

  orchestrator_code_url = "https://${huaweicloud_obs_bucket.data.bucket_domain_name}/${local.orchestrator_zip_key}"
  checks_code_url       = "https://${huaweicloud_obs_bucket.data.bucket_domain_name}/${local.checks_zip_key}"

  # Cloud Eye custom metrics. A custom namespace has to stay inside the
  # character set the metric interface accepts: letters, digits and
  # underscores only.
  metric_namespace = "${upper(replace(var.name_prefix, "-", "_"))}_ORCHESTRATOR"

  metric_failures = "batch_run_failed"
  metric_duration = "batch_run_duration"
  metric_success  = "batch_run_success"

  common_tags = {
    managed_by  = "terraform"
    environment = var.environment
    solution    = local.full_name
  }

  # Regional service endpoints reached from inside the private subnets over
  # the internal service network. This address block is the fixed range that
  # the regional services answer on and is not derived from anything.
  internal_service_network_cidr = "100.125.0.0/16"
  obs_endpoint                  = "obs.${local.region}.${var.cloud}"
  smn_endpoint                  = "smn.${local.region}.${var.cloud}"
  ces_endpoint                  = "ces.${local.region}.${var.cloud}"
}
