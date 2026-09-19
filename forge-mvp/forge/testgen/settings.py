"""The ``test_generation`` block of agents.yaml, resolved once.

Defaults live here rather than at every call site, so a missing block behaves
exactly like the documented one. The two model ids default to the migration's
own: the same cross-validation — one model writes, a different model grades.
"""

from dataclasses import dataclass
from typing import Tuple

DEFAULT_PASS_THRESHOLD = 75
DEFAULT_RETRY_THRESHOLD = 50
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_SOURCE_CHARS = 60_000
DEFAULT_CONTEXT_CHARS = 12_000
RUN_MODES = ("maven", "gradle", "command")


@dataclass(frozen=True)
class RunSettings:
    """How to execute a generated test. Opt-in, like build verification."""

    enabled: bool = False
    mode: str = "maven"
    command: str = ""
    timeout_seconds: int = 900


@dataclass(frozen=True)
class TestGenSettings:
    enabled: bool = True
    style: str = "junit5"
    model: str = ""
    review_model: str = ""
    pass_threshold: int = DEFAULT_PASS_THRESHOLD
    retry_threshold: int = DEFAULT_RETRY_THRESHOLD
    max_retries: int = DEFAULT_MAX_RETRIES
    # Regenerate over a test that already exists. Off: an existing test is a
    # human's work, and overwriting it is the one thing this must never do.
    overwrite: bool = False
    max_source_chars: int = DEFAULT_MAX_SOURCE_CHARS
    context_max_chars: int = DEFAULT_CONTEXT_CHARS
    # Which detected kinds to generate for. Empty means every kind.
    kinds: Tuple[str, ...] = ()
    run: RunSettings = RunSettings()

    @classmethod
    def from_config(cls, config) -> "TestGenSettings":
        block = (config.get("test_generation") if config is not None else None) or {}
        run = block.get("run_tests") or {}
        mode = str(run.get("mode") or "maven").lower()
        if mode not in RUN_MODES:
            mode = "maven"
        return cls(
            enabled=bool(block.get("enabled", True)),
            style=str(block.get("style") or "junit5"),
            model=str(block.get("model") or (config.get("transform_model", "") if config else "")),
            review_model=str(block.get("review_model") or (config.get("review_model", "") if config else "")),
            pass_threshold=int(block.get("pass_threshold", DEFAULT_PASS_THRESHOLD)),
            retry_threshold=int(block.get("retry_threshold", DEFAULT_RETRY_THRESHOLD)),
            max_retries=int(block.get("max_retries", DEFAULT_MAX_RETRIES)),
            overwrite=bool(block.get("overwrite", False)),
            max_source_chars=int(block.get("max_source_chars", DEFAULT_MAX_SOURCE_CHARS)),
            context_max_chars=int(block.get("context_max_chars", DEFAULT_CONTEXT_CHARS)),
            kinds=tuple(str(k) for k in (block.get("kinds") or ())),
            run=RunSettings(
                enabled=bool(run.get("enabled", False)),
                mode=mode,
                command=str(run.get("command") or ""),
                timeout_seconds=int(run.get("timeout_seconds", 900)),
            ),
        )
