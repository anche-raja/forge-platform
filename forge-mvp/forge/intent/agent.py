"""The one model call: a sentence and a profile in, a proposal out.

What it is given is deliberately narrow. The profile block below is assembled
field by field rather than dumped, because the rule from ``GUARDRAILS.md`` §7 —
a model is never the control that decides what a model may see — means the
intent layer must not be the thing that leaks a file. Coordinates, counts,
import *prefixes* and descriptor *names* are metadata. File contents are not,
and never appear here. ``test_intent_agent.py`` asserts it.

There is no retry. A response that will not parse degrades to "no proposal",
and :func:`forge.intent.resolve.reconcile` then returns the plan discovery
would have produced on its own. A guess is worse than a default.
"""

from typing import Any, Dict, List, Optional

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.config import ForgeConfig
from forge.intent.vocabulary import render_vocabulary
from forge.utils.cost import estimate_cost, usage_from_response
from forge.utils.llm_json import extract_json
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

_MAX_DEPENDENCIES = 200
_MAX_DESCRIPTORS = 40

_SYSTEM = """\
You map a migration request written in plain English onto a fixed vocabulary.

You are given a repository PROFILE — build metadata only, never source code — \
and the closed set of packs and decisions you may choose from.

Rules you must follow:

1. You may only NARROW. Never ask for a pack that is not in the list you were \
given: the list is evidence-based, and a pack absent from it has no evidence in \
this repository. If the request names a technology that is not there, put it in \
"unsupported" with the reason. Do not put it in "include".
2. Every decision value must be copied exactly from the options given. If the \
request does not determine a decision, OMIT the key — do not guess. The platform \
default will be used and reported as an assumption.
3. "include" is the packs you want. If the request is broad ("modernize this", \
"upgrade to the latest"), include everything you were given — that is what the \
evidence says the project needs — and set the decisions you can infer.
4. "exclude" is for packs the request explicitly rules out ("leave the tests \
alone", "don't touch the UI"), each with the words that ruled it out.
5. "scope.exclude_globs" is for directories or files the request says to ignore \
("ignore the db folder"). Use glob syntax relative to the repository root, e.g. \
"db/**". Excluding is always safe; when in doubt about a phrase like this, honour it.
6. "questions" is for a choice that materially changes the outcome and that the \
request genuinely does not settle. Keep it short; do not ask about anything you \
were able to decide.

Respond ONLY with valid JSON — no markdown, no explanation:
{
  "decisions": {"<key>": "<value from the options>"},
  "include": ["<pack id>"],
  "exclude": [{"pack": "<pack id>", "reason": "<the words that ruled it out>"}],
  "scope": {"exclude_globs": ["<glob>"], "package_prefix": "<java package or empty>"},
  "unsupported": [{"asked": "<what was requested>", "reason": "<why it is not available>"}],
  "assumptions": ["<something you inferred that was not stated>"],
  "questions": ["<a choice worth confirming>"]
}"""


def _profile_block(profile: Dict[str, Any]) -> str:
    """The repository, as metadata. Never file contents — see the module docstring."""
    modules = profile.get("modules") or []
    dependencies = (profile.get("dependencies") or [])[:_MAX_DEPENDENCIES]
    descriptors = sorted({d.rsplit("/", 1)[-1] for d in (profile.get("descriptors") or [])})
    counts = profile.get("counts") or {}
    prefixes = profile.get("import_prefixes") or {}

    lines = [
        "PROFILE (build metadata; no source code):",
        f"  build system   {profile.get('build_system')}  ({len(modules)} module(s))",
        f"  java level     {profile.get('java_level') or 'unknown'}",
        "  file counts    " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
    ]
    if descriptors:
        lines.append("  descriptors    " + ", ".join(descriptors[:_MAX_DESCRIPTORS]))
    if modules:
        lines.append("  modules        " + ", ".join(
            f"{m.get('path') or '.'}[{m.get('packaging')}]" for m in modules[:20]))
    if prefixes:
        top = sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
        lines.append("  import prefixes " + ", ".join(f"{k} ({v})" for k, v in top))
    if dependencies:
        lines.append("  dependencies")
        lines += [f"      {d}" for d in dependencies]
    return "\n".join(lines)


class IntentAgent:
    """One Bedrock call. Returns a raw proposal for ``reconcile`` to check."""

    def __init__(self, config: ForgeConfig):
        self.config = config
        settings = config.get("intent") or {}
        self.model = settings.get("model") or config.transform_model
        self.llm = ChatBedrockConverse(model=self.model, region_name=config.aws_region)
        self.bedrock_calls = 0
        self.cost_usd = 0.0

    def propose(self, intent: str, profile: Dict[str, Any], activations: List[dict],
                decisions: Dict[str, str]) -> Optional[dict]:
        """A proposal, or ``None`` when the model's answer was unusable."""
        user_content = (
            f"REQUEST: {intent}\n\n"
            f"{_profile_block(profile)}\n\n"
            f"{render_vocabulary(activations, decisions)}"
        )
        messages = [SystemMessage(content=_SYSTEM), HumanMessage(content=user_content)]

        response = self.llm.invoke(messages)
        self.bedrock_calls += 1
        tokens_in, tokens_out = usage_from_response(response)
        self.cost_usd += estimate_cost(self.model, tokens_in, tokens_out,
                                       self.config.get("model_pricing", {}))

        try:
            proposal = extract_json(response.content)
        except Exception as e:  # noqa: BLE001 — a bad answer is a missing answer, never a guess
            _log.warning("Intent response was not valid JSON (%s); falling back to discovery", e)
            return None
        if not isinstance(proposal, dict):
            _log.warning("Intent response was not a JSON object; falling back to discovery")
            return None
        return proposal
