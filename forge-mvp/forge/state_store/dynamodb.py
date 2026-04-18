import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterator, List, Optional, Sequence, Tuple, Any

import boto3
from boto3.dynamodb.conditions import Key

from forge.config import ForgeConfig
from forge.state import FileStatus


# ─── Application-level state manager ─────────────────────────────────────────

class DynamoDBStateManager:
    """Tracks file-level migration status in the forge-migration-state table."""

    def __init__(self, config: ForgeConfig):
        self.dynamodb = boto3.resource("dynamodb", region_name=config.aws_region)
        self.table = self.dynamodb.Table(config.dynamodb_table)

    def put_file_status(self, file_status: FileStatus) -> None:
        item = {k: v for k, v in file_status.items() if v is not None}
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        # DynamoDB cannot store float — convert
        if "risk_score" in item:
            item["risk_score"] = Decimal(str(item["risk_score"]))
        if "review_score" in item and item["review_score"] is not None:
            item["review_score"] = Decimal(str(item["review_score"]))
        if item.get("transform_output"):
            item["transform_output"] = json.dumps(item["transform_output"])
        self.table.put_item(Item=item)

    def get_file_status(self, file_path: str) -> Optional[FileStatus]:
        response = self.table.get_item(Key={"file_path": file_path})
        item = response.get("Item")
        if not item:
            return None
        return self._deserialize(item)

    def get_files_by_status(self, status: str) -> List[FileStatus]:
        response = self.table.query(
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq(status),
        )
        return [self._deserialize(item) for item in response.get("Items", [])]

    def get_progress_summary(self) -> dict:
        response = self.table.scan(
            ProjectionExpression="#s, retry_count",
            ExpressionAttributeNames={"#s": "status"},
        )
        counts: dict = {}
        for item in response.get("Items", []):
            s = item.get("status", "UNKNOWN")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def mark_pending(self, file_paths: List[str], phase: str) -> None:
        with self.table.batch_writer() as batch:
            for fp in file_paths:
                batch.put_item(Item={
                    "file_path": fp,
                    "status": "PENDING",
                    "phase": phase,
                    "risk_tier": "UNSCORED",
                    "risk_score": Decimal("0"),
                    "retry_count": Decimal("0"),
                    "guardrail_findings": [],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

    @staticmethod
    def _deserialize(item: dict) -> FileStatus:
        if "transform_output" in item and isinstance(item["transform_output"], str):
            try:
                item["transform_output"] = json.loads(item["transform_output"])
            except json.JSONDecodeError:
                item["transform_output"] = None
        for field in ("risk_score", "review_score", "retry_count"):
            if field in item and isinstance(item[field], Decimal):
                item[field] = int(item[field])
        return item  # type: ignore[return-value]


# ─── LangGraph checkpointer ───────────────────────────────────────────────────

try:
    from langgraph.checkpoint.base import (
        BaseCheckpointSaver,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
    )
    from langchain_core.runnables import RunnableConfig

    class DynamoDBSaver(BaseCheckpointSaver):
        """LangGraph checkpointer backed by the forge-langgraph-checkpoints table.

        Table schema: thread_id (PK, S) + checkpoint_id (SK, S).
        """

        def __init__(self, config: ForgeConfig):
            super().__init__()
            dynamodb = boto3.resource("dynamodb", region_name=config.aws_region)
            self.table = dynamodb.Table(config.dynamodb_checkpoint_table)

        def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
            thread_id = config["configurable"]["thread_id"]
            checkpoint_id = config["configurable"].get("checkpoint_id")

            if checkpoint_id:
                response = self.table.get_item(
                    Key={"thread_id": thread_id, "checkpoint_id": checkpoint_id}
                )
                item = response.get("Item")
            else:
                response = self.table.query(
                    KeyConditionExpression=Key("thread_id").eq(thread_id),
                    ScanIndexForward=False,
                    Limit=1,
                )
                items = response.get("Items", [])
                item = items[0] if items else None

            if not item:
                return None

            checkpoint = json.loads(item["checkpoint"])
            metadata = json.loads(item.get("metadata", "{}"))
            parent_id = item.get("parent_checkpoint_id") or None
            parent_config = (
                {**config, "configurable": {**config["configurable"], "checkpoint_id": parent_id}}
                if parent_id else None
            )
            return CheckpointTuple(
                config={**config, "configurable": {**config["configurable"], "checkpoint_id": item["checkpoint_id"]}},
                checkpoint=checkpoint,
                metadata=metadata,
                parent_config=parent_config,
            )

        def list(
            self,
            config: Optional[RunnableConfig],
            *,
            filter: Optional[dict] = None,
            before: Optional[RunnableConfig] = None,
            limit: Optional[int] = None,
        ) -> Iterator[CheckpointTuple]:
            if config is None:
                return
            thread_id = config["configurable"]["thread_id"]
            kwargs: dict = dict(
                KeyConditionExpression=Key("thread_id").eq(thread_id),
                ScanIndexForward=False,
            )
            if limit:
                kwargs["Limit"] = limit
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                checkpoint = json.loads(item["checkpoint"])
                metadata = json.loads(item.get("metadata", "{}"))
                yield CheckpointTuple(
                    config={**config, "configurable": {**config["configurable"], "checkpoint_id": item["checkpoint_id"]}},
                    checkpoint=checkpoint,
                    metadata=metadata,
                    parent_config=None,
                )

        def put(
            self,
            config: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: Any,
        ) -> RunnableConfig:
            thread_id = config["configurable"]["thread_id"]
            checkpoint_id = checkpoint["id"]
            parent_id = config["configurable"].get("checkpoint_id", "")
            self.table.put_item(Item={
                "thread_id": thread_id,
                "checkpoint_id": checkpoint_id,
                "checkpoint": json.dumps(checkpoint, default=str),
                "metadata": json.dumps(metadata, default=str),
                "parent_checkpoint_id": parent_id,
            })
            return {**config, "configurable": {**config["configurable"], "checkpoint_id": checkpoint_id}}

except ImportError:
    # Fallback: LangGraph not installed — DynamoDBSaver unavailable
    class DynamoDBSaver:  # type: ignore[no-redef]
        def __init__(self, config):
            raise RuntimeError("langgraph is not installed; cannot use DynamoDBSaver")
