import copy
import os
from pathlib import Path
from typing import Mapping, Optional

import yaml


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
