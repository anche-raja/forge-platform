"""SQS escalation for files that need human review.

When a file is escalated to MANUAL_REVIEW, send a lightweight pointer message to
the manual-review SQS queue so a human gets paged. The full record (including
transform_output) lives in DynamoDB; the review portal reads that. This message
is just the "a human is needed" signal + a summary.

No-op when `sqs_queue_url` is empty; never raises (a flaky queue must not crash a
migration).
"""

import json
import logging

log = logging.getLogger(__name__)

# Summary fields only — NOT transform_output (portal reads that from DynamoDB).
_SUMMARY_FIELDS = (
    "file_path",
    "status",
    "phase",
    "review_score",
    "review_verdict",
    "review_feedback",
    "retry_count",
    "guardrail_findings",
    "error",
    "transform_model",
    "review_model",
)


class SqsEscalator:
    def __init__(self, config):
        self.queue_url = (config.get("sqs_queue_url", "") if config else "") or ""
        self.region = config.get("aws_region", "us-east-1") if config else "us-east-1"
        self.enabled = bool(self.queue_url)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("sqs", region_name=self.region)
        return self._client

    def send(self, file_status: dict) -> bool:
        """Send a manual-review escalation. Returns False (no-op) when disabled."""
        if not self.enabled or not file_status:
            return False
        body = {k: file_status.get(k) for k in _SUMMARY_FIELDS if file_status.get(k) is not None}
        try:
            self._get_client().send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(body, default=str),
            )
            return True
        except Exception as e:  # never break a run on a queue failure
            log.warning("SQS send_message failed for %s: %s", file_status.get("file_path"), e)
            return False
