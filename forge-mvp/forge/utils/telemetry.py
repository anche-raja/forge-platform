"""Logging and CloudWatch metric emission.

The Terraform observability module provisions four alarms and a dashboard over
the FORGE/Migration namespace. Nothing emitted those metrics, so the alarms
could never fire. This module closes that loop.

Metric names and the absence of dimensions are a contract with
forge-terraform/modules/observability/main.tf — the alarms declare no
dimensions, so metrics published WITH dimensions would not match them.
"""

import logging
import os
from typing import Optional

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s :: %(message)s"

# Must match the metric names referenced by the alarms and dashboard widgets.
PIPELINE_METRICS = (
    "files_processed",
    "files_passed",
    "files_retried",
    "files_manual",
    "files_blocked",
    "review_score",
    "bedrock_calls",
    "estimated_cost_usd",
)

_COUNT_METRICS = {"estimated_cost_usd": "None", "review_score": "None"}


def configure_logging(level: Optional[str] = None) -> None:
    """Set up root logging once, honouring FORGE_LOG_LEVEL."""
    resolved = (level or os.environ.get("FORGE_LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(resolved)
        return
    logging.basicConfig(level=resolved, format=_LOG_FORMAT)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class MetricsEmitter:
    """Publishes pipeline counters to CloudWatch.

    Fail-soft by design: a migration run must not die because telemetry is
    unavailable (no credentials, no network, running against mocks). Every
    failure is logged once and swallowed.
    """

    def __init__(self, config, enabled: bool = True):
        self.namespace = config.get("cloudwatch_namespace", "FORGE/Migration")
        self.enabled = enabled and bool(config.get("emit_cloudwatch_metrics", True))
        self._log = get_logger(__name__)
        self._client = None
        self._degraded = False
        if self.enabled:
            try:
                import boto3

                self._client = boto3.client(
                    "cloudwatch", region_name=config.get("aws_region", "us-east-1")
                )
            except Exception as e:  # noqa: BLE001 — telemetry must never be fatal
                self._log.warning("CloudWatch metrics disabled (client init failed): %s", e)
                self.enabled = False

    def emit(self, values: dict) -> bool:
        """Publish the given metric_name -> value pairs. Returns True on success."""
        if not self.enabled or self._client is None:
            return False

        data = [
            {
                "MetricName": name,
                "Value": float(value),
                "Unit": _COUNT_METRICS.get(name, "Count"),
            }
            for name, value in values.items()
            if name in PIPELINE_METRICS and value is not None
        ]
        if not data:
            return False

        try:
            self._client.put_metric_data(Namespace=self.namespace, MetricData=data)
            self._log.info("Published %d metrics to %s", len(data), self.namespace)
            return True
        except Exception as e:  # noqa: BLE001
            if not self._degraded:
                self._log.warning("CloudWatch put_metric_data failed, continuing: %s", e)
                self._degraded = True
            return False
