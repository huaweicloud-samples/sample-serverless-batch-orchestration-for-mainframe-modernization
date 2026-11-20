# ---------------------------------------------------------------------------
# Function code packages. Both archives are produced by
# scripts/package_functions.py and land in the .build directory next to this
# module before the deployment is applied.
# ---------------------------------------------------------------------------

resource "huaweicloud_obs_bucket_object" "orchestrator_package" {
  bucket       = huaweicloud_obs_bucket.data.bucket
  key          = local.orchestrator_zip_key
  source       = "${local.build_dir}/batch-orchestrator.zip"
  content_type = "application/zip"

  tags = local.common_tags
}

resource "huaweicloud_obs_bucket_object" "checks_package" {
  bucket       = huaweicloud_obs_bucket.data.bucket
  key          = local.checks_zip_key
  source       = "${local.build_dir}/batch-checks.zip"
  content_type = "application/zip"

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Runtime configuration shared by every function.
# ---------------------------------------------------------------------------

locals {
  # Behaviour copied from the batch retry policy: two attempts, ten seconds
  # between them, no backoff growth and a full randomisation of the wait.
  job_retry_max_attempts    = 2
  job_retry_base_seconds    = 10
  job_poll_interval_seconds = 10
  job_ttl_seconds           = 3600

  # A Rate timer counts whole minutes and its smallest step is one minute, so the
  # resume interval is expressed in minutes and floored at one. A sub-minute
  # value would otherwise be rejected as an invalid rate expression.
  resume_interval_minutes = format("%dm", max(1, floor(var.resume_interval_seconds / 60)))

  common_env = {
    DEPLOYMENT_REGION      = local.region
    DEPLOYMENT_ENVIRONMENT = var.environment
    HW_PROJECT_ID          = local.project_id
    OBS_BUCKET             = huaweicloud_obs_bucket.data.bucket
    OBS_ENDPOINT           = "https://${local.obs_endpoint}"
    LOG_LEVEL              = "INFO"
  }

  data_env = {
    INBOUND_PREFIX    = local.inbound_prefix
    PROCESSING_PREFIX = local.processing_prefix
    OUTBOUND_PREFIX   = local.outbound_prefix
    RUNS_PREFIX       = local.runs_prefix
    CALENDAR_OBJECT   = local.calendar_object
    MAX_AGE_DAYS      = tostring(var.max_input_age_days)
    SCHEDULE_TIMEZONE = var.schedule_timezone
  }
}

# ---------------------------------------------------------------------------
# Validation functions. Each one answers a single question about the incoming
# data and is invoked by the orchestration layer.
# ---------------------------------------------------------------------------

resource "huaweicloud_fgs_function" "file_check" {
  name             = "${local.full_name}-file-check"
  app              = var.name_prefix
  description      = "Verifies that every input dataset a batch job needs is present and fresh"
  runtime          = "Python3.9"
  handler          = "index.handler_file_check"
  memory_size      = 256
  timeout          = 30
  code_type        = "obs"
  code_url         = local.checks_code_url
  agency           = huaweicloud_identity_agency.functions.name
  vpc_id           = huaweicloud_vpc.main.id
  network_id       = huaweicloud_vpc_subnet.a.id
  max_instance_num = "2"
  enable_lts_log   = true
  log_group_id     = huaweicloud_lts_group.batch.id
  log_stream_id    = huaweicloud_lts_stream.validation.id
  log_group_name   = huaweicloud_lts_group.batch.group_name
  log_stream_name  = huaweicloud_lts_stream.validation.stream_name
  user_data        = jsonencode(merge(local.common_env, local.data_env))
  tags             = local.common_tags

  lifecycle {
    ignore_changes       = [code_url]
    replace_triggered_by = [huaweicloud_obs_bucket_object.checks_package]
  }
}

resource "huaweicloud_fgs_function" "calendar_check" {
  name             = "${local.full_name}-calendar-check"
  app              = var.name_prefix
  description      = "Decides whether the scheduled launch falls on a business day"
  runtime          = "Python3.9"
  handler          = "index.handler_calendar_check"
  memory_size      = 128
  timeout          = 15
  code_type        = "obs"
  code_url         = local.checks_code_url
  agency           = huaweicloud_identity_agency.functions.name
  vpc_id           = huaweicloud_vpc.main.id
  network_id       = huaweicloud_vpc_subnet.a.id
  max_instance_num = "2"
  enable_lts_log   = true
  log_group_id     = huaweicloud_lts_group.batch.id
  log_stream_id    = huaweicloud_lts_stream.validation.id
  log_group_name   = huaweicloud_lts_group.batch.group_name
  log_stream_name  = huaweicloud_lts_stream.validation.stream_name
  user_data        = jsonencode(merge(local.common_env, local.data_env))
  tags             = local.common_tags

  lifecycle {
    ignore_changes       = [code_url]
    replace_triggered_by = [huaweicloud_obs_bucket_object.checks_package]
  }
}

# ---------------------------------------------------------------------------
# Orchestration layer: launches runs, drives batch containers as jobs, polls
# them to a terminal state and keeps the run state in object storage.
# ---------------------------------------------------------------------------

locals {
  # The function service refuses an environment variable whose value is empty
  # ("FSS.1009 Invalid function UserData"), so the cluster access values that are
  # filled in only after the first apply are dropped from the payload instead of
  # being sent as empty strings. The orchestration layer treats an absent value
  # and an empty value the same way.
  orchestrator_env_optional = {
    for key, value in {
      K8S_API_SERVER = var.k8s_api_server
      K8S_CA_CERT    = var.k8s_ca_cert
      K8S_SA_TOKEN   = var.k8s_service_account_token
    } : key => value if value != ""
  }

  orchestrator_env = merge(
    local.common_env,
    local.data_env,
    local.orchestrator_env_optional,
    {
      # Names of the two validation steps. The orchestration layer calls them
      # by uniform resource name, which is what lets one definition move
      # between environments untouched.
      FILE_CHECK_URN         = huaweicloud_fgs_function.file_check.urn
      CALENDAR_CHECK_URN     = huaweicloud_fgs_function.calendar_check.urn
      K8S_NAMESPACE          = var.job_namespace
      BATCH_IMAGE            = local.batch_image
      BATCH_CONFIGMAP        = "batch-config"
      JOB_CONTAINER_NAME     = "batch-job"
      JOB_SERVICE_ACCOUNT    = "batch-job-runner"
      JOB_CPU_REQUEST        = var.job_cpu_request
      JOB_MEMORY_REQUEST     = var.job_memory_request
      JOB_CPU_LIMIT          = var.job_cpu_limit
      JOB_MEMORY_LIMIT       = var.job_memory_limit
      JOB_DEADLINE_SECONDS   = tostring(var.job_deadline_seconds)
      JOB_TTL_SECONDS        = tostring(local.job_ttl_seconds)
      STAGE_BUDGET_SECONDS   = tostring(var.stage_budget_seconds)
      POLL_INTERVAL_SECONDS  = tostring(local.job_poll_interval_seconds)
      RETRY_MAX_ATTEMPTS     = tostring(local.job_retry_max_attempts)
      RETRY_BASE_SECONDS     = tostring(local.job_retry_base_seconds)
      NOTIFICATION_TOPIC_URN = huaweicloud_smn_topic.batch.topic_urn
      ENABLE_CUSTOM_METRICS  = tostring(var.enable_custom_metrics)
      METRIC_NAMESPACE       = local.metric_namespace
      REGRESSION_CONCURRENCY = tostring(var.regression_max_concurrency)
      WORKFLOW_DEFINITION    = "${local.full_name}-orchestrator"
    },
  )
}

resource "huaweicloud_fgs_function" "orchestrator" {
  name             = "${local.full_name}-orchestrator"
  app              = var.name_prefix
  description      = "Runs the batch workflow: business day gate, input validation, job launch, exit code routing"
  runtime          = "Python3.9"
  handler          = "index.handler"
  memory_size      = 512
  timeout          = var.stage_budget_seconds + 300
  code_type        = "obs"
  code_url         = local.orchestrator_code_url
  agency           = huaweicloud_identity_agency.functions.name
  vpc_id           = huaweicloud_vpc.main.id
  network_id       = huaweicloud_vpc_subnet.a.id
  max_instance_num = "3"
  enable_lts_log   = true
  log_group_id     = huaweicloud_lts_group.batch.id
  log_stream_id    = huaweicloud_lts_stream.orchestrator.id
  log_group_name   = huaweicloud_lts_group.batch.group_name
  log_stream_name  = huaweicloud_lts_stream.orchestrator.stream_name
  user_data        = jsonencode(local.orchestrator_env)
  tags             = local.common_tags

  lifecycle {
    ignore_changes       = [code_url]
    replace_triggered_by = [huaweicloud_obs_bucket_object.orchestrator_package]
  }
}

# ---------------------------------------------------------------------------
# Schedules. A cron pass opens the daily run and a short rate pass advances
# whichever run is in flight, which is what keeps each invocation inside the
# time budget of one function pass.
#
# An active timer cannot be updated in place, therefore both triggers ignore
# their timer block and status; recreate the trigger when the schedule changes.
# ---------------------------------------------------------------------------

resource "huaweicloud_fgs_trigger" "daily_launch" {
  function_urn = huaweicloud_fgs_function.orchestrator.urn
  type         = "TIMER"
  status       = "ACTIVE"

  timer {
    name          = "${local.full_name}-daily-launch"
    schedule_type = "Cron"
    schedule      = var.daily_cron
    additional_information = jsonencode({
      action     = "start_run"
      user_event = "Daily batch launch"
    })
  }

  lifecycle {
    ignore_changes = [timer, status]
  }
}

resource "huaweicloud_fgs_trigger" "resume_pass" {
  function_urn = huaweicloud_fgs_function.orchestrator.urn
  type         = "TIMER"
  status       = "ACTIVE"

  timer {
    name          = "${local.full_name}-resume-pass"
    schedule_type = "Rate"
    schedule      = local.resume_interval_minutes
    additional_information = jsonencode({
      action     = "resume_runs"
      user_event = "Advance in flight runs"
    })
  }

  lifecycle {
    ignore_changes = [timer, status]
  }
}
