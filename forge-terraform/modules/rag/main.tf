terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
    awscc = {
      source = "hashicorp/awscc"
    }
  }
}

# ─── S3 Knowledge Base Bucket ─────────────────────────────────────────────────

resource "aws_s3_bucket" "knowledge_base" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_versioning" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id

  rule {
    id     = "archive-old-documents"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "INTELLIGENT_TIERING"
    }
  }
}

# ─── Bedrock Knowledge Base IAM Role ─────────────────────────────────────────

resource "aws_iam_role" "bedrock_kb" {
  name = "forge-bedrock-kb-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "bedrock.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_kb" {
  name = "forge-bedrock-kb-policy"
  role = aws_iam_role.bedrock_kb.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3BucketRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.knowledge_base.arn,
          "${aws_s3_bucket.knowledge_base.arn}/*"
        ]
      },
      {
        Sid      = "EmbeddingModel"
        Effect   = "Allow"
        Action   = "bedrock:InvokeModel"
        Resource = local.embedding_model_arn
      },
      {
        Sid      = "OpenSearchAccess"
        Effect   = "Allow"
        Action   = "aoss:APIAccessAll"
        Resource = "*"
      }
    ]
  })
}

# ─── OpenSearch Serverless ────────────────────────────────────────────────────

resource "aws_opensearchserverless_security_policy" "encryption" {
  name = "${local.collection_name}-enc"
  type = "encryption"

  policy = jsonencode({
    Rules = [
      {
        Resource     = ["collection/${local.collection_name}"]
        ResourceType = "collection"
      }
    ]
    AWSOwnedKey = true
  })
}

# Network policy JSON must be an ARRAY (not an object).
resource "aws_opensearchserverless_security_policy" "network" {
  name = "${local.collection_name}-net"
  type = "network"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "collection"
        },
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "dashboard"
        }
      ]
      AllowFromPublic = true
    }
  ])
}

# awscc provider is required for the collection resource — the standard aws provider
# does not support awscc_opensearchserverless_collection.
# Tags must use list-of-{key,value} format because awscc does not use default_tags.
resource "awscc_opensearchserverless_collection" "forge_kb" {
  name = local.collection_name
  type = "VECTORSEARCH"

  tags = [
    { key = "Project", value = "FORGE" },
    { key = "Environment", value = var.environment },
    { key = "ManagedBy", value = "Terraform" }
  ]

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network
  ]
}

resource "aws_opensearchserverless_access_policy" "forge_kb" {
  name = "${local.collection_name}-access"
  type = "data"

  policy = jsonencode([
    {
      Rules = [
        {
          Resource     = ["collection/${local.collection_name}"]
          ResourceType = "collection"
          Permission   = ["aoss:CreateCollectionItems", "aoss:DeleteCollectionItems", "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems"]
        },
        {
          Resource     = ["index/${local.collection_name}/*"]
          ResourceType = "index"
          Permission   = ["aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"]
        }
      ]
      Principal = [aws_iam_role.bedrock_kb.arn]
    }
  ])
}

# ─── Bedrock Knowledge Base ───────────────────────────────────────────────────
#
# IMPORTANT: The OpenSearch Serverless collection takes 5-10 minutes to become
# ACTIVE after creation. If this resource fails with "collection not active",
# wait for the collection to reach ACTIVE state in the AWS console and re-run:
#   terraform apply -target=module.rag
#
resource "aws_bedrockagent_knowledge_base" "forge" {
  name     = "forge-knowledge-base-${var.environment}"
  role_arn = aws_iam_role.bedrock_kb.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.embedding_model_arn
    }
  }

  storage_configuration {
    type = "OPENSEARCH_SERVERLESS"

    opensearch_serverless_configuration {
      collection_arn    = awscc_opensearchserverless_collection.forge_kb.arn
      vector_index_name = "forge-kb-index"

      field_mapping {
        vector_field   = "embedding"
        text_field     = "text"
        metadata_field = "metadata"
      }
    }
  }

  depends_on = [
    awscc_opensearchserverless_collection.forge_kb,
    aws_opensearchserverless_access_policy.forge_kb
  ]
}

resource "aws_bedrockagent_data_source" "s3_docs" {
  name             = "forge-s3-docs-${var.environment}"
  knowledge_base_id = aws_bedrockagent_knowledge_base.forge.id

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn = aws_s3_bucket.knowledge_base.arn
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "FIXED_SIZE"

      fixed_size_chunking_configuration {
        max_tokens         = 512
        overlap_percentage = 10 # ~50 tokens overlap on a 512-token chunk
      }
    }
  }
}

# ─── S3 Bucket Policy ─────────────────────────────────────────────────────────

data "aws_iam_policy_document" "kb_bucket_policy" {
  statement {
    sid    = "BedrockGetObject"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.knowledge_base.arn, "${aws_s3_bucket.knowledge_base.arn}/*"]
  }

  statement {
    sid    = "ForgeRoleAccess"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = [var.execution_role_arn]
    }

    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.knowledge_base.arn, "${aws_s3_bucket.knowledge_base.arn}/*"]
  }
}

resource "aws_s3_bucket_policy" "knowledge_base" {
  bucket = aws_s3_bucket.knowledge_base.id
  policy = data.aws_iam_policy_document.kb_bucket_policy.json

  # Must be applied after the public access block to avoid API conflicts
  depends_on = [aws_s3_bucket_public_access_block.knowledge_base]
}

# ─── Seed Documents ───────────────────────────────────────────────────────────
#
# Uses for_each for stable resource addresses — safe to reorder the map without
# triggering resource replacement, unlike count-indexed resources.
# path.module resolves to modules/rag/; docs/ is at ../../docs/ relative to this module.

resource "aws_s3_object" "seed_docs" {
  for_each = {
    coding_standards = "docs/coding_standards.md"
    spring_migration = "docs/spring_migration_patterns.md"
    struts2_rules    = "docs/struts2_to_mvc_rules.md"
    arch_decisions   = "docs/arch_decisions.md"
    liberty_config   = "docs/liberty_config_standards.md"
  }

  bucket       = aws_s3_bucket.knowledge_base.id
  key          = each.value
  source       = "${path.module}/../../docs/${basename(each.value)}"
  content_type = "text/markdown"
  etag         = filemd5("${path.module}/../../docs/${basename(each.value)}")
}
