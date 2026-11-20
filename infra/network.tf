# ---------------------------------------------------------------------------
# Private network. The whole stack runs in a VPC without any route to the
# public internet: regional services are reached over the internal service
# network, so no NAT gateway and no elastic IP address is provisioned.
#
# Private access to the regional services is a property of that service
# network rather than something switched on per service, therefore the
# security group below is the single enforcement point. Its rules carry the
# service network block in place of the per-service address ranges.
# ---------------------------------------------------------------------------

resource "huaweicloud_vpc" "main" {
  name = "${local.full_name}-vpc"
  cidr = var.vpc_cidr
  tags = local.common_tags
}

resource "huaweicloud_vpc_subnet" "a" {
  name              = "${local.full_name}-subnet-a"
  cidr              = var.subnet_a_cidr
  gateway_ip        = cidrhost(var.subnet_a_cidr, 1)
  vpc_id            = huaweicloud_vpc.main.id
  availability_zone = var.subnet_a_az
  tags              = local.common_tags
}

resource "huaweicloud_vpc_subnet" "b" {
  name              = "${local.full_name}-subnet-b"
  cidr              = var.subnet_b_cidr
  gateway_ip        = cidrhost(var.subnet_b_cidr, 1)
  vpc_id            = huaweicloud_vpc.main.id
  availability_zone = var.subnet_b_az
  tags              = local.common_tags
}

# ---------------------------------------------------------------------------
# Workload security group. Batch containers get no inbound port at all: they
# only dial out to the regional service network and to the cluster API server.
# ---------------------------------------------------------------------------

resource "huaweicloud_networking_secgroup" "jobs" {
  name                 = "${local.full_name}-jobs-sg"
  description          = "Egress control for batch workload containers"
  delete_default_rules = true
  tags                 = local.common_tags
}

resource "huaweicloud_networking_secgroup_rule" "jobs_https_to_vpc" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = var.vpc_cidr
  description       = "Cluster API server and in-cluster endpoints"
}

resource "huaweicloud_networking_secgroup_rule" "jobs_https_to_service_network" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = local.internal_service_network_cidr
  description       = "Regional service network used by object storage, notification and monitoring endpoints"
}

resource "huaweicloud_networking_secgroup_rule" "jobs_dns_udp" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = 53
  port_range_max    = 53
  remote_ip_prefix  = var.vpc_cidr
  description       = "Name resolution through the subnet resolver"
}

resource "huaweicloud_networking_secgroup_rule" "jobs_dns_tcp" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 53
  port_range_max    = 53
  remote_ip_prefix  = var.vpc_cidr
  description       = "Name resolution through the subnet resolver"
}

# Workloads that bypass the in-cluster resolver still have to resolve the
# regional service hostnames, and the resolver of those names lives inside the
# service network block. Without these two rules the endpoints stay unreachable
# even though the port 443 rule above permits the traffic itself.
resource "huaweicloud_networking_secgroup_rule" "jobs_dns_to_service_network" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "udp"
  port_range_min    = 53
  port_range_max    = 53
  remote_ip_prefix  = local.internal_service_network_cidr
  description       = "Name resolution of regional service hostnames"
}

resource "huaweicloud_networking_secgroup_rule" "jobs_dns_tcp_to_service_network" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 53
  port_range_max    = 53
  remote_ip_prefix  = local.internal_service_network_cidr
  description       = "Name resolution of regional service hostnames"
}

# Self referencing rule: pods of the same run may address each other, which is
# all the parallel branches need in order to share a scratch dataset.
resource "huaweicloud_networking_secgroup_rule" "jobs_intra_group" {
  security_group_id = huaweicloud_networking_secgroup.jobs.id
  direction         = "ingress"
  ethertype         = "IPv4"
  remote_group_id   = huaweicloud_networking_secgroup.jobs.id
  description       = "Traffic between workloads of the same group"
}
