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
            self._cfg = yaml.safe_load(f)
        self._reject_placeholders(path)

    def _reject_placeholders(self, path: str) -> None:
        stale = [k for k, v in _PLACEHOLDERS.items() if (self._cfg or {}).get(k) == v]
        if not stale:
            return
        raise ConfigError(
            f"{path} still has the template placeholder for: {', '.join(stale)}.\n"
            "It is a reference for the keys, not a runnable config. Generate the real one:\n"
            "  ./forge-terraform/scripts/generate-agents-yaml.sh dev > forge-mvp/agents.yaml\n"
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
