"""Where a transformed file lands. The model's path key is a hint, never the answer.

A migration puts a rewritten file back where it came from. The key the model
returns for it varies by model and by mood: the original path, the original
path with its leading slash lost (``Users/raja/...``), the path from the module
root instead of the reactor root (``src/test/java/...``), a path missing one
directory (``AssetManagementInternalWeb/src/...``), or a bare filename. On AMS
the JUnit pack committed three tests at three of those, and the build failed
on the two originals it had left behind.

So the unit's own rewrite goes to the unit's own path, whatever the key says;
any other key that names the tail of exactly one existing source file goes to
that file; a bare filename is rebuilt from its package and matched the same
way; and nothing may escape the output directory.
"""

import os
import re
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from forge.state import ForgeState
from forge.utils.fs import prune_dirs
from forge.utils.java_checks import declared_package
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

_PUBLIC_TYPE_RE = re.compile(
    r"^\s*(?:public\s+)?(?:final\s+|abstract\s+|sealed\s+)*(?:class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)
_ARTIFACT_DIRS = ("target", "build", ".git", "node_modules", ".idea")


def _package_relative_path(content: str) -> Optional[Path]:
    """Derive src/main/java/<pkg>/<Type>.java from the source's own declarations.

    Used when the model returns a bare filename instead of the original path —
    the package path must be preserved exactly, so reconstruct it rather than
    flattening the file into the output root.
    """
    pkg = declared_package(content)
    type_name = _PUBLIC_TYPE_RE.search(content)
    if not pkg or not type_name:
        return None
    return Path("src/main/java", *pkg.split("."), f"{type_name.group(1)}.java")


@lru_cache(maxsize=16)
def _source_index(source_dir: str) -> Dict[str, Tuple[str, ...]]:
    """``basename -> relative paths`` of every file under ``source_dir``, FORGE output and build dirs pruned.

    Cached per source tree: a run never mutates its source (output goes to
    ``output_dir``), and one walk serves every unit of the run.
    """
    index: Dict[str, List[str]] = {}
    root = Path(source_dir)
    for dirpath, dirs, files in os.walk(root):
        prune_dirs(dirpath, dirs, also=_ARTIFACT_DIRS)
        for name in files:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            index.setdefault(name, []).append(rel)
    return {name: tuple(sorted(paths)) for name, paths in index.items()}


def clear_source_index() -> None:
    _source_index.cache_clear()


def _match_existing(rel: Path, source_dir: Path) -> Optional[Path]:
    """The one existing source file whose path ends with ``rel``, if there is exactly one."""
    candidates = _source_index(str(source_dir)).get(rel.name, ())
    tail = rel.as_posix().lstrip("./")
    matches = [c for c in candidates if c == tail or c.endswith("/" + tail)]
    return Path(matches[0]) if len(matches) == 1 else None


def _resolve_relative(file_path: str, content: str, source_dir: Path) -> Path:
    raw = Path(file_path)
    if raw.is_absolute():
        abs_src = raw.resolve()
        try:
            return abs_src.relative_to(source_dir)
        except ValueError:
            # Absolute but outside source_dir — fall back to the declared package.
            return _package_relative_path(content) or Path(abs_src.name)
    if raw.parent != Path("."):
        # An absolute path that lost its leading slash still names a source file.
        try:
            return (Path("/") / raw).resolve().relative_to(source_dir)
        except ValueError:
            pass
        if (source_dir / raw).is_file():
            return raw
        # A path from the module root, or missing a directory: the tail of one
        # existing file is that file.
        return _match_existing(raw, source_dir) or raw
    # A bare name loses the package, so rebuild it, then find its module.
    by_package = _package_relative_path(content)
    if by_package is not None:
        return _match_existing(by_package, source_dir) or by_package
    return _match_existing(raw, source_dir) or raw


def resolve_relative(file_path: str, content: str, source_dir: str) -> Path:
    """Public name for the destination-path rule; the review queue keys transformed files by it."""
    return _resolve_relative(file_path, content, Path(source_dir).resolve())


# Held units wait here, inside output_dir, until a human approves them. Inside
# so the same path guard covers both trees; a dot-directory so the merged view
# and the acceptance checks skip it.
STAGING_DIR = ".forge-staging"


def staging_root(output_dir: str) -> Path:
    return Path(output_dir).resolve() / STAGING_DIR


def write_files(files: Mapping[str, str], source_dir: str, dest_root: Path, *,
                unit_path: Optional[str] = None, deleted: Sequence[str] = ()) -> List[str]:
    """Write ``files`` (model-keyed path → content) under ``dest_root``, preserving package paths.

    The key comes from model output; it is never allowed to escape ``dest_root``.
    When the transform returned one file for the unit at ``unit_path`` and
    retired nothing, that file *is* the unit's rewrite and lands at the unit's
    own path whatever the key says; a rename or move is what ``deleted``
    is for. Returns the absolute paths written.
    """
    dest_root = Path(dest_root).resolve()
    src = Path(source_dir).resolve()
    written: List[str] = []
    own_rel: Optional[Path] = None
    if unit_path and len(files) == 1 and not deleted:
        try:
            own_rel = Path(unit_path).resolve().relative_to(src)
        except ValueError:
            own_rel = None
    for file_path, content in files.items():
        rel_path = own_rel if own_rel is not None else _resolve_relative(file_path, content, src)
        if own_rel is not None and Path(file_path).name != own_rel.name:
            _log.info("Transform keyed %s as %s; written at the unit's own path", own_rel, file_path)
        dest = (dest_root / rel_path).resolve()
        if not dest.is_relative_to(dest_root):
            _log.error("Refusing to write outside %s: %s -> %s", dest_root, file_path, dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(str(dest))
        _log.info("Wrote %s", dest)
    return written


def _files_from(state: ForgeState) -> Mapping[str, str]:
    out = state["current_file"].get("transform_output") or {}
    return out.get("files", {}) if isinstance(out, dict) else {}


def _unit_of(state: ForgeState) -> Tuple[Optional[str], Sequence[str]]:
    """``(the unit's own path or None for a generated target, what it retired)``."""
    fs = state["current_file"]
    out = fs.get("transform_output") or {}
    deleted = out.get("deleted_files") if isinstance(out, dict) else None
    unit = None if fs.get("generate") else fs.get("file_path")
    return unit, list(deleted or [])


def write_output(state: ForgeState) -> List[str]:
    """Write transformed files to output_dir. No-op returning [] when dry_run=True."""
    if state.get("dry_run"):
        return []
    unit, deleted = _unit_of(state)
    return write_files(_files_from(state), state["source_dir"], Path(state["output_dir"]),
                       unit_path=unit, deleted=deleted)


def stage_output(state: ForgeState) -> List[str]:
    """Write transformed files to the staging tree instead — held for a human."""
    if state.get("dry_run"):
        return []
    unit, deleted = _unit_of(state)
    return write_files(_files_from(state), state["source_dir"], staging_root(state["output_dir"]),
                       unit_path=unit, deleted=deleted)


def promote_staged(output_dir: str, held_paths: Sequence[str]) -> List[str]:
    """Move approved files from staging into output_dir. Both ends are guarded."""
    root = Path(output_dir).resolve()
    stage = staging_root(output_dir)
    written: List[str] = []
    for held in held_paths:
        src = Path(held).resolve()
        if not src.is_relative_to(stage) or not src.is_file():
            _log.error("Refusing to promote %s: not a staged file", held)
            continue
        dest = (root / src.relative_to(stage)).resolve()
        if not dest.is_relative_to(root):
            _log.error("Refusing to promote %s outside output dir", held)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        written.append(str(dest))
        _log.info("Promoted %s", dest)
    _prune_empty(stage)
    return written


def discard_staged(output_dir: str, held_paths: Sequence[str]) -> None:
    """Remove rejected or retried units from staging."""
    stage = staging_root(output_dir)
    for held in held_paths:
        p = Path(held).resolve()
        if p.is_relative_to(stage) and p.is_file():
            p.unlink()
    _prune_empty(stage)


def _prune_empty(root: Path) -> None:
    if not root.is_dir():
        return
    for d in sorted((d for d in root.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
