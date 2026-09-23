import json
import re
from typing import Any


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.IGNORECASE | re.DOTALL)


def extract_json(content: Any) -> dict:
    """Parse JSON from a Bedrock/LangChain LLM response.

    Handles three cases that trip the naive `json.loads(response.content)`:
      1. ChatBedrockConverse returns content as a list of blocks, not a string.
      2. Models frequently wrap JSON in ```json ... ``` fences even when told not to.
      3. Models reason in prose first and put the fenced JSON at the end. Opus
         does this on build packs: "This module is a `jar` ... Let me check each
         rule." then the object. Stripping a leading fence cannot see it, and the
         parse failed as "Expecting value: line 1 column 1 (char 0)" -- which
         reads like an empty response, and held every POM of a reactor at score 0.
    """
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    if not isinstance(content, str):
        content = str(content)

    text = _FENCE_RE.sub("", content.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        if not text:
            raise
        # The answer follows the reasoning, so the last fenced block wins.
        for block in reversed(_FENCED_BLOCK_RE.findall(content)):
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                continue
        # Unfenced: the first '{' that decodes to a whole object.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            try:
                value, _ = decoder.raw_decode(content, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise first_error
