import copy
import os
from pathlib import Path
from typing import Mapping, Optional

import yaml

# `agents.yaml.example` ships this so a reader can see the shape of the key.
# Left in place it reaches Bedrock, which rejects it with "Guardrail was enabled
# but input is in incorrect format" — a message about the wrong thing entirely,
# under a botocore traceback, three model calls into the run.
_PLACEHOLDERS = {
    "guardrail_id": "REPLACE_WITH_GUARDRAIL_ID",
}


class ConfigError(Exception):
    """The config cannot produce a working run, and says what to do about it."""


class ForgeConfig:
    def __init__(self, path: Optional[str] = None, *, data: Optional[dict] = None):
        if data is not None:
            # Built in-process (a UI run with per-run decisions); nothing on disk.
            self._cfg: dict = dict(data)
            return
        if path is None:
            path = os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")
        with open(path) as f:
            loaded = yaml.safe_load(f)
        # An empty file parses to None, and a bare scalar to a str/int. Either
        # one sails past every `config is not None` guard downstream and only
        # fails inside `.get`, as an AttributeError several frames deep in
        # whatever happened to ask for a key first. A 0-byte agents.yaml is the
        # common case: a shell redirect truncates its target before the
        # generator runs, so a failed run leaves one behind. `--out` exists to
        # avoid exactly that, which is why the fix below names it.
        if loaded is None:
            raise ConfigError(
                f"{path} is empty, so there is no model, region or guardrail to run with.\n"
                "A 0-byte file is usually a failed generator run: `> file` empties its target "
                "before the script starts, so nothing is left when the script then fails.\n"
                "Regenerate it with --out, which only replaces the file on success:\n"
                "  ./forge-terraform/scripts/generate-agents-yaml.sh dev --out forge-mvp/agents.yaml\n"
                "Until that succeeds, delete the empty file so the error names the missing config."
            )
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"{path} is not a YAML mapping -- it parsed as {type(loaded).__name__}.\n"
                "It must be a block of top-level keys; agents.yaml.example shows the shape."
            )
        self._cfg = loaded
        self._reject_placeholders(path)

    def _reject_placeholders(self, path: str) -> None:
        stale = [k for k, v in _PLACEHOLDERS.items() if (self._cfg or {}).get(k) == v]
        if not stale:
            return
        raise ConfigError(
            f"{path} still has the template placeholder for: {', '.join(stale)}.\n"
            "It is a reference for the keys, not a runnable config. Generate the real one:\n"
            "  ./forge-terraform/scripts/generate-agents-yaml.sh dev --out forge-mvp/agents.yaml\n"
            "That reads your terraform outputs, so re-run it after any terraform apply — "
            "editing a guardrail publishes a new version and the pipeline pins the version."
        )

    def __getattr__(self, name: str):
        try:
            return self._cfg[name]
        except KeyError:
            raise AttributeError(f"ForgeConfig has no key '{name}'")

    def get(self, name: str, default=None):
        return self._cfg.get(name, default)

    def with_overrides(self, overrides: Mapping) -> "ForgeConfig":
        """A copy with ``overrides`` applied; the original is untouched.

        Nested mappings merge rather than replace, so a run can set
        ``decisions.risk_ceiling`` without discarding the other decisions the
        file declares. Every consumer reads through ``.get``/attribute access on
        the merged dict, so the override reaches the hold gate, acceptance and
        discovery without any of them changing.
        """
        merged = copy.deepcopy(self._cfg or {})
        for key, value in overrides.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = copy.deepcopy(value)
        return ForgeConfig(data=merged)


# ─── Model output budget ──────────────────────────────────────────────────────

# Large enough for a whole migrated file inside a JSON envelope. A pom.xml of a
# few hundred lines is the common case, and JSON-escaping it costs more tokens
# than its line count suggests. This is a cap, not a charge -- only tokens the
# model actually emits are billed -- so it is set generously on purpose.
DEFAULT_MODEL_MAX_TOKENS = 16384

# Below this a transform cannot return a file of any size, so a value that low
# is treated as a mistake rather than honoured.
MIN_MODEL_MAX_TOKENS = 256


# Output ceilings Bedrock enforces per model, matched by substring of the model
# ID. Asking for more is not clamped: Converse rejects the whole call with
# "The maximum tokens you requested exceeds the model limit of 10000". The
# shared max_tokens is sized for the transform model, so the Nova reviewer
# failed every call until this cap. `model_output_limits` in agents.yaml adds
# or overrides entries.
MODEL_OUTPUT_LIMITS = {
    "anthropic.claude-opus-4-8": 128000,
    "amazon.nova-pro": 10000,
    "amazon.nova-lite": 10000,
    "amazon.nova-micro": 10000,
}


def model_max_tokens(config, model: Optional[str] = None) -> int:
    """``max_tokens`` for ``model``, capped at the model's own output limit."""
    budget = _shared_max_tokens(config)
    if not model:
        return budget
    limits = {**MODEL_OUTPUT_LIMITS, **((config.get("model_output_limits") if config is not None else None) or {})}
    # Every matching entry is a ceiling, so the strictest one wins.
    for fragment, limit in limits.items():
        if fragment in model:
            try:
                budget = min(budget, int(limit))
            except (TypeError, ValueError):
                continue
    return budget


def _shared_max_tokens(config) -> int:
    """The Converse ``maxTokens`` for every pipeline model call.

    ``ChatBedrockConverse`` defaults this to ``None``, which omits ``maxTokens``
    from the request and lets Bedrock apply its own much smaller default. A
    transform that has to return a whole file inside a JSON envelope then stops
    mid-object; worse, on a reasoning model that spends the budget before
    emitting any text it returns an *empty* content block, which reaches
    ``extract_json`` as ``""`` and fails as "Expecting value: line 1 column 1
    (char 0)" -- a parse error that looks nothing like the token limit it is.

    The leader reads its own ``leader.max_tokens``; this is the one every other
    model call shares.
    """
    raw = config.get("max_tokens") if config is not None else None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MODEL_MAX_TOKENS
    return value if value >= MIN_MODEL_MAX_TOKENS else DEFAULT_MODEL_MAX_TOKENS


# ─── Bedrock client timeouts ──────────────────────────────────────────────────

# Converse is not streamed, so no byte arrives until the whole reply is written.
# botocore's default read_timeout is 60s, and Opus returning a large pom.xml in
# a JSON envelope runs past it: the run died with ReadTimeoutError, after
# botocore had quietly retried the same 60s wait. Ten minutes covers a
# DEFAULT_MODEL_MAX_TOKENS reply with room to spare.
DEFAULT_BEDROCK_READ_TIMEOUT = 600
MIN_BEDROCK_READ_TIMEOUT = 60


def bedrock_client_config(config):
    """The botocore ``Config`` for every Bedrock runtime client.

    ``bedrock_read_timeout`` (seconds) overrides the read timeout; a value below
    botocore's own default is treated as a mistake, like a crippling max_tokens.
    Retries stay few: a timed-out call is re-billed in full, and a slow reply
    retried is just as slow.
    """
    from botocore.config import Config

    raw = config.get("bedrock_read_timeout") if config is not None else None
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        timeout = DEFAULT_BEDROCK_READ_TIMEOUT
    if timeout < MIN_BEDROCK_READ_TIMEOUT:
        timeout = DEFAULT_BEDROCK_READ_TIMEOUT
    return Config(
        read_timeout=timeout,
        connect_timeout=10,
        retries={"max_attempts": 2, "mode": "adaptive"},
    )
