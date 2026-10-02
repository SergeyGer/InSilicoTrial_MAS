# ---------------------------------------------------------------------------
# AWS storage layer: S3 buckets, IAM role and the Unity Catalog storage
# credential / external location that expose them to Databricks.
#
# Two-phase apply (unavoidable, and documented rather than hidden): the IAM trust
# policy needs the external id that Databricks generates for the storage
# credential, while the credential needs the role ARN.
#
#   1) terraform apply -target=aws_s3_bucket.lakehouse -target=aws_s3_bucket.genomics \
#        -target=aws_iam_role.storage_credential
#   2) terraform apply -target=databricks_storage_credential.lakehouse
#      -> take `storage_credential_external_id` from the output
#   3) terraform apply -var databricks_external_id=<value>
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "lakehouse" {
  bucket        = var.lakehouse_bucket_name
  force_destroy = false

  tags = merge(var.tags, { Name = "insilico-trial-lakehouse", Layer = "silver-gold" })
}

resource "aws_s3_bucket" "genomics" {
  bucket        = var.genomics_bucket_name
  force_destroy = false

  tags = merge(var.tags, { Name = "insilico-trial-genomics", Layer = "bronze" })
}

resource "aws_s3_bucket_versioning" "lakehouse" {
  bucket = aws_s3_bucket.lakehouse.id

  versioning_configuration {
    status = "Enabled" # object-level history complements Delta time travel
  }
}

resource "aws_s3_bucket_versioning" "genomics" {
  bucket = aws_s3_bucket.genomics.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lakehouse" {
  bucket = aws_s3_bucket.lakehouse.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "genomics" {
  bucket = aws_s3_bucket.genomics.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "lakehouse" {
  bucket                  = aws_s3_bucket.lakehouse.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "genomics" {
  bucket                  = aws_s3_bucket.genomics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Genomic reference data is large and cold: tier it out of the hot storage class.
resource "aws_s3_bucket_lifecycle_configuration" "genomics" {
  bucket = aws_s3_bucket.genomics.id

  rule {
    id     = "archive-reference-data"
    status = "Enabled"

    filter {
      prefix = var.genomics_prefix
    }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 120
      storage_class = "GLACIER_IR"
    }
  }
}

# ---------------------------------------------------------------------------
# IAM role assumed by Databricks
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "databricks_assume_role" {
  statement {
    sid     = "DatabricksControlPlane"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.databricks_aws_account_id}:root"]
    }

    dynamic "condition" {
      for_each = var.databricks_external_id == "" ? [] : [1]
      content {
        test     = "StringEquals"
        variable = "sts:ExternalId"
        values   = [var.databricks_external_id]
      }
    }
  }
}

resource "aws_iam_role" "storage_credential" {
  name                 = "insilico-trial-mas-unity-catalog"
  description          = "Role that Unity Catalog assumes to read/write the trial simulation lakehouse"
  assume_role_policy   = data.aws_iam_policy_document.databricks_assume_role.json
  max_session_duration = 3600

  tags = var.tags
}

data "aws_iam_policy_document" "lakehouse_access" {
  statement {
    sid    = "LakehouseObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListMultipartUploadParts",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      "${aws_s3_bucket.lakehouse.arn}/*",
      "${aws_s3_bucket.genomics.arn}/*",
    ]
  }

  statement {
    sid    = "BucketMetadata"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
      "s3:ListBucketMultipartUploads",
    ]
    resources = [
      aws_s3_bucket.lakehouse.arn,
      aws_s3_bucket.genomics.arn,
    ]
  }
}

resource "aws_iam_role_policy" "lakehouse_access" {
  name   = "insilico-trial-lakehouse-access"
  role   = aws_iam_role.storage_credential.id
  policy = data.aws_iam_policy_document.lakehouse_access.json
}

# ---------------------------------------------------------------------------
# Unity Catalog storage credential + external location
# ---------------------------------------------------------------------------

resource "databricks_storage_credential" "lakehouse" {
  name    = "insilico_trial_storage_credential"
  comment = "Access to the InSilicoTrial MAS S3 lakehouse and genomic reference data"

  aws_iam_role {
    role_arn = aws_iam_role.storage_credential.arn
  }

  depends_on = [aws_iam_role_policy.lakehouse_access]
}

resource "databricks_external_location" "lakehouse" {
  name            = "insilico_trial_lakehouse"
  url             = "s3://${var.lakehouse_bucket_name}"
  credential_name = databricks_storage_credential.lakehouse.name
  comment         = "Bronze/Silver/Gold Delta tables for the simulation platform"

  depends_on = [aws_iam_role_policy.lakehouse_access]
}

resource "databricks_external_location" "genomics" {
  name            = "insilico_trial_genomics"
  url             = "s3://${var.genomics_bucket_name}/${var.genomics_prefix}"
  credential_name = databricks_storage_credential.lakehouse.name
  comment         = "Raw genomic reference datasets (licensed sources)"

  depends_on = [aws_iam_role_policy.lakehouse_access]
}

# ---------------------------------------------------------------------------
# Optional: AWS Batch queue for burst genomic pre-processing
# ---------------------------------------------------------------------------

# AWS provider 6.x renamed this resource's `compute_environment_name` argument to
# `name` (and `compute_environment_name_prefix` to `name_prefix`).
resource "aws_batch_compute_environment" "genomics" {
  count = var.enable_aws_batch ? 1 : 0

  name         = "insilico-genomics-${var.environment}"
  type         = "MANAGED"
  state        = "ENABLED"
  service_role = aws_iam_role.batch_service[0].arn
  depends_on   = [aws_iam_role_policy_attachment.batch_service]

  compute_resources {
    type                = "EC2"
    max_vcpus           = 256
    min_vcpus           = 0
    desired_vcpus       = 0
    instance_type       = ["m5.4xlarge", "m5a.4xlarge"]
    security_group_ids  = []
    subnets             = []
    allocation_strategy = "SPOT_CAPACITY_OPTIMIZED"
  }
}

resource "aws_iam_role" "batch_service" {
  count = var.enable_aws_batch ? 1 : 0
  name  = "insilico-genomics-batch-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "batch.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  count      = var.enable_aws_batch ? 1 : 0
  role       = aws_iam_role.batch_service[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}
