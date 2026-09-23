"""Which pack wrote which file into an output directory.

Packs do not compose. ``run_migration`` scans ``source_dir`` and the transform
opens that path, so the second pack to touch a file is handed the *original*,
not the first pack's result — and ``write_output`` overwrites rather than
merging. Running ``javax-to-jakarta`` and then ``java8-to-java21`` over the same
tree therefore throws away the Jakarta rename, silently and with no failed file.

The engine cannot fix that by merging: two packs' transforms of the same file
are two different answers to two different questions, and picking one is not a
mechanical decision. What it can do is refuse. This module records what each
pack wrote so the next pack can be stopped before it spends anything.

The combined ``java21`` and ``struts-spring6`` phases exist precisely because
overlapping concerns have to be asked in a single pass; chaining
``--output-dir`` into the next run's ``source_dir`` is the other honest answer.
"""

import json
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

MANIFEST_NAME = ".forge-writes.json"


def _path(output_dir: str) -> Path:
    return Path(output_dir).resolve() / MANIFEST_NAME


def _read(output_dir: str) -> Dict[str, Dict[str, str]]:
    """The raw manifest, as ``{"writes": {...}, "deleted": {...}}``.

    A missing or unreadable manifest is an empty one: this is a guard, and a
    guard that cannot read its own notes must not block a run.
    """
    try:
        data = json.loads(_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"writes": {}, "deleted": {}}
    if not isinstance(data, dict):
        return {"writes": {}, "deleted": {}}
    # The first shape was a flat {path: phase} map of writes.
    if "writes" not in data and "deleted" not in data:
        return {"writes": {str(k): str(v) for k, v in data.items()}, "deleted": {}}
    return {
        "writes": {str(k): str(v) for k, v in (data.get("writes") or {}).items()},
        "deleted": {str(k): str(v) for k, v in (data.get("deleted") or {}).items()},
    }


def load(output_dir: str) -> Dict[str, str]:
    """``{relative path: phase}`` for everything written into this directory."""
    return _read(output_dir)["writes"]


def deleted_paths(output_dir: str) -> List[str]:
    """Relative paths an earlier pack retired.

    A chained run must not hand the next pack a descriptor the last one
    replaced — it would migrate a file that is on its way out, and pay for it.
    """
    return sorted(_read(output_dir)["deleted"])


def record(output_dir: str, phase: str, written: Sequence[str],
           deleted: Sequence[str] = ()) -> None:
    """Note what ``phase`` wrote and retired (paths relative to ``output_dir``)."""
    if not written and not deleted:
        return
    manifest = _read(output_dir)
    for rel in written:
        manifest["writes"][_rel(output_dir, rel)] = phase
    for rel in deleted:
        manifest["deleted"][_rel(output_dir, rel)] = phase
    target = _path(output_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        # Losing the manifest costs the next run its guard, not this run its work.
        pass


def forget(output_dir: str, rels: Sequence[str]) -> None:
    """Drop ``rels`` from the writes: the output no longer holds FORGE's copy of them.

    Used when a damaged file is moved out of the output tree, so the chained
    view falls back to the original and no pack is still recorded as its owner.
    """
    manifest = _read(output_dir)
    gone = [r for r in rels if r in manifest["writes"]]
    if not gone:
        return
    for rel in gone:
        del manifest["writes"][rel]
    try:
        _path(output_dir).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _rel(output_dir: str, path: str) -> str:
    root = Path(output_dir).resolve()
    p = Path(path)
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def conflicts(output_dir: str, phase: str, source_dir: str,
              unit_paths: Sequence[str]) -> List[Tuple[str, str]]:
    """``[(relative path, the phase that wrote it)]`` for units another pack already wrote.

    Matched on the unit's path relative to ``source_dir``, because that is the
    path ``write_output`` mirrors into the output tree. Re-running the *same*
    phase is not a conflict — a pack must stay re-runnable after a fix.
    """
    manifest = load(output_dir)
    if not manifest:
        return []
    root = Path(source_dir).resolve()
    out = []
    for unit in unit_paths:
        try:
            rel = Path(unit).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        owner = manifest.get(rel)
        if owner and owner != phase:
            out.append((rel, owner))
    return out


def refusal(phase: str, clashes: Sequence[Tuple[str, str]]) -> str:
    """The message a caller shows instead of clobbering an earlier pack's work."""
    owners = sorted({owner for _, owner in clashes})
    shown = "\n".join(f"  {rel}  (written by {owner})" for rel, owner in list(clashes)[:5])
    more = f"\n  … and {len(clashes) - 5} more" if len(clashes) > 5 else ""
    return (
        f"'{phase}' would overwrite {len(clashes)} file(s) that {' and '.join(owners)} "
        f"already migrated into this output directory:\n{shown}{more}\n\n"
        "Packs do not compose: this run reads the ORIGINAL source, so its output would "
        "replace the earlier pack's work rather than build on it.\n\n"
        "Either run a combined phase that does both transformations in one pass "
        "(--phase java21, --phase struts-spring6), or chain them by making the first "
        "pack's output the next run's source:\n"
        "  migrate.py <src>    --phase <first>  --output-dir ./step1\n"
        "  migrate.py ./step1  --phase <second> --output-dir ./step2"
    )
