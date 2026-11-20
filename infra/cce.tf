# ---------------------------------------------------------------------------
# Compute plane. Batch containers run as Kubernetes Jobs inside a private
# cluster whose worker nodes have no public address.
# ---------------------------------------------------------------------------

resource "huaweicloud_cce_cluster" "main" {
  name                   = local.full_name
  description            = "Batch workload cluster for the ${var.environment} environment"
  flavor_id              = var.cluster_spec
  vpc_id                 = huaweicloud_vpc.main.id
  subnet_id              = huaweicloud_vpc_subnet.a.id
  container_network_type = "overlay_l2"
  container_network_cidr = var.container_network_cidr
  service_network_cidr   = var.service_network_cidr
  cluster_version        = var.cluster_version == "" ? null : var.cluster_version
  timezone               = "Asia/Shanghai"
  # The cluster expects the public address of the elastic IP, not its
  # identifier. Passing the identifier is rejected as an invalid address.
  eip        = var.enable_api_server_public_access ? huaweicloud_vpc_eip.api[0].address : null
  delete_all = "true"
  labels     = local.common_tags
}

# Optional public entry point for operators without in-VPC access. Off by
# default so that the shipped topology stays reachable from inside the VPC
# only.
resource "huaweicloud_vpc_eip" "api" {
  count = var.enable_api_server_public_access ? 1 : 0

  name        = "${local.full_name}-api-eip"
  description = "Public entry point of the cluster API server"

  publicip {
    type       = "dedicated"
    ip_version = 4
  }

  bandwidth {
    name        = "${local.full_name}-api-bandwidth"
    share_type  = "PER"
    size        = var.api_server_bandwidth_mbitps
    charge_mode = "traffic"
  }

  tags = local.common_tags
}

resource "huaweicloud_cce_node_pool" "workers" {
  cluster_id         = huaweicloud_cce_cluster.main.id
  name               = "${local.full_name}-workers"
  os                 = var.node_os
  flavor_id          = var.node_flavor
  initial_node_count = var.node_count
  availability_zone  = var.subnet_a_az
  type               = "vm"
  # The key pair field takes the name of an imported key pair, never a public
  # key string. Either this or the password has to be set, see the precondition.
  key_pair = var.node_pool_key_pair == "" ? null : var.node_pool_key_pair
  password = var.node_password == "" ? null : var.node_password

  root_volume {
    size       = var.node_root_volume_size
    volumetype = var.node_root_volume_type
  }

  data_volumes {
    size       = var.node_data_volume_size
    volumetype = var.node_data_volume_type
  }

  tags = local.common_tags

  # Node sign-in is mandatory for this API: the cluster rejects a node pool
  # that carries neither a key pair name nor a password, and the acceptance
  # only surfaces while the pool is being created.
  lifecycle {
    precondition {
      condition     = var.node_pool_key_pair != "" || var.node_password != ""
      error_message = "Set node_pool_key_pair or node_password: worker nodes require one of the two."
    }
  }
}

# ---------------------------------------------------------------------------
# Elastic lane. A serverless container namespace with its own per pod network
# absorbs a run that exceeds the capacity of the node pool. Every pod created
# there carries the workload security group above.
# ---------------------------------------------------------------------------

resource "huaweicloud_vpc_subnet" "pods" {
  count = var.cci_enabled ? 1 : 0

  name              = "${local.full_name}-pod-subnet"
  cidr              = var.cci_pod_network_cidr
  gateway_ip        = cidrhost(var.cci_pod_network_cidr, 1)
  vpc_id            = huaweicloud_vpc.main.id
  availability_zone = var.subnet_b_az
  tags              = local.common_tags
}

resource "huaweicloud_cci_namespace" "burst" {
  count = var.cci_enabled ? 1 : 0

  name                      = "${local.full_name}-burst"
  type                      = var.cci_namespace_type
  rbac_enabled              = true
  container_network_enabled = true
  warmup_pool_size          = var.cci_warmup_pool_size

  # The pool of pre-warmed capacity is consumed and grown by the platform as
  # runs come and go, while the value above only states what to start with.
  # Without this, the next run would silently pull the lane back to its
  # original size.
  lifecycle {
    ignore_changes = [warmup_pool_size]
  }
}

resource "huaweicloud_cci_network" "burst" {
  count = var.cci_enabled ? 1 : 0

  name              = "${local.full_name}-burst-network"
  namespace         = huaweicloud_cci_namespace.burst[0].name
  network_id        = huaweicloud_vpc_subnet.pods[0].id
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  availability_zone = var.subnet_b_az
}
