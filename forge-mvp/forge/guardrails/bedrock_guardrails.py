import boto3
from typing import Literal

from forge.config import ForgeConfig, bedrock_client_config


def intervention_reason(result: dict, source: Literal["INPUT", "OUTPUT"]) -> str:
    """The `error` for a unit the guardrail stopped: which policies, never what matched.

    A finding is ``"<policy>: <assessment>"`` and the assessment can quote the
    matched bytes (a PII match carries them), so only the policy names reach
    the reason. An intervention whose assessments name no policy this module
    knows still gets a reason -- an empty one is what left a unit in manual
    review at review 100 with nothing to say why (issue #26).
    """
    policies = sorted({str(f).split(":", 1)[0].strip() for f in result.get("findings") or []} - {""})
    what = "the source file" if source == "INPUT" else "the transform output"
    detail = ", ".join(policies) if policies else "no policy assessment was returned"
    return f"Bedrock guardrail intervened on {what} ({detail})"


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
