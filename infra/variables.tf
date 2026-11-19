# ---------------------------------------------------------------------------
# Deployment inputs. Every value that influences resource sizing is declared
# here with an explicit default so that a plain `terraform apply` provisions a
# predictable, reviewable topology.
# ---------------------------------------------------------------------------

variable "access_key" {
  description = "Huawei Cloud IAM access key of the operator. Leave empty to read HW_ACCESS_KEY from the environment."
  type        = string
  default     = ""
}

variable "secret_key" {
  description = "Huawei Cloud IAM secret key of the operator. Leave empty to read HW_SECRET_KEY from the environment."
  type        = string
  default     = ""
  sensitive   = true
}

variable "security_token" {
  description = "Optional temporary security token to be used together with access_key and secret_key."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloud" {
  description = "Huawei Cloud endpoint suffix."
  type        = string
  default     = "myhuaweicloud.com"
}

variable "environment" {
  description = "Deployment environment name, used in resource names and in the data bucket prefix."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "environment must be 2 to 16 lowercase letters, digits or hyphens."
  }
}

variable "name_prefix" {
  description = "Common name prefix for all resources created by this stack."
  type        = string
  default     = "batch"
}

variable "notification_email" {
  description = "Operator mailbox subscribed to the notification topic. When empty no subscription is created and the topic is still used for alarm forwarding."
  type        = string
  default     = ""
}

variable "agency_extra_roles" {
  description = "Extra project-level IAM roles appended to the FunctionGraph agency. Add the roles required by the runtime integrations you enable (for example Cloud Eye custom metric writing) after confirming their exact names in the IAM console."
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR of the isolated VPC that hosts the cluster and the batch workloads."
  type        = string
  default     = "10.20.0.0/16"
}

variable "subnet_a_cidr" {
  description = "CIDR of the first private subnet (no internet route is attached to it)."
  type        = string
  default     = "10.20.1.0/24"
}

variable "subnet_b_cidr" {
  description = "CIDR of the second private subnet (no internet route is attached to it)."
  type        = string
  default     = "10.20.2.0/24"
}

variable "subnet_a_az" {
  description = "Availability zone of the first private subnet."
  type        = string
  default     = "cn-north-4a"
}

variable "subnet_b_az" {
  description = "Availability zone of the second private subnet."
  type        = string
  default     = "cn-north-4b"
}

variable "container_network_cidr" {
  description = "Pod network CIDR of the cluster."
  type        = string
  default     = "172.16.0.0/16"
}

variable "service_network_cidr" {
  description = "Service (ClusterIP) CIDR of the cluster."
  type        = string
  default     = "10.254.0.0/16"
}

# ---------------------------------------------------------------------------
# Container platform
# ---------------------------------------------------------------------------

variable "cluster_spec" {
  description = "Cluster specification. cce.s1.small supports up to 50 nodes."
  type        = string
  default     = "cce.s1.small"
}

variable "cluster_version" {
  description = "Kubernetes version of the cluster. Leave empty to accept the default version offered by the region."
  type        = string
  default     = ""
}

variable "node_flavor" {
  description = "Compute flavor of every worker node. c7.large.2 is 2 vCPU / 4 GiB, x86_64."
  type        = string
  default     = "c7.large.2"
}

variable "node_count" {
  description = "Number of worker nodes in the node pool. Fixed size: two nodes cover the peak of two concurrent batch containers."
  type        = number
  default     = 2
}

variable "node_os" {
  description = "Operating system image of the worker nodes."
  type        = string
  default     = "Huawei Cloud EulerOS 2.0"
}

variable "node_root_volume_size" {
  description = "System disk size of each worker node, in GiB."
  type        = number
  default     = 40
}

variable "node_root_volume_type" {
  description = "System disk category of each worker node."
  type        = string
  default     = "SAS"
}

variable "node_data_volume_size" {
  description = "Data disk size of each worker node, in GiB. Holds the container image cache."
  type        = number
  default     = 100
}

variable "node_data_volume_type" {
  description = "Data disk category of each worker node."
  type        = string
  default     = "SAS"
}

variable "node_pool_key_pair" {
  description = "Name of an existing key pair used to log in to worker nodes. Supply either this or node_password."
  type        = string
  default     = ""
}

variable "node_password" {
  description = "Login password for worker nodes. Used only when node_pool_key_pair is empty. Supply either this or node_pool_key_pair; read it from the environment rather than committing it."
  type        = string
  default     = ""
  sensitive   = true
}

variable "enable_api_server_public_access" {
  description = "Attach an elastic IP address to the cluster API server. Keep false when kubectl is run from a host inside the VPC."
  type        = bool
  default     = false
}

variable "api_server_bandwidth_mbitps" {
  description = "Bandwidth size of the API server elastic IP address, in Mbit/s. Only used when public access is enabled."
  type        = number
  default     = 5
}

variable "cci_enabled" {
  description = "Provision the serverless container namespace and its per-pod network, used as the burst capacity lane for batch runs."
  type        = bool
  default     = true
}

variable "cci_namespace_type" {
  description = "Serverless container namespace type: general-computing or gpu-accelerated."
  type        = string
  default     = "general-computing"
}

