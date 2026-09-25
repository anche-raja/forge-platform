"""Find and render the context block for the unit a graph node is working on."""

from pathlib import Path
from typing import List, Optional, Set, Tuple

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


def declared_context(state: ForgeState) -> str:
    """The context name the running pack declares — ``"none"`` when it declares one.

    Read separately from :func:`context_block_for` so a caller can tell the two
    reasons for an empty block apart: a pack that wants no context, and a pack
    whose extractor does not exist yet. Both produce ``None`` from
    ``context_block_for``, and conflating them is how a degraded run looks like
    a clean one.
    """
    fs = state["current_file"]
    phase = state.get("phase") or fs.get("phase") or "java21"
    return getattr(get_phase(phase), "context", "none") or "none"


def context_available(name: str) -> bool:
    """Whether an extractor is registered for ``name``. ``"none"`` is not a context."""
    return name != "none" and get_extractor(name) is not None


def decisions_block(state: ForgeState, config: ForgeConfig) -> Optional[str]:
    """The project decisions the running pack declares, as its transform and review read them.

    Packs branch on decisions -- "per the `container` decision", "only on a
    bare servlet container (tomcat, jetty)" -- and until this block existed
    the value never reached the prompt: setting ``container: tomcat`` switched
    the Liberty pack off at discovery and changed nothing any model wrote. The
    values are the ones discovery resolves: agents.yaml (with a chat's intent
    overrides) over the platform defaults. A pack that declares no decisions,
    and every built-in phase, gets no block.
    """
    from forge.discover.emit import DEFAULT_DECISIONS

    fs = state["current_file"]
    names = tuple(getattr(get_phase(state.get("phase") or fs.get("phase") or "java21"), "decisions", ()) or ())
    if not names:
        return None
    values = {**DEFAULT_DECISIONS, **dict(config.get("decisions") or {})}
    lines = [f"- {name}: {values[name]}" for name in names if values.get(name)]
    if not lines:
        return None
    return ("PROJECT DECISIONS (fixed for this project; a rule that depends on one of these means this value):\n"
            + "\n".join(lines))


def plan_packs(state: ForgeState, config: ForgeConfig) -> Optional[List[str]]:
    """The packs this run belongs to, or None when nothing says.

    The chat's plan first (``plan_packs``, set by the leader from the packs
    discovery selected), then the ``packs:`` list of the ``forge-profile.yaml``
    discovery wrote into the output directory -- the file a CLI user edits to
    skip a pack.
    """
    chosen = config.get("plan_packs")
    if isinstance(chosen, (list, tuple)):
        return [str(p) for p in chosen]
    out = state.get("output_dir")
    if not out:
        return None
    import yaml

    from forge.discover.emit import PROFILE_YAML

    try:
        data = yaml.safe_load((Path(out) / PROFILE_YAML).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    packs = data.get("packs") if isinstance(data, dict) else None
    return [str(p) for p in packs if p] if isinstance(packs, list) else None


def coordinates_block(state: ForgeState, config: ForgeConfig) -> Optional[str]:
    """For a build-tier pack: the ``eliminates`` and ``upgrades`` the plan's other packs declare.

    "A pack declares the coordinate changes its technology requires, and the
    build pack applies them" (forge/packs/spec.py) -- but until this block the
    lists were parsed and never sent, so the build pack's "remove every
    coordinate in the eliminates list supplied to you" had no list. It guessed:
    on AMS it dropped junit:junit with nothing in its place and left Struts on
    6.8.0 under code the Struts pack had moved to 7.3.0.

    Only runnable packs of the plan count: a pack the plan set aside, or one
    that cannot run yet, migrates no code, so the build must not change its
    coordinates. Empty lists are said, so an empty list is not read as a gap to
    fill. No plan at all (a bare ``--phase`` run with no profile) gives no
    block, with a warning.
    """
    from forge.utils.file_scanner import runnable_phases

    fs = state["current_file"]
    spec = get_phase(state.get("phase") or fs.get("phase") or "java21")
    if getattr(spec, "tier", None) != "build":
        return None
    packs = plan_packs(state, config)
    if packs is None:
        _warn_once("coordinates:no-plan", "no plan and no forge-profile.yaml: the build pack runs without the "
                                          "eliminates/upgrades the other packs declare")
        return None
    runnable = set(runnable_phases())
    eliminates: List[str] = []
    upgrades: List[str] = []
    for pack_id in dict.fromkeys(packs):
        if pack_id not in runnable:
            continue
        try:
            other = get_phase(pack_id)
        except ValueError:
            continue
        eliminates += [f"- {c}   (for {pack_id})" for c in getattr(other, "eliminates", ()) or ()]
        upgrades += [f"- {c}   (for {pack_id})" for c in getattr(other, "upgrades", ()) or ()]
    return "\n".join(
        ["DEPENDENCY CHANGES THE PACKS IN THIS PLAN REQUIRE (Rules 3 and 4 act on exactly these; add nothing):",
         "eliminates:"] + (eliminates or ["- none"])
        + ["upgrades:"] + (upgrades or ["- none"]))


def context_block_for(state: ForgeState, config: ForgeConfig) -> Tuple[Optional[str], Optional[str]]:
    """``(rendered block, digest)``, or ``(None, None)`` when the unit carries no context.

    No context for: the built-in phases, a pack with ``context: none``, a pack
    whose extractor is not built yet (warned once), or a module the extractor
    cannot make sense of (warned per module). In each case the pipeline runs as
    it did before contexts existed — the model just is not given the descriptors.

    A ``None`` here is not self-explaining: use :func:`declared_context` and
    :func:`context_available` to record *why* it was empty.
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
