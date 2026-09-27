"""The ``reactor`` extractor: what a pom inherits, from the parents as this run has them.

The build pack migrates one pom at a time, but a pom is not judged alone. A
child module inherits its Java level, its BOM imports and its managed versions
from a parent chain it never restates, and version pinning belongs in the
parent while dependencies belong in the module. Given only the child's bytes,
a transform guesses where a setting lives and a reviewer either deducts for
what it cannot see -- on AMS a sound services pom scored 45, every deduction
reading "parent not provided, cannot verify" -- or has to be told to assume.

So this extractor hands both of them the parent chain: each parent's
coordinates, packaging, ``<properties>``, BOM imports, managed dependency and
plugin versions, plus the module graph of the reactor, all read with no model.
A parent this run has already rewritten into the output directory is read from
there and marked ``rewritten``; the build tier runs parents before children,
one at a time, so a child is judged against what its parent has become, not
what it was. The module's own pre-migration values are deliberately not
restated: the transform has the original and the reviewer has the transform,
and a root pom was once marked down against its own old properties shown
back to it as "context".

Deterministic, local, no network. Not cached: what a parent is changes as the
run proceeds, and reading a handful of small poms costs nothing.
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from forge.extract import Extractor, register
from forge.utils.fs import prune_dirs

NAME = "reactor"
MAX_PARENT_DEPTH = 8
# Build output and version control never hold a reactor's poms.
_SKIP_DIRS = ("target", "build", ".git", "node_modules", ".idea")


# ─── reading a pom ────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child(el: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    if el is None:
        return None
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: Optional[ET.Element], name: str, default: str = "") -> str:
    found = _child(el, name)
    return (found.text or "").strip() if found is not None and found.text else default


def _parse(path: Path) -> Optional[ET.Element]:
    try:
        return ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        return None


def _coordinates(root: ET.Element) -> str:
    """``groupId:artifactId:version`` with the parent's values filled in where the pom inherits them."""
    parent = _child(root, "parent")
    group = _text(root, "groupId") or _text(parent, "groupId")
    version = _text(root, "version") or _text(parent, "version")
    return f"{group}:{_text(root, 'artifactId')}:{version}"


def _declared_parent(root: ET.Element) -> Optional[str]:
    parent = _child(root, "parent")
    if parent is None:
        return None
    return f"{_text(parent, 'groupId')}:{_text(parent, 'artifactId')}:{_text(parent, 'version')}"


def _properties(root: ET.Element) -> Dict[str, str]:
    """The pom's own ``<properties>``; profile properties are listed separately."""
    props = _child(root, "properties")
    if props is None:
        return {}
    return {_local(c.tag): (c.text or "").strip() for c in props}


def _profile_properties(root: ET.Element) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    profiles = _child(root, "profiles")
    for profile in (profiles if profiles is not None else ()):
        if _local(profile.tag) != "profile":
            continue
        props = _properties(profile)
        if props:
            out[_text(profile, "id") or "?"] = props
    return out


def _dependency_management(root: ET.Element) -> Tuple[List[str], Dict[str, str]]:
    """``(bom imports, managed versions)`` from ``<dependencyManagement>``."""
    boms: List[str] = []
    managed: Dict[str, str] = {}
    deps = _child(_child(root, "dependencyManagement"), "dependencies")
    for dep in (deps if deps is not None else ()):
        if _local(dep.tag) != "dependency":
            continue
        key = f"{_text(dep, 'groupId')}:{_text(dep, 'artifactId')}"
        version = _text(dep, "version")
        if _text(dep, "scope") == "import" and _text(dep, "type") == "pom":
            boms.append(f"{key}:{version}")
        else:
            managed[key] = version
    return boms, managed


def _plugin_versions(root: ET.Element) -> Dict[str, str]:
    """Plugin versions the pom pins, in ``<build><plugins>`` and ``<pluginManagement>``."""
    out: Dict[str, str] = {}
    build = _child(root, "build")
    for holder in (build, _child(build, "pluginManagement")):
        plugins = _child(holder, "plugins")
        for plugin in (plugins if plugins is not None else ()):
            if _local(plugin.tag) != "plugin":
                continue
            group = _text(plugin, "groupId") or "org.apache.maven.plugins"
            version = _text(plugin, "version")
            if version:
                out[f"{group}:{_text(plugin, 'artifactId')}"] = version
    return out


def _modules(root: ET.Element) -> List[str]:
    modules = _child(root, "modules")
    return [(m.text or "").strip() for m in (modules if modules is not None else ()) if _local(m.tag) == "module"]


