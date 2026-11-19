# ---------------------------------------------------------------------------
# Delegated access. Cloud services assume these agencies and receive short
# lived credentials; no key pair is embedded in any resource.
# ---------------------------------------------------------------------------

# Agency assumed by FunctionGraph. Grants the data plane access used by the
# orchestration functions. Object storage is only reachable through the bucket
# statement created in obs.tf, which references this agency by name.
resource "huaweicloud_identity_agency" "functions" {
  name                   = "${local.full_name}-functions-agency"
  description            = "Assumed by FunctionGraph to run the batch orchestration functions"
  delegated_service_name = "op_svc_cff"

  # Account scoped role: object storage authorises data requests by account.
  domain_roles = ["OBS Administrator"]

  # Regional scoped roles. Cloud monitoring and notification are regional
  # services, so their roles have to sit here and not in the domain list: a
  # role placed in the wrong scope is accepted silently and simply never takes
  # effect. The run counters published by the orchestration layer need the
  # monitoring role, the failure notices need the notification role.
  #
  # The virtual private cloud role is mandatory once a function is attached to a
  # subnet: without it the platform refuses to place the function in the network
  # and every invocation fails immediately with "no operational permissions vpc,
  # check the func xrole permissions" before any function code runs.
  #
  # The function role is what lets the orchestration layer invoke the two
  # validation functions. The role has to be named exactly "fgs_inv": a policy
  # name that does not exist is accepted by the identity service without an
  # error and then simply never takes effect, and an action prefix of "fgs" is
  # rejected at run time, so both the name and the vocabulary matter here. The
  # invocation is otherwise refused from inside the running function with
  # "FSS.0403 user not permitted".
  project_role {
    project = local.region
    roles = concat(
      ["SMN FullAccess", "CES FullAccess", "VPC Administrator", huaweicloud_identity_role.invoke.name],
      var.agency_extra_roles,
    )
  }
}

# The role that actually carries the invocation permission. The bundled roles
# for this service do not cover a function calling another function, so the two
# actions the orchestration layer needs are granted directly. The action prefix
# is the service name in CamelCase - "FunctionGraph:function:invoke", with the
# resource path in the slash form "function/<application>/<name>" rather than
# the uniform resource name the call itself uses.
resource "huaweicloud_identity_role" "invoke" {
  name        = "${local.full_name}-invoke"
  description = "Lets the batch orchestration functions invoke the validation functions"
  type        = "XA"

  policy = jsonencode({
    Version = "1.1"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "FunctionGraph:function:invoke",
          "FunctionGraph:function:invokeAsync",
        ]
        Resource = [
          "FunctionGraph:*:*:function:${var.name_prefix}/${local.full_name}-file-check",
          "FunctionGraph:*:*:function:${var.name_prefix}/${local.full_name}-calendar-check",
        ]
      },
    ]
  })
}

# Agency assumed by the container platform. Nodes use it to pull images from
# the registry and to read the data bucket, so no image pull secret and no
# object storage key is mounted into a pod.
resource "huaweicloud_identity_agency" "cluster" {
  name                  = "${local.full_name}-cluster-agency"
  description           = "Assumed by Cloud Container Engine to pull images and mount object storage"
  delegated_domain_name = "op_svc_cce"

  project_role {
    project = local.region
    roles   = ["OBS OperateAccess", "SWR Admin", "CCE Admin"]
  }
}
