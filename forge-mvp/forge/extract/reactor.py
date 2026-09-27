"""The ``reactor`` extractor: what a pom inherits, from the parents on disk.

The build pack migrates one pom at a time, but a pom is not judged alone. A
child module inherits its Java level, its BOM imports and its managed versions
from a parent chain it never restates, and version pinning belongs in the
parent while dependencies belong in the module. Given only the child's bytes,
a transform guesses where a setting lives and a reviewer either deducts for
what it cannot see -- on AMS a sound services pom scored 45, every deduction
reading "parent not provided, cannot verify" -- or has to be told to assume.

So this extractor hands both of them the parent chain: each parent's
coordinates, packaging, ``<properties>``, BOM imports, managed dependency and
plugin versions, plus the module graph of the reactor, all read from the source
tree with no model. The chain is the reactor AS IT WAS BEFORE THE PACK RAN --
each parent is migrated by its own unit in the same run -- and the summary says
so, because judging a child by its parent's pre-migration values would be the
mistake this context exists to prevent.

Deterministic, local, no network. One run per module, cached by
``forge.extract.get_context``.
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def _declares(root: ET.Element) -> Dict[str, int]:
    """How much of each kind the pom declares itself -- what a reviewer can judge here."""
    def count(el: Optional[ET.Element], child: str) -> int:
        return sum(1 for c in (el if el is not None else ()) if _local(c.tag) == child)
    build = _child(root, "build")
    return {
        "properties": len(_properties(root)),
        "dependencies": count(_child(root, "dependencies"), "dependency"),
        "dependency_management": count(_child(_child(root, "dependencyManagement"), "dependencies"), "dependency"),
        "plugins": count(_child(build, "plugins"), "plugin")
                   + count(_child(_child(build, "pluginManagement"), "plugins"), "plugin"),
        "profiles": count(_child(root, "profiles"), "profile"),
        "modules": count(_child(root, "modules"), "module"),
    }


def _modules(root: ET.Element) -> List[str]:
    modules = _child(root, "modules")
    return [(m.text or "").strip() for m in (modules if modules is not None else ()) if _local(m.tag) == "module"]


def _parent_path(pom: Path, root: ET.Element) -> Tuple[Optional[Path], str]:
    """``(path of the parent pom on disk or None, why not)``.

    Maven's rule: ``<relativePath>`` if given (a directory means its pom.xml),
    ``../pom.xml`` when absent, and an empty ``<relativePath/>`` means the
    parent comes from a repository only -- never from the disk.
    """
    parent = _child(root, "parent")
    if parent is None:
        return None, "no <parent>: this pom is the reactor root"
    rel_el = _child(parent, "relativePath")
    rel = (rel_el.text or "").strip() if rel_el is not None else None
    if rel_el is not None and not rel:
        return None, "empty <relativePath/>: the parent is resolved from a repository, not the disk"
    declared = f"{_text(parent, 'groupId')}:{_text(parent, 'artifactId')}:{_text(parent, 'version')}"
    candidate = pom.parent / (rel or "../pom.xml")
    if candidate.is_dir():
        candidate = candidate / "pom.xml"
    if not candidate.is_file():
        return None, f"parent {declared} is not on disk (external)"
    # Maven takes the pom at relativePath only when its coordinates are the
    # declared ones; otherwise the parent comes from a repository. A sibling
    # project's pom one directory up is not this pom's parent.
    candidate_root = _parse(candidate)
    if candidate_root is None or _coordinates(candidate_root) != declared:
        found = _coordinates(candidate_root) if candidate_root is not None else "unparseable"
        return None, f"parent {declared} is not on disk (external); {candidate.name} at that path is {found}"
    return candidate, ""


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


def run(source_dir: str, module_dir: Optional[str]) -> dict:
    """The reactor context for the module at ``module_dir``: the pom, its parent chain, the module graph."""
    module_path = Path(module_dir or source_dir).resolve()
    pom = module_path / "pom.xml"
    ctx: dict = {
        "extractor": NAME,
        "version": 1,
        "module_dir": str(module_path),
        "module_rel": _rel(module_path, source_dir) or ".",
    }
    notes: List[str] = []
    root = _parse(pom) if pom.is_file() else None
    if root is None:
        notes.append(f"{_rel(pom, source_dir)} is missing or does not parse; nothing inherited is known")
        ctx["summary"] = {"module": None, "parent_chain": [], "notes": notes}
        return ctx

    boms, managed = _dependency_management(root)
    ctx["module"] = {
        "path": _rel(pom, source_dir),
        "coordinates": _coordinates(root),
        "packaging": _text(root, "packaging", "jar"),
        "parent": _coordinates_of_parent(root),
        "modules": _modules(root),
        "declares": _declares(root),
        "properties": _properties(root),
        "bom_imports": boms,
        "managed_versions": managed,
        "plugin_versions": _plugin_versions(root),
    }

    parents: List[dict] = []
    inherited: Dict[str, str] = {}
    current, current_root = pom, root
    for _ in range(MAX_PARENT_DEPTH):
        parent_path, why = _parent_path(current, current_root)
        if parent_path is None:
            notes.append(why)
            break
        parent_root = _parse(parent_path)
        if parent_root is None:
            notes.append(f"parent {_rel(parent_path, source_dir)} does not parse; its properties are unknown")
            break
        p_boms, p_managed = _dependency_management(parent_root)
        props = _properties(parent_root)
        parents.append({
            "path": _rel(parent_path, source_dir),
            "coordinates": _coordinates(parent_root),
            "packaging": _text(parent_root, "packaging", "jar"),
            "properties": props,
            "profile_properties": _profile_properties(parent_root),
            "bom_imports": p_boms,
            "managed_versions": p_managed,
            "plugin_versions": _plugin_versions(parent_root),
            "modules": _modules(parent_root),
        })
        # The nearest parent wins, as it does for Maven.
        for key, value in props.items():
            inherited.setdefault(key, value)
        current, current_root = parent_path, parent_root
    else:
        notes.append(f"parent chain deeper than {MAX_PARENT_DEPTH}; stopped")

    ctx["parents"] = parents
    ctx["inherited_properties"] = inherited
    ctx["reactor"] = {
        "root": parents[-1]["path"] if parents else ctx["module"]["path"],
        "modules": sorted(_rel(Path(m) / "pom.xml", source_dir) for m in find_modules(source_dir)),
    }
    ctx["summary"] = {
        "module": ctx["module"]["coordinates"],
        "packaging": ctx["module"]["packaging"],
        "parent_chain": [p["coordinates"] for p in parents],
        "inherits": {
            "properties": len(inherited),
            "bom_imports": sum(len(p["bom_imports"]) for p in parents),
            "managed_versions": sum(len(p["managed_versions"]) for p in parents),
        },
        "notes": notes + [
            "The parent chain is the reactor BEFORE this pack ran; each parent pom is migrated by "
            "its own unit in the same run. Judge this file on what it declares, and read the parents "
            "for what it inherits -- never for their pre-migration values.",
        ],
    }
    return ctx


def _coordinates_of_parent(root: ET.Element) -> Optional[str]:
    parent = _child(root, "parent")
    if parent is None:
        return None
    return f"{_text(parent, 'groupId')}:{_text(parent, 'artifactId')}:{_text(parent, 'version')}"


EXTRACTOR = register(Extractor(
    name=NAME,
    run=run,
    selectors={},
    find_modules=find_modules,
    module_for=module_for,
))
