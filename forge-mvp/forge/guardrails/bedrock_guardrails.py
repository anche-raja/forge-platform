import boto3
from typing import Literal

from forge.config import ForgeConfig


class BedrockGuardrails:
    def __init__(self, config: ForgeConfig):
        self.client = boto3.client("bedrock-runtime", region_name=config.aws_region)
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
        findings = []
        for assessment in response.get("assessments", []):
            for category, data in assessment.items():
                if data:
                    findings.append(f"{category}: {data}")
        return {
            "action": action,
            "findings": findings,
            "intervened": action == "GUARDRAIL_INTERVENED",
        }
