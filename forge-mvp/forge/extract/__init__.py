"""Context extractors — deterministic, model-free parsers that produce the
cross-file facts a pack's rules reference.

A pack declares ``context: <name>``; the engine guarantees that extractor has
run before any model call for that pack. Extractors also resolve the pack's
``selector:`` entries — file sets that cannot be named by a path glob ("every
class web.xml registers as a filter").

Nothing an extractor produces is stored in ``ForgeState``: the context is cached
per process and re-rendered on demand, because a checkpointed state item has a
400 KB ceiling in DynamoDB and a parsed descriptor set can exceed it.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Mapping, NamedTuple, Optional, Tuple


class Selection(NamedTuple):
    """What a selector resolved to.

    ``files`` exist and will be transformed. ``generated`` are target paths that
    do not exist yet — a pack that produces a new file (Tomcat ``META-INF/context.xml``)
    names where it goes, and the transform is given the context instead of a
    source file.
    """

    files: Tuple[str, ...] = ()
    generated: Tuple[str, ...] = ()


SelectorFn = Callable[[dict, str], Selection]
RunFn = Callable[[str, Optional[str]], dict]


@dataclass(frozen=True)
class Extractor:
    name: str
    run: RunFn                                   # (source_dir, module_dir) -> JSON-able dict
    selectors: Mapping[str, SelectorFn]          # selector name -> resolver
    find_modules: Callable[[str], Tuple[str, ...]]   # source_dir -> module dirs it applies to
    module_for: Callable[[str, str], str]        # (file_path, source_dir) -> module dir
    # An extractor whose facts change as the run writes -- the `reactor`
    # reads a parent this run has already rewritten -- is run as
    # ``run(source_dir, module_dir, output_dir=...)`` and never cached.
    wants_output_dir: bool = False

    def provides(self, selector: str) -> bool:
        return selector in self.selectors


class Context(NamedTuple):
    name: str
    module_dir: str
    data: dict


EXTRACTORS: Dict[str, Extractor] = {}


def register(extractor: Extractor) -> Extractor:
    if extractor.name in EXTRACTORS:
        raise ValueError(f"extractor '{extractor.name}' is already registered")
    EXTRACTORS[extractor.name] = extractor
    return extractor


def get_extractor(name: str) -> Optional[Extractor]:
    return EXTRACTORS.get(name)


@lru_cache(maxsize=64)
def _cached(name: str, source_dir: str, module_dir: str) -> Context:
    extractor = EXTRACTORS[name]
    return Context(name=name, module_dir=module_dir, data=extractor.run(source_dir, module_dir))


def get_context(name: str, source_dir: str, module_dir: str, output_dir: Optional[str] = None) -> Context:
    """The extracted context for one module, computed once per process.

    Source is never mutated during a run — output goes to ``output_dir`` — so
    caching on the resolved paths is safe for the life of the process. An
    extractor that ``wants_output_dir`` reads what the run has written so far,
    which does change, so it is computed every time and handed ``output_dir``.
    """
    if name not in EXTRACTORS:
        raise KeyError(f"no extractor registered for context '{name}'")
    source, module = str(Path(source_dir).resolve()), str(Path(module_dir).resolve())
    extractor = EXTRACTORS[name]
    if extractor.wants_output_dir:
        out = str(Path(output_dir).resolve()) if output_dir else None
        return Context(name=name, module_dir=module, data=extractor.run(source, module, output_dir=out))
    return _cached(name, source, module)


def clear_context_cache() -> None:
    _cached.cache_clear()
    # Extractors may keep their own memoised indexes.
    for extractor in EXTRACTORS.values():
        clear = getattr(extractor.run, "cache_clear", None)
        if clear:
            clear()
    from forge.extract import web_bootstrap as _wb

    _wb.clear_caches()


# Registration — import for side effect. Keep at the bottom so ``Selection`` and
# ``register`` exist before the extractor modules import them.
from forge.extract import web_bootstrap as _web_bootstrap  # noqa: E402,F401
from forge.extract import reactor as _reactor  # noqa: E402,F401

__all__ = [
    "Context",
    "EXTRACTORS",
    "Extractor",
    "Selection",
    "clear_context_cache",
    "get_context",
    "get_extractor",
    "register",
]
