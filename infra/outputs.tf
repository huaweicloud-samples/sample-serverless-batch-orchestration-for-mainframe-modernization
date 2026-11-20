output "region" {
  description = "Region this stack is deployed into."
  value       = local.region
}

output "data_bucket" {
  description = "Bucket holding inbound data, intermediate datasets, results, run checkpoints and function packages."
  value       = huaweicloud_obs_bucket.data.bucket
}

output "data_bucket_domain_name" {
  description = "Bucket endpoint host of the data bucket."
  value       = huaweicloud_obs_bucket.data.bucket_domain_name
}

output "object_storage_endpoint" {
  description = "Regional object storage endpoint used by the containers and the functions."
  value       = "https://${local.obs_endpoint}"
}

output "inbound_prefix" {
  description = "Prefix an input dataset has to be uploaded under."
  value       = local.inbound_prefix
}

output "processing_prefix" {
  description = "Prefix holding the intermediate datasets of a run."
  value       = local.processing_prefix
}

output "outbound_prefix" {
  description = "Prefix holding the results of a run."
  value       = local.outbound_prefix
}

output "runs_prefix" {
  description = "Prefix under which one checkpoint file per run is stored."
  value       = local.runs_prefix
}

output "cluster_id" {
  description = "Identifier of the container cluster."
  value       = huaweicloud_cce_cluster.main.id
}

output "cluster_api_server" {
  description = "API server address of the cluster. Copy it into k8s_api_server after the first apply."
  value       = huaweicloud_cce_cluster.main.certificate_clusters[0].server
}

output "job_namespace" {
  description = "Namespace that holds the batch jobs and the orchestrator service account."
  value       = var.job_namespace
}

output "image_registry_host" {
  description = "Registry host used by the build and push script."
  value       = huaweicloud_swr_organization.main.login_server
}

output "batch_image" {
  description = "Fully qualified image referenced by every batch job."
  value       = local.batch_image
}

output "function_agency_name" {
  description = "Agency assumed by the functions. Referenced by the bucket statement."
  value       = huaweicloud_identity_agency.functions.name
}

output "orchestrator_function_urn" {
  description = "Uniform resource name of the orchestration function."
  value       = huaweicloud_fgs_function.orchestrator.urn
}

output "file_check_function_urn" {
  description = "Uniform resource name of the input validation function."
  value       = huaweicloud_fgs_function.file_check.urn
}

output "calendar_check_function_urn" {
  description = "Uniform resource name of the business day function."
  value       = huaweicloud_fgs_function.calendar_check.urn
}

output "daily_schedule" {
  description = "Cron expression of the daily launch pass."
  value       = var.daily_cron
}

output "resume_interval_seconds" {
  description = "Interval in seconds of the checkpoint resumption pass."
  value       = var.resume_interval_seconds
}

output "notification_topic_urn" {
  description = "Topic that receives run failures and alarm notifications."
  value       = huaweicloud_smn_topic.batch.topic_urn
}

output "metric_namespace" {
  description = "Custom metric namespace the orchestration layer reports into."
  value       = local.metric_namespace
}

output "dashboard_id" {
  description = "Identifier of the operations dashboard."
  value       = var.enable_ces_alarms ? huaweicloud_ces_dashboard.batch[0].id : ""
}

output "workload_security_group_id" {
  description = "Security group attached to the elastic batch pods."
  value       = huaweicloud_networking_secgroup.jobs.id
}

output "vpc_id" {
  description = "Identifier of the private network holding the whole stack."
  value       = huaweicloud_vpc.main.id
}

output "subnet_a_id" {
  description = "First private subnet: cluster nodes and functions."
  value       = huaweicloud_vpc_subnet.a.id
}

output "subnet_b_id" {
  description = "Second private subnet, the failure domain of the first one."
  value       = huaweicloud_vpc_subnet.b.id
}

output "burst_namespace" {
  description = "Serverless container namespace absorbing runs beyond node pool capacity. Empty when the lane is switched off."
  value       = var.cci_enabled ? huaweicloud_cci_namespace.burst[0].name : ""
}

output "burst_network_id" {
  description = "Pod network of the elastic lane, referenced when a run is placed on it."
  value       = var.cci_enabled ? huaweicloud_cci_network.burst[0].id : ""
}

output "next_step_inputs" {
  description = "Values that must be filled in terraform.tfvars after the manifests are applied."
  value = join("\n", [
    "k8s_api_server                = output.cluster_api_server",
    "k8s_ca_cert                   = export_k8s_access.sh -> CA",
    "k8s_service_account_token     = export_k8s_access.sh -> token",
  ])
}
