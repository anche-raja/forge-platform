"""CloudWatch metric emission.

The observability Terraform module defines 4 alarms + a dashboard in namespace
`FORGE/Migration`, but nothing published those metrics. This emitter closes that
gap. It is a no-op when disabled (dry-run / no namespace) and never raises — a
metrics hiccup must not fail a migration.

Metric names MUST match the Terraform alarms/dashboard exactly (case-sensitive):
files_processed, files_passed, files_retried, files_manual, files_blocked,
estimated_cost_usd, bedrock_calls, review_score.
"""

import logging

log = logging.getLogger(__name__)

# Cost is the only non-count metric; CloudWatch has no USD unit, so emit "None".
_NONE_UNIT = {"estimated_cost_usd", "review_score"}


class MetricsEmitter:
    def __init__(self, config, *, enabled: bool = True):
        self.namespace = (config.get("cloudwatch_namespace", "") if config else "") or ""
        self.region = config.get("aws_region", "us-east-1") if config else "us-east-1"
        self.enabled = bool(enabled and self.namespace)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("cloudwatch", region_name=self.region)
        return self._client

    def put_batch(self, pairs: dict) -> bool:
        """Emit a batch of {metric_name: value}. ≤20 per PutMetricData call."""
        if not self.enabled or not pairs:
            return False
        data = []
        for name, value in pairs.items():
            if value is None:
                continue
            unit = "None" if name in _NONE_UNIT else "Count"
            data.append({"MetricName": name, "Value": float(value), "Unit": unit})
        if not data:
            return False
        try:
            client = self._get_client()
            # PutMetricData accepts at most 20 (1000 in newer limits, but keep 20).
            for i in range(0, len(data), 20):
                client.put_metric_data(Namespace=self.namespace, MetricData=data[i : i + 20])
            return True
        except Exception as e:  # never break a run on a metrics failure
            log.warning("CloudWatch put_metric_data failed: %s", e)
            return False


def metrics_for_file(file_status: dict, bedrock_calls: int, avg_cost: float) -> dict:
    """Build the per-file metric batch from a finished FileStatus."""
    status = file_status.get("status")
    score = file_status.get("review_score")
    out = {
        "files_processed": 1,
        "files_passed": 1 if status == "DONE" else 0,
        "files_retried": 1 if (file_status.get("retry_count") or 0) > 0 else 0,
        "files_manual": 1 if status == "MANUAL_REVIEW" else 0,
        "files_blocked": 1 if status == "BLOCKED" else 0,
        "bedrock_calls": bedrock_calls,
        "estimated_cost_usd": round((bedrock_calls or 0) * float(avg_cost or 0.0), 4),
    }
    if score is not None:
        out["review_score"] = score
    return out
