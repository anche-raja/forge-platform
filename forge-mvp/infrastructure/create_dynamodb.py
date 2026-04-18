#!/usr/bin/env python3
"""Create the FORGE DynamoDB tables locally (for dev/test without Terraform).

Usage:
    python infrastructure/create_dynamodb.py [--region us-east-1] [--env dev]

For production, use Terraform (forge-terraform/modules/foundation/main.tf).
"""

import argparse
import boto3
from botocore.exceptions import ClientError


def create_migration_state_table(dynamodb, app_name: str, env: str) -> None:
    table_name = f"{app_name}-migration-state-{env}"
    try:
        dynamodb.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "file_path", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "file_path", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "phase", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "phase", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "phase-status-index",
                    "KeySchema": [
                        {"AttributeName": "phase", "KeyType": "HASH"},
                        {"AttributeName": "status", "KeyType": "RANGE"},
                    ],
                    "Projection": {
                        "ProjectionType": "INCLUDE",
                        "NonKeyAttributes": ["file_path", "review_score", "retry_count", "updated_at"],
                    },
                },
            ],
        )
        print(f"Created table: {table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table already exists: {table_name}")
        else:
            raise


def create_checkpoints_table(dynamodb, app_name: str, env: str) -> None:
    table_name = f"{app_name}-langgraph-checkpoints-{env}"
    try:
        dynamodb.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "thread_id", "KeyType": "HASH"},
                {"AttributeName": "checkpoint_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "thread_id", "AttributeType": "S"},
                {"AttributeName": "checkpoint_id", "AttributeType": "S"},
            ],
        )
        print(f"Created table: {table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"Table already exists: {table_name}")
        else:
            raise


def main():
    parser = argparse.ArgumentParser(description="Create FORGE DynamoDB tables")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--env", default="dev")
    parser.add_argument("--app-name", default="forge")
    parser.add_argument("--endpoint-url", default=None, help="Override endpoint (e.g. http://localhost:8000 for DynamoDB Local)")
    args = parser.parse_args()

    dynamodb = boto3.client(
        "dynamodb",
        region_name=args.region,
        endpoint_url=args.endpoint_url,
    )

    create_migration_state_table(dynamodb, args.app_name, args.env)
    create_checkpoints_table(dynamodb, args.app_name, args.env)
    print("Done.")


if __name__ == "__main__":
    main()
