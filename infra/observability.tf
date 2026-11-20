# ---------------------------------------------------------------------------
# Notification channel used by both the runtime failure paths and the alarms.
# ---------------------------------------------------------------------------

resource "huaweicloud_smn_topic" "batch" {
  name         = "${local.full_name}-alerts"
  display_name = "Batch operations"
  introduction = "Notifications raised by the batch orchestration run and by the Cloud Eye alarms"

  tags = local.common_tags
}

resource "huaweicloud_smn_subscription" "operator" {
  count = var.notification_email == "" ? 0 : 1

  topic_urn = huaweicloud_smn_topic.batch.topic_urn
  protocol  = "email"
  endpoint  = var.notification_email
  remark    = "Batch operations mailbox"
}

# ---------------------------------------------------------------------------
# Alarms. Both rules watch the run metrics published by the orchestration
# layer: one counts failed runs, one tracks how long a run takes.
# ---------------------------------------------------------------------------

resource "huaweicloud_ces_alarmrule" "failed_runs" {
  count = var.enable_ces_alarms ? 1 : 0

  alarm_name           = "${local.full_name}-failed-runs"
  alarm_description    = "A batch run finished with a failed stage"
  alarm_type           = "ALL_INSTANCE"
  alarm_action_enabled = true
  alarm_enabled        = true

  metric {
    namespace = local.metric_namespace

    dimensions {
      name  = "run_environment"
      value = var.environment
    }
  }

  condition {
    period              = 300
    filter              = "sum"
    comparison_operator = ">="
    value               = 1
    count               = 1
    metric_name         = local.metric_failures
    alarm_level         = 2
    suppress_duration   = var.failure_alarm_suppression_minutes * 60
  }

  alarm_actions {
    type              = "notification"
    notification_list = [huaweicloud_smn_topic.batch.topic_urn]
  }

  ok_actions {
    type              = "notification"
    notification_list = [huaweicloud_smn_topic.batch.topic_urn]
  }
}

resource "huaweicloud_ces_alarmrule" "run_duration" {
  count = var.enable_ces_alarms ? 1 : 0

  alarm_name           = "${local.full_name}-run-duration"
  alarm_description    = "A batch run exceeded the agreed turnaround window"
  alarm_type           = "ALL_INSTANCE"
  alarm_action_enabled = true
  alarm_enabled        = true

  metric {
    namespace = local.metric_namespace

    dimensions {
      name  = "run_environment"
      value = var.environment
    }
  }

  condition {
    period              = 300
    filter              = "max"
    comparison_operator = ">"
    value               = var.duration_alarm_threshold_seconds
    count               = 1
    metric_name         = local.metric_duration
    alarm_level         = 3
    suppress_duration   = 900
  }

  alarm_actions {
    type              = "notification"
    notification_list = [huaweicloud_smn_topic.batch.topic_urn]
  }

  ok_actions {
    type              = "notification"
    notification_list = [huaweicloud_smn_topic.batch.topic_urn]
  }
}

# ---------------------------------------------------------------------------
# Operations view: the same two series the alarms watch.
# ---------------------------------------------------------------------------

resource "huaweicloud_ces_dashboard" "batch" {
  count = var.enable_ces_alarms ? 1 : 0

  name           = local.full_name
  row_widget_num = 2

  extend_info {
    enable_legend = true
    period        = "300"
  }
}

resource "huaweicloud_ces_dashboard_widget" "failed_runs" {
  count = var.enable_ces_alarms ? 1 : 0

  dashboard_id        = huaweicloud_ces_dashboard.batch[0].id
  title               = "Failed runs"
  view                = "bar"
  metric_display_mode = "single"

  metrics {
    namespace   = local.metric_namespace
    metric_name = local.metric_failures

    dimensions {
      name        = "run_environment"
      filter_type = "specific_instances"
      values      = [var.environment]
    }
  }

  location {
    left   = 0
    top    = 0
    width  = 6
    height = 4
  }
}

resource "huaweicloud_ces_dashboard_widget" "run_duration" {
  count = var.enable_ces_alarms ? 1 : 0

  dashboard_id        = huaweicloud_ces_dashboard.batch[0].id
  title               = "Run duration in seconds"
  view                = "line"
  metric_display_mode = "single"
  unit                = "s"

  metrics {
    namespace   = local.metric_namespace
    metric_name = local.metric_duration

    dimensions {
      name        = "run_environment"
      filter_type = "specific_instances"
      values      = [var.environment]
    }
  }

  location {
    left   = 6
    top    = 0
    width  = 6
    height = 4
  }
}
