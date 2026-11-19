# ---------------------------------------------------------------------------
# Data lake: a single versioned, encrypted, private bucket holds the inbound
# files, the intermediate datasets of each run, the outbound results, the run
# checkpoints and the function code packages.
# ---------------------------------------------------------------------------

resource "huaweicloud_obs_bucket" "data" {
  bucket        = local.data_bucket
  storage_class = "STANDARD"
  acl           = "private"

  versioning    = true
  force_destroy = true

  encryption    = true
  sse_algorithm = "AES256"

  # Intermediate datasets are read once by the following stage and then sit
  # idle until the next reconciliation window.
  lifecycle_rule {
    name    = "cool-down-intermediate-datasets"
    prefix  = local.intermediate_prefix
    enabled = true

    transition {
      days          = var.lifecycle_transition_days
      storage_class = "WARM"
    }
  }

  # Superseded function packages keep their previous versions for a month and
  # are then removed automatically.
  lifecycle_rule {
    name    = "retire-old-function-packages"
    prefix  = local.code_prefix
    enabled = true

    noncurrent_version_expiration {
      days = var.lifecycle_transition_days
    }
  }

  tags = local.common_tags
}

# Public access is denied through the bucket policy below. The object storage
# service on this cloud does not expose a separate public-access-block endpoint
# (that call answers 405 Method Not Allowed), so the same guarantee is carried
# by an explicit Deny statement instead of a dedicated resource.

# Data plane authorisation. Object storage does not honour the temporary
# credentials of an agency on its own: the statement below, which names the
# agency principal, is what actually grants read and write access.
resource "huaweicloud_obs_bucket_policy" "data" {
  bucket = huaweicloud_obs_bucket.data.bucket

  # Object storage speaks its own policy dialect, not the identity policy one:
  # bare action names, the bucket name as the resource, a principal object with
  # an ID list, the 2008-10-17 version and string conditions. Anything else is
  # refused as MalformedPolicy while creation of the resource itself succeeds.
  policy_format = "obs"

  policy = jsonencode({
    Version = "2008-10-17"
    Statement = [
      {
        Sid       = "FunctionsReadWrite"
        Effect    = "Allow"
        Principal = { ID = ["domain/${local.account_id}:agency/${huaweicloud_identity_agency.functions.name}"] }
        Action = [
          "GetObject",
          "PutObject",
          "DeleteObject",
          "ListBucket",
        ]
        Resource = [
          huaweicloud_obs_bucket.data.bucket,
          "${huaweicloud_obs_bucket.data.bucket}/*",
        ]
      },
      {
        Sid       = "ContainersReadWrite"
        Effect    = "Allow"
        Principal = { ID = ["domain/${local.account_id}:agency/${huaweicloud_identity_agency.cluster.name}"] }
        Action = [
          "GetObject",
          "PutObject",
          "DeleteObject",
          "ListBucket",
        ]
        Resource = [
          huaweicloud_obs_bucket.data.bucket,
          "${huaweicloud_obs_bucket.data.bucket}/*",
        ]
      },
      {
        Sid       = "EnforceTLS"
        Effect    = "Deny"
        Principal = { ID = ["*"] }
        Action    = ["*"]
        Resource = [
          huaweicloud_obs_bucket.data.bucket,
          "${huaweicloud_obs_bucket.data.bucket}/*",
        ]
        Condition = {
          Bool = {
            SecureTransport = ["false"]
          }
        }
      },
      {
        # Anonymous access is refused outright. This is the guarantee the
        # public-access-block endpoint would have carried; naming "*" as the
        # principal while denying keeps every named agency above working, since
        # an explicit Allow on a concrete principal is what those statements
        # grant and this statement only ever matches unauthenticated callers.
        Sid       = "DenyAnonymousAccess"
        Effect    = "Deny"
        Principal = { ID = ["*"] }
        Action    = ["*"]
        Resource = [
          huaweicloud_obs_bucket.data.bucket,
          "${huaweicloud_obs_bucket.data.bucket}/*",
        ]
        Condition = {
          StringEquals = {
            "g:PrincipalAccount" = "Anonymous"
          }
        }
      },
    ]
  })
}