def _parent_path(pom: Path, root: ET.Element) -> Tuple[Optional[Path], str]:
    """``(path of the parent pom on disk or None, why not)``.

    Maven's rule: ``<relativePath>`` if given (a directory means its pom.xml),
    ``../pom.xml`` when absent, an empty ``<relativePath/>`` means the parent
    comes from a repository only, and the pom found there counts only when
    its coordinates are the declared ones -- a sibling project's pom one
    directory up is not this pom's parent.
    """
    parent = _child(root, "parent")
    if parent is None:
        return None, "no <parent>: this pom is the reactor root"
    declared = _declared_parent(root)
    rel_el = _child(parent, "relativePath")
    rel = (rel_el.text or "").strip() if rel_el is not None else None
    if rel_el is not None and not rel:
        return None, "empty <relativePath/>: the parent is resolved from a repository, not the disk"
    candidate = pom.parent / (rel or "../pom.xml")
    if candidate.is_dir():
        candidate = candidate / "pom.xml"
    if not candidate.is_file():
        return None, f"parent {declared} is not on disk (external)"
    candidate_root = _parse(candidate)
    if candidate_root is None or _coordinates(candidate_root) != declared:
        found = _coordinates(candidate_root) if candidate_root is not None else "unparseable"
        return None, f"parent {declared} is not on disk (external); {candidate.name} at that path is {found}"
    return candidate, ""


def parent_chain(pom: Path) -> Tuple[List[Path], str]:
    """The parents of ``pom`` on disk, nearest first, and why the walk stopped."""
    chain: List[Path] = []
    current, root = pom, _parse(pom)
    if root is None:
        return chain, "does not parse"
    for _ in range(MAX_PARENT_DEPTH):
        parent, why = _parent_path(current, root)
        if parent is None:
            return chain, why
        chain.append(parent)
        current, root = parent, _parse(parent)
        if root is None:
            return chain, f"parent {parent} does not parse"
    return chain, f"parent chain deeper than {MAX_PARENT_DEPTH}; stopped"


def inheritance_depth(pom_path: str) -> int:
    """How many parents ``pom_path`` has on disk: 0 for a reactor root."""
    return len(parent_chain(Path(pom_path))[0])


def build_order(paths: Sequence[str]) -> List[str]:
    """Poms ordered parents before children, so a child is transformed against a rewritten parent.

    Depth in the directory tree is not the order: on AMS the parent BOM lives
    in ``ams-parent-bom/`` beside the modules that inherit from it. Inheritance
    depth is, and the original order breaks ties so the run stays reproducible.
    """
    order = {p: i for i, p in enumerate(paths)}
    return sorted(paths, key=lambda p: (inheritance_depth(p) if Path(p).name == "pom.xml" else 0, order[p]))


# ─── the extractor ────────────────────────────────────────────────────────────

def _rel(path: Path, source_dir: str) -> str:
    try:
        return path.resolve().relative_to(Path(source_dir).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def module_for(file_path: str, source_dir: str) -> str:
    """The module a file belongs to: the nearest directory up the tree that holds a pom.xml."""
    path = Path(file_path)
    start = path.parent if path.suffix or path.is_file() else path
    root = Path(source_dir).resolve()
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "pom.xml").is_file():
            return str(candidate)
        if candidate == root:
            break
    return str(root)


def find_modules(source_dir: str) -> Tuple[str, ...]:
    """Every directory under ``source_dir`` that holds a pom.xml, FORGE output and build dirs pruned."""
    found: List[str] = []
    for dirpath, dirs, files in os.walk(source_dir):
        prune_dirs(dirpath, dirs, also=_SKIP_DIRS)
        dirs.sort()
        if "pom.xml" in files:
            found.append(str(Path(dirpath).resolve()))
    return tuple(sorted(found))


def _current_text(pom: Path, source_dir: str, output_dir: Optional[str]) -> Tuple[Optional[ET.Element], bool]:
    """The parent as this run has it: the output directory's copy when rewritten, else the source's."""
    if output_dir:
        rewritten = Path(output_dir) / _rel(pom, source_dir)
        if rewritten.is_file():
            root = _parse(rewritten)
            if root is not None:
                return root, True
    return _parse(pom), False


