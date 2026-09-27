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
        # Unfenced: the first '{' that decodes to a whole, non-empty object. An
        # empty `{}` is what a Java method body looks like, and taking it as
        # the answer sent a fenced-file reply down the wrong-shape path.
        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", content):
            try:
                value, _ = decoder.raw_decode(content, match.start())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value:
                return value
        raise first_error


_ANY_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)\n?```", re.DOTALL)
# What the start of a Java or XML file looks like; prose does not.
_FILE_START_RE = re.compile(r"^\s*(package\s|import\s|<\?xml|<[A-Za-z]|//|/\*|@[A-Za-z]|public\s|final\s|class\s)")


def salvage_file_reply(content: Any, file_path: str) -> Any:
    """The transform envelope rebuilt from a reply that gave the file as a fenced block instead.

    A Java file with regexes and string literals is hard to JSON-escape, and a
    model that fails at it once tends to fail again: on AMS a class reached a
    human as "Expecting ',' delimiter: char 2291" after every retry. The
    retry now offers the model an easier answer -- the whole file, alone, in
    one fenced block -- and this reads it. The last fenced block whose text
    starts like a file is the file. ``None`` when there is no such block, so
    the caller's retry loop is unchanged.
    """
    if isinstance(content, list):
        content = "".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content)
    if not isinstance(content, str):
        return None
    for block in reversed(_ANY_FENCE_RE.findall(content)):
        text = block.strip("\n")
        if text.lstrip().startswith("{"):
            continue                     # a JSON envelope the caller already failed to parse
        if _FILE_START_RE.match(text) and text.count("\n") >= 2:
            return {"files": {file_path: text + ("\n" if not text.endswith("\n") else "")},
                    "deleted_files": [], "manual_flags": [], "salvaged": "fenced file"}
    return None


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
