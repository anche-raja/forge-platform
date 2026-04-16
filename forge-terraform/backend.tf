# Terraform state is stored in S3 with DynamoDB locking.
#
# Variables are NOT allowed inside a backend block. The bucket name must be
# supplied via -backend-config flags at init time:
#
#   terraform init \
#     -backend-config="bucket=forge-terraform-state-<your_account_id>" \
#     -backend-config="key=forge/dev/terraform.tfstate" \
#     -backend-config="region=us-east-1"
#
# Run scripts/bootstrap-state.sh <account_id> BEFORE terraform init to
# create the S3 bucket and DynamoDB table if they don't exist yet.

terraform {
  backend "s3" {
    # Placeholder — override at init time via -backend-config
    bucket         = "forge-terraform-state-ACCOUNT_ID"
    key            = "forge/dev/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    sse_algorithm  = "AES256"
    dynamodb_table = "forge-terraform-lock"
  }
}
