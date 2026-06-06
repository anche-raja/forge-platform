"""Lenient JSON parsing for LLM responses.

Models frequently wrap JSON in ```json ... ``` markdown fences (or emit a list of
content blocks) even when told not to. `loads_lenient` strips fences, joins block
lists, and falls back to extracting the outermost {...} object before parsing.
"""

import json


def _to_text(content) -> str:
    if isinstance(content, str):
        return content
    # ChatBedrockConverse can return a list of content blocks
    if isinstance(content, list):
        return "".join(
            (b.get("text", "") if isinstance(b, dict) else str(b)) for b in content
        )
    return str(content)


def loads_lenient(content):
    """Parse JSON from an LLM response, tolerating markdown fences and preamble."""
    s = _to_text(content).strip()

    # Strip a leading ```json / ``` fence and trailing ```
    if s.startswith("```"):
        s = s[3:]
        nl = s.find("\n")
        if nl != -1 and s[:nl].strip().isalpha():  # drop a language tag like "json"
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Last resort: grab the outermost object
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            return json.loads(s[i:j + 1])
        raise
