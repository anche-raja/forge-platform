"""Persist the extracted context alongside the migration output.

Two reasons. A prompt block is bounded and may omit sections; the file is the
complete record the block points at. And the pre-migration facts a later
acceptance check diffs — the filter chain, the authorization rules, the
datasources — must survive the run that changes them.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from forge.context.render import context_digest, render_context
from forge.extract import get_context

SNAPSHOT_NAME = "migration-context.json"


def write_context_snapshot(output_dir: str, name: str, source_dir: str, modules: Sequence[str]) -> Path:
    """Write ``<output_dir>/migration-context.json`` for every module given."""
    src = Path(source_dir).resolve()
    entries = {}
    for module in modules:
        ctx = get_context(name, str(src), module).data
        rendered = render_context(name, ctx, for_target=module)
        entries[ctx.get("module_rel", ".")] = {
            "module_dir": ctx.get("module_dir"),
            "digest": context_digest(rendered),
            "filter_chain": ctx.get("filter_chain", []),
            "authz": ctx.get("authz", {}),
            "datasources": ctx.get("datasources", []),
            "notes": ctx.get("summary", {}).get("notes", []),
            "context": ctx,
        }
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / SNAPSHOT_NAME
    path.write_text(json.dumps({
        "context": name,
        "source_dir": str(src),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "modules": entries,
    }, indent=2, default=str), encoding="utf-8")
    return path
