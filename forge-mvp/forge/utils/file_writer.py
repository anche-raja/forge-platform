import re
import shutil
from pathlib import Path
from typing import List, Mapping, Optional, Sequence

from forge.state import ForgeState
from forge.utils.java_checks import declared_package
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

_PUBLIC_TYPE_RE = re.compile(
    r"^\s*(?:public\s+)?(?:final\s+|abstract\s+|sealed\s+)*(?:class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)


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


def _resolve_relative(file_path: str, content: str, source_dir: Path) -> Path:
    raw = Path(file_path)
    if raw.is_absolute():
        abs_src = raw.resolve()
        try:
            return abs_src.relative_to(source_dir)
        except ValueError:
            # Absolute but outside source_dir — fall back to the declared package.
            return _package_relative_path(content) or Path(abs_src.name)
    # Already relative: treat as relative to the project root, but only if it
    # carries directory structure. A bare name loses the package, so rebuild it.
    if raw.parent != Path("."):
        return raw
    return _package_relative_path(content) or raw


def resolve_relative(file_path: str, content: str, source_dir: str) -> Path:
    """Public name for the destination-path rule; the review queue keys transformed files by it."""
    return _resolve_relative(file_path, content, source_dir)


# Held units wait here, inside output_dir, until a human approves them. Inside
# so the same path guard covers both trees; a dot-directory so the merged view
# and the acceptance checks skip it.
STAGING_DIR = ".forge-staging"


def staging_root(output_dir: str) -> Path:
    return Path(output_dir).resolve() / STAGING_DIR


def write_files(files: Mapping[str, str], source_dir: str, dest_root: Path) -> List[str]:
    """Write ``files`` (model-keyed path → content) under ``dest_root``, preserving package paths.

    The key comes from model output; it is never allowed to escape ``dest_root``.
    Returns the absolute paths written.
    """
    dest_root = Path(dest_root).resolve()
    src = Path(source_dir).resolve()
    written: List[str] = []
    for file_path, content in files.items():
        rel_path = _resolve_relative(file_path, content, src)
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


def write_output(state: ForgeState) -> List[str]:
    """Write transformed files to output_dir. No-op returning [] when dry_run=True."""
    if state.get("dry_run"):
        return []
    return write_files(_files_from(state), state["source_dir"], Path(state["output_dir"]))


def stage_output(state: ForgeState) -> List[str]:
    """Write transformed files to the staging tree instead — held for a human."""
    if state.get("dry_run"):
        return []
    return write_files(_files_from(state), state["source_dir"], staging_root(state["output_dir"]))


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
