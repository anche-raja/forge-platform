import os
import yaml
from pathlib import Path


class ForgeConfig:
    def __init__(self, path: str | None = None):
        if path is None:
            path = os.environ.get("FORGE_AGENTS_YAML", "agents.yaml")
        with open(path) as f:
            self._cfg: dict = yaml.safe_load(f)

    def __getattr__(self, name: str):
        try:
            return self._cfg[name]
        except KeyError:
            raise AttributeError(f"ForgeConfig has no key '{name}'")

    def get(self, name: str, default=None):
        return self._cfg.get(name, default)
