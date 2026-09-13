"""Find and render the context block for the unit a graph node is working on."""

from typing import Optional, Set, Tuple

from forge.config import ForgeConfig
from forge.context.render import DEFAULT_MAX_CHARS, context_digest, render_context
from forge.extract import get_context, get_extractor
from forge.phases import get_phase
from forge.state import ForgeState
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)
_warned: Set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        _log.warning(message)


def context_block_for(state: ForgeState, config: ForgeConfig) -> Tuple[Optional[str], Optional[str]]:
    """``(rendered block, digest)``, or ``(None, None)`` when the unit carries no context.

    No context for: the built-in phases, a pack with ``context: none``, a pack
    whose extractor is not built yet (warned once), or a module the extractor
    cannot make sense of (warned per module). In each case the pipeline runs as
    it did before contexts existed — the model just is not given the descriptors.
    """
    fs = state["current_file"]
    phase = state.get("phase") or fs.get("phase") or "java21"
    spec = get_phase(phase)
    name = getattr(spec, "context", "none")
    if name == "none":
        return None, None

    extractor = get_extractor(name)
    if extractor is None:
        _warn_once(f"unregistered:{name}", f"pack '{phase}' names context '{name}', but no extractor is "
                                            "registered; running without context")
        return None, None

    source_dir = state["source_dir"]
    file_path = fs["file_path"]
    module_dir = fs.get("module_dir") or extractor.module_for(file_path, source_dir)
    try:
        ctx = get_context(name, source_dir, module_dir).data
    except ValueError as e:
        _warn_once(f"module:{module_dir}", f"context '{name}' unavailable for {module_dir}: {e}; running without context")
        return None, None

    max_chars = int((config.get("context") or {}).get("max_chars", DEFAULT_MAX_CHARS))
    block = render_context(name, ctx, for_target=file_path, max_chars=max_chars)
    return block, context_digest(block)