variable "cci_pod_network_cidr" {
  description = "Subnet used by the per-pod network of the serverless container namespace."
  type        = string
  default     = "10.20.3.0/24"
}

variable "cci_warmup_pool_size" {
  description = "Number of addresses pre-allocated in the per-pod network so that a burst job starts without waiting for a new address. Valid range 1 to 500."
  type        = number
  default     = 10
}

# ---------------------------------------------------------------------------
# Image registry
# ---------------------------------------------------------------------------

variable "swr_organization" {
  description = "Name of the software repository service organization that holds the batch image."
  type        = string
  default     = "batch-modernization"
}

variable "swr_repository" {
  description = "Name of the image repository that holds the batch job container."
  type        = string
  default     = "batch-stub"
}

variable "swr_image_tag" {
  description = "Image tag pushed by the build script and referenced by the batch jobs."
  type        = string
  default     = "1.0.0"
}

# ---------------------------------------------------------------------------
# Data bucket lifecycle
# ---------------------------------------------------------------------------

variable "lifecycle_transition_days" {
  description = "Number of days after which objects under the intermediate processing prefix move to the infrequent access storage class."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "Retention of the log group used by the functions and the cluster control plane."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# Batch behaviour
# ---------------------------------------------------------------------------

variable "schedule_timezone" {
  description = "IANA time zone in which business days are evaluated."
  type        = string
  default     = "Asia/Shanghai"
}

variable "daily_cron" {
  description = "Six field cron expression (second minute hour day month weekday) of the daily batch launch."
  type        = string
  default     = "0 0 2 * * ?"
}

variable "resume_interval_seconds" {
  description = "Interval in seconds of the checkpoint resumption pass. Controls how often an in-flight run advances. The scheduler accepts a plain number of seconds."
  type        = number
  default     = 180
}

variable "max_input_age_days" {
  description = "Maximum age, in UTC calendar days, accepted for an inbound data file."
  type        = number
  default     = 2
}

variable "stage_budget_seconds" {
  description = "Time budget of a single orchestration pass. When the budget is exhausted the run is checkpointed and the next scheduled pass continues it."
  type        = number
  default     = 900
}

variable "job_deadline_seconds" {
  description = "Hard deadline of one batch container execution, in seconds. Enforced by the job active deadline and by the orchestrator poller."
  type        = number
  default     = 7200
}

variable "job_cpu_request" {
  description = "CPU request of a batch container."
  type        = string
  default     = "500m"
}

variable "job_memory_request" {
  description = "Memory request of a batch container."
  type        = string
  default     = "1Gi"
}

variable "job_cpu_limit" {
  description = "CPU limit of a batch container: one full core."
  type        = string
  default     = "1"
}

variable "job_memory_limit" {
  description = "Memory limit of a batch container."
  type        = string
  default     = "2Gi"
}

variable "regression_max_concurrency" {
  description = "Number of regression modules executed at the same time. Mirrors the size of the on-premises test resource pool."
  type        = number
  default     = 3
}

# ---------------------------------------------------------------------------
# Kubernetes access used by the orchestrator
# ---------------------------------------------------------------------------

variable "k8s_api_server" {
  description = "Internal API server address of the cluster, for example https://<private-ip>:5443. Fill it after the cluster is created; leave empty on the first apply."
  type        = string
  default     = ""
}

variable "k8s_ca_cert" {
  description = "PEM encoded API server certificate authority bundle read from the orchestrator service account secret. Leave empty on the first apply."
  type        = string
  default     = ""
  sensitive   = true
}

variable "k8s_service_account_token" {
  description = "Long lived token of the orchestrator service account. Leave empty on the first apply."
  type        = string
  default     = ""
  sensitive   = true
}

variable "job_namespace" {
  description = "Namespace in which batch jobs are created."
  type        = string
  default     = "batch-jobs"
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

variable "enable_ces_alarms" {
  description = "Create the Cloud Eye metric alarms with this stack. Cloud Eye rejects alarm rules bound to a custom metric namespace, so this is off by default; create the same two alarms in the Cloud Eye console using the identical metric, dimensions and threshold."
  type        = bool
  default     = false
}

variable "duration_alarm_threshold_seconds" {
  description = "Elapsed time of one batch run above which the duration alarm fires."
  type        = number
  default     = 1800
}

variable "failure_alarm_suppression_minutes" {
  description = "Repeat suppression window of the failed-run alarm, in minutes. Only the intervals accepted by Cloud Eye are allowed."
  type        = number
  default     = 15

  validation {
    condition     = contains([0, 5, 10, 15, 30, 60, 180, 360, 720, 1440], var.failure_alarm_suppression_minutes)
    error_message = "failure_alarm_suppression_minutes must be one of 0, 5, 10, 15, 30, 60, 180, 360, 720, 1440."
  }
}

variable "enable_custom_metrics" {
  description = "Publish the run counters to Cloud Eye as custom metrics from the orchestration layer. The monitoring role is already attached to the function agency; turning this off only stops the reporting step."
  type        = bool
  default     = true
}
