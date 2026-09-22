#!/usr/bin/env bash
# bootstrap-state.sh — creates the S3 bucket and DynamoDB table that Terraform
# uses for remote state and locking. Run this ONCE before `terraform init`.
#
# Usage:
#   bash scripts/bootstrap-state.sh <aws_account_id> [region]
#
# Example:
#   bash scripts/bootstrap-state.sh 123456789012
#   bash scripts/bootstrap-state.sh 123456789012 eu-west-1

set -euo pipefail

ACCOUNT_ID="${1:-}"
REGION="${2:-us-east-1}"

if [[ -z "$ACCOUNT_ID" ]]; then
  echo "ERROR: AWS account ID is required as the first argument."
  echo "Usage: bash scripts/bootstrap-state.sh <aws_account_id> [region]"
  exit 1
fi

BUCKET_NAME="forge-terraform-state-${ACCOUNT_ID}"
LOCK_TABLE="forge-terraform-lock"

echo "==> Bootstrapping Terraform state backend"
echo "    Bucket : ${BUCKET_NAME}"
echo "    Table  : ${LOCK_TABLE}"
echo "    Region : ${REGION}"
echo ""

# ─── Step 1: Create S3 bucket ─────────────────────────────────────────────────
echo "==> Creating S3 bucket..."

if [[ "$REGION" == "us-east-1" ]]; then
  # us-east-1 does NOT accept LocationConstraint — passing it causes
  # IllegalLocationConstraintException
  aws s3api create-bucket \
    --bucket "$BUCKET_NAME" \
    --region "$REGION" \
    2>/dev/null || echo "    (bucket may already exist, continuing)"
else
  aws s3api create-bucket \
    --bucket "$BUCKET_NAME" \
    --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" \
    2>/dev/null || echo "    (bucket may already exist, continuing)"
fi

# ─── Step 2: Enable versioning ────────────────────────────────────────────────
echo "==> Enabling versioning..."
aws s3api put-bucket-versioning \
  --bucket "$BUCKET_NAME" \
  --versioning-configuration Status=Enabled

# ─── Step 3: Enable AES256 encryption ────────────────────────────────────────
echo "==> Enabling server-side encryption (AES256)..."
aws s3api put-bucket-encryption \
  --bucket "$BUCKET_NAME" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'

# ─── Step 4: Block public access ─────────────────────────────────────────────
echo "==> Blocking all public access on state bucket..."
aws s3api put-public-access-block \
  --bucket "$BUCKET_NAME" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# ─── Step 5: Create DynamoDB lock table ──────────────────────────────────────
echo "==> Creating DynamoDB lock table..."
aws dynamodb create-table \
  --table-name "$LOCK_TABLE" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" \
  2>/dev/null || echo "    (table may already exist, continuing)"

# ─── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "State backend ready. Now run:"
echo ""
echo "  terraform init \\"
echo "    -backend-config=\"bucket=${BUCKET_NAME}\" \\"
echo "    -backend-config=\"key=forge/dev/terraform.tfstate\" \\"
echo "    -backend-config=\"region=${REGION}\""
echo ""
echo "For other environments, change the key path (e.g. forge/staging/terraform.tfstate)."
