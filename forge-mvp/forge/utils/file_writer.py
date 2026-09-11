import re
from pathlib import Path
from typing import List, Optional

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


def write_output(state: ForgeState) -> List[str]:
    """Write transformed files to output_dir, preserving package paths.

    Returns the absolute paths written, so the build verifier knows what to
    compile. No-op returning [] when dry_run=True.
    """
    if state.get("dry_run"):
        return []

    transform_output = state["current_file"].get("transform_output") or {}
    output_dir = Path(state["output_dir"]).resolve()
    source_dir = Path(state["source_dir"]).resolve()
    written: List[str] = []

    for file_path, content in transform_output.get("files", {}).items():
        rel_path = _resolve_relative(file_path, content, source_dir)
        dest = (output_dir / rel_path).resolve()

        # The key comes from model output; never let it escape output_dir.
        if not dest.is_relative_to(output_dir):
            _log.error("Refusing to write outside output dir: %s -> %s", file_path, dest)
            continue

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(str(dest))
        _log.info("Wrote %s", dest)

    return written
