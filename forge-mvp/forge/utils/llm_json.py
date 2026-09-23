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


# Keys a model uses when it wraps a file's text in an object instead of giving
# the text itself. Seen live: Sonnet 4.5 answered the java8-to-java21 pack with
# {"files": {"<path>": {"language": "java", "code": "...", "changelog": [...]}}}
# because the pack's schema line read `"files": {...}` and left the value's
# shape to the model.
_CONTENT_KEYS = ("content", "code", "source", "text")


class TransformShapeError(ValueError):
    """The transform's ``files`` map cannot be read as path -> file text."""


def normalize_files(files: Any) -> dict:
    """Return ``files`` as ``{path: str}``, unwrapping ``{"code": "..."}``-style values.

    Everything downstream -- the reviewer, guardrails_post, the review queue and
    the file writer -- assumes a string per path. A dict that reached
    ``Path.write_text`` raised ``TypeError: data must be str, not dict`` and
    killed the whole run, not just the file. A value this cannot read raises
    ``TransformShapeError`` so the caller can route that one file to review.
    """
    if files is None:
        return {}
    if not isinstance(files, dict):
        raise TransformShapeError(f"'files' is a {type(files).__name__}, expected an object of path -> content")
    out = {}
    for path, value in files.items():
        if isinstance(value, dict):
            found = [value[k] for k in _CONTENT_KEYS if isinstance(value.get(k), str)]
            if len(found) != 1:
                raise TransformShapeError(
                    f"'files[{path}]' is an object with keys {sorted(value)}; expected the file's text"
                )
            value = found[0]
        if not isinstance(value, str):
            raise TransformShapeError(f"'files[{path}]' is a {type(value).__name__}; expected the file's text")
        out[str(path)] = value
    return out
