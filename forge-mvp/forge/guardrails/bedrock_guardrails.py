import boto3
from typing import Literal

from forge.config import ForgeConfig, bedrock_client_config


class BedrockGuardrails:
    def __init__(self, config: ForgeConfig):
        self.client = boto3.client("bedrock-runtime", region_name=config.aws_region,
                                   config=bedrock_client_config(config))
        self.guardrail_id = config.guardrail_id
        self.guardrail_version = str(config.guardrail_version)

    def evaluate(self, text: str, source: Literal["INPUT", "OUTPUT"]) -> dict:
        response = self.client.apply_guardrail(
            guardrailIdentifier=self.guardrail_id,
            guardrailVersion=self.guardrail_version,
            source=source,
            content=[{"text": {"text": text}}],
        )
        action = response.get("action", "NONE")
        findings: list[str] = []
        # Only policy categories are findings — invocationMetrics is telemetry.
        policy_keys = {
            "topicPolicy",
            "contentPolicy",
            "wordPolicy",
            "sensitiveInformationPolicy",
            "contextualGroundingPolicy",
        }
        for assessment in response.get("assessments", []):
            for category, data in assessment.items():
                if category not in policy_keys or not data:
                    continue
                findings.append(f"{category}: {data}")
        return {
            "action": action,
            "findings": findings,
            "intervened": action == "GUARDRAIL_INTERVENED",
        }
