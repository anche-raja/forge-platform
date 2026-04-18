import os
from pathlib import Path

from forge.state import ForgeState


def write_output(state: ForgeState) -> None:
    """Write transformed files to output_dir, preserving package paths.

    No-op when dry_run=True.
    """
    if state.get("dry_run"):
        return

    transform_output = state["current_file"].get("transform_output") or {}
    output_dir = Path(state["output_dir"])
    source_dir = Path(state["source_dir"]).resolve()

    for file_path, content in transform_output.get("files", {}).items():
        abs_src = Path(file_path).resolve()
        try:
            rel_path = abs_src.relative_to(source_dir)
        except ValueError:
            # file_path already relative or from different root
            rel_path = Path(abs_src.name)

        dest = output_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
