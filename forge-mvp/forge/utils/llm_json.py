import json
import re
from typing import Any


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def extract_json(content: Any) -> dict:
    """Parse JSON from a Bedrock/LangChain LLM response.

    Handles two cases that trip the naive `json.loads(response.content)`:
      1. ChatBedrockConverse returns content as a list of blocks, not a string.
      2. Models frequently wrap JSON in ```json ... ``` fences even when told not to.
    """
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    if not isinstance(content, str):
        content = str(content)

    text = content.strip()
    text = _FENCE_RE.sub("", text).strip()
    return json.loads(text)