def run(source_dir: str, module_dir: Optional[str], output_dir: Optional[str] = None) -> dict:
    """The reactor context for the module at ``module_dir``: the pom, its parent chain, the module graph."""
    module_path = Path(module_dir or source_dir).resolve()
    pom = module_path / "pom.xml"
    ctx: dict = {
        "extractor": NAME,
        "version": 2,
        "module_dir": str(module_path),
        "module_rel": _rel(module_path, source_dir) or ".",
    }
    notes: List[str] = []
    root = _parse(pom) if pom.is_file() else None
    if root is None:
        notes.append(f"{_rel(pom, source_dir)} is missing or does not parse; nothing inherited is known")
        ctx["summary"] = {"module": None, "parent_chain": [], "notes": notes}
        return ctx

    # What names the module -- never its own pre-migration properties or versions.
    ctx["module"] = {
        "path": _rel(pom, source_dir),
        "coordinates": _coordinates(root),
        "packaging": _text(root, "packaging", "jar"),
        "parent": _declared_parent(root),
        "modules": _modules(root),
    }

    chain, why = parent_chain(pom)
    notes.append(why)
    parents: List[dict] = []
    inherited: Dict[str, str] = {}
    for parent_pom in chain:
        parent_root, rewritten = _current_text(parent_pom, source_dir, output_dir)
        if parent_root is None:
            notes.append(f"parent {_rel(parent_pom, source_dir)} does not parse; its properties are unknown")
            break
        p_boms, p_managed = _dependency_management(parent_root)
        props = _properties(parent_root)
        entry = {
            "path": _rel(parent_pom, source_dir),
            "rewritten": rewritten,
            "coordinates": _coordinates(parent_root),
            "packaging": _text(parent_root, "packaging", "jar"),
            "properties": props,
            "profile_properties": _profile_properties(parent_root),
            "bom_imports": p_boms,
            "managed_versions": p_managed,
            "plugin_versions": _plugin_versions(parent_root),
            "modules": _modules(parent_root),
        }
        if not rewritten and output_dir:
            # A parent this run has not rewritten yet (held, or still to come)
            # is shown by NAME only: what it defines, never its pre-migration
            # values. A reviewer given "maven.compiler.source: 1.8" deducted
            # for it twice, whatever the note beside it said; a name a child
            # may reference is all the child's transform needs.
            entry["properties"] = {k: "(pre-migration; the parent's own unit sets this)" for k in props}
            entry["profile_properties"] = {pid: sorted(pp) for pid, pp in entry["profile_properties"].items()}
            entry["bom_imports"] = [b.rsplit(":", 1)[0] + ":(pre-migration)" for b in p_boms]
            entry["managed_versions"] = {k: "(pre-migration)" for k in p_managed}
            entry["plugin_versions"] = {k: "(pre-migration)" for k in entry["plugin_versions"]}
        parents.append(entry)
        # The nearest parent wins, as it does for Maven.
        for key, value in props.items():
            inherited.setdefault(key, value)

    ctx["parents"] = parents
    ctx["inherited_properties"] = inherited
    ctx["reactor"] = {
        "root": parents[-1]["path"] if parents else ctx["module"]["path"],
        "modules": sorted(_rel(Path(m) / "pom.xml", source_dir) for m in find_modules(source_dir)),
    }
    pending = [p["path"] for p in parents if not p["rewritten"]]
    if not parents:
        standing = "This pom inherits nothing: judge it on what it declares."
    elif pending:
        standing = ("A parent marked rewritten: true is this run's output -- what this pom now inherits. "
                    f"Parents not yet rewritten ({', '.join(pending)}) are shown as they were BEFORE this "
                    "pack ran and are migrated by their own units; never deduct for their pre-migration "
                    "values. Judge this file on what it declares, and read the parents for what it inherits.")
    else:
        standing = ("Every parent shown has already been rewritten by this run: it is what this pom now "
                    "inherits. Judge this file on what it declares, and read the parents for what it inherits.")
    ctx["summary"] = {
        "module": ctx["module"]["coordinates"],
        "packaging": ctx["module"]["packaging"],
        "parent_chain": [p["coordinates"] for p in parents],
        "rewritten_parents": [p["path"] for p in parents if p["rewritten"]],
        "inherits": {
            "properties": len(inherited),
            "bom_imports": sum(len(p["bom_imports"]) for p in parents),
            "managed_versions": sum(len(p["managed_versions"]) for p in parents),
        },
        "notes": notes + [standing],
    }
    return ctx


EXTRACTOR = register(Extractor(
    name=NAME,
    run=run,
    selectors={},
    find_modules=find_modules,
    module_for=module_for,
    wants_output_dir=True,
))
