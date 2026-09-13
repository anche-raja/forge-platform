"""Build a stack profile of a repository in one walk.

Everything here is evidence a pack's ``detect`` rule can be checked against:
dependency coordinates with versions resolved through ``${properties}`` and
``dependencyManagement`` across the reactor, Java levels from Maven and Gradle,
the distinct imports in the Java sources, the XML elements present, the
notable descriptors, and hits for whatever content patterns the packs ask for.
"""

import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from forge.utils.fs import EXCLUDED_DIRS, is_test_path

_MAX_TEXT_BYTES = 2 * 1024 * 1024
_TEXT_EXT = {".java", ".jsp", ".jspf", ".jspx", ".xml", ".properties", ".ftl", ".tag", ".tagf",
             ".gradle", ".kts", ".yml", ".yaml", ".xhtml", ".html", ".vm"}
_DESCRIPTOR_NAMES = {
    "web.xml", "struts.xml", "struts-config.xml", "validation.xml", "faces-config.xml", "ejb-jar.xml",
    "persistence.xml", "application.xml", "jboss-web.xml", "weblogic.xml", "ibm-web-bnd.xml", "ibm-web-ext.xml",
    "ibm-web-bnd.xmi", "ibm-web-ext.xmi", "server.xml", "build.xml", "ivy.xml", "hibernate.cfg.xml",
    "sqlMapConfig.xml", "context.xml", "beans.xml",
}
_DESCRIPTOR_PATTERNS = (re.compile(r"^struts.*\.xml$"), re.compile(r".*\.hbm\.xml$"), re.compile(r".*-ds\.xml$"),
                        re.compile(r".*SqlMap\.xml$"), re.compile(r".*-validation\.xml$"))
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;", re.MULTILINE)

# An imported BOM manages its whole group family at the BOM's version — Maven's
# rule, applied here so a version-less `spring-core` under an imported
# spring-framework-bom resolves instead of reading as "?". Only families whose
# coverage is unambiguous; spring-boot-dependencies manages hundreds of groups
# and is deliberately absent.
_BOM_FAMILIES = {
    "org.springframework:spring-framework-bom": ("org.springframework",),
    "org.springframework.security:spring-security-bom": ("org.springframework.security",),
    "org.springframework.data:spring-data-bom": ("org.springframework.data",),
    "com.fasterxml.jackson:jackson-bom": ("com.fasterxml.jackson",),
    "org.junit:junit-bom": ("org.junit",),
    "org.apache.logging.log4j:log4j-bom": ("org.apache.logging.log4j",),
    "io.micrometer:micrometer-bom": ("io.micrometer",),
}
_PROP_REF = re.compile(r"\$\{([^}]+)\}")


@dataclass
class Dependency:
    group: str
    artifact: str
    version: str            # resolved; "" when unresolvable
    raw_version: str
    scope: str
    managed: bool           # from <dependencyManagement> rather than <dependencies>
    pom: str                # relative path of the declaring build file

    @property
    def coord(self) -> str:
        return f"{self.group}:{self.artifact}"


@dataclass
class Module:
    path: str               # relative dir
    build_file: str
    system: str             # maven | gradle | ant
    artifact_id: str = ""
    group_id: str = ""
    packaging: str = ""
    parent_artifact: str = ""
    submodules: List[str] = field(default_factory=list)
    properties: Dict[str, str] = field(default_factory=dict)
    dependencies: List[Dependency] = field(default_factory=list)
    java_source: str = ""
    java_target: str = ""


@dataclass
class Profile:
    source_dir: str
    build_system: str
    modules: List[Module]
    dependencies: List[Dependency]
    properties: Dict[str, str]          # effective, reactor-wide (root first, children override)
    gradle_properties: Dict[str, str]
    java_level: Optional[str]
    imports: List[str]                  # distinct, sorted
    xml_elements: List[str]             # "namespace:local", distinct, sorted
    descriptors: List[str]              # relative paths of notable files
    files: List[str]                    # every relative path considered
    counts: Dict[str, int]              # by extension, plus "test_java"
    content_hits: Dict[str, List[str]]  # pattern -> relative paths (capped)
    decisions: Dict[str, str] = field(default_factory=dict)

    def has_dependency(self, group: str, artifact: str) -> List[Dependency]:
        return [d for d in self.dependencies
                if d.group == group and (artifact == "*" or d.artifact == artifact)]

    def to_json(self) -> dict:
        """The persisted form: counts and coordinates, not every path."""
        return {
            "source_dir": self.source_dir,
            "build_system": self.build_system,
            "java_level": self.java_level,
            "modules": [{
                "path": m.path, "system": m.system, "artifact_id": m.artifact_id, "packaging": m.packaging,
                "parent": m.parent_artifact, "java_source": m.java_source, "java_target": m.java_target,
                "dependencies": len(m.dependencies),
            } for m in self.modules],
            "dependencies": sorted({f"{d.coord}:{d.version or d.raw_version or '?'}" for d in self.dependencies}),
            "properties": self.properties,
            "gradle_properties": self.gradle_properties,
            "counts": self.counts,
            "descriptors": self.descriptors,
            "xml_elements": self.xml_elements,
            "import_prefixes": _prefix_counts(self.imports),
            "decisions": self.decisions,
        }


def _prefix_counts(imports: Iterable[str]) -> Dict[str, int]:
    """Top-two-segment prefixes with counts — a readable summary of what the code depends on."""
    counter: Counter = Counter()
    for imp in imports:
        parts = imp.split(".")
        counter[".".join(parts[:3 if parts[0] in ("javax", "jakarta", "org", "com") else 2])] += 1
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


# ─── walk ─────────────────────────────────────────────────────────────────────

def _walk(root: Path) -> Iterable[Path]:
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for f in sorted(files):
            yield Path(dirpath) / f


def _rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def _text(p: Path) -> Optional[str]:
    try:
        if p.stat().st_size > _MAX_TEXT_BYTES:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ─── maven ────────────────────────────────────────────────────────────────────

def _strip(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _child_text(e: ET.Element, name: str) -> str:
    for c in e:
        if _strip(c.tag) == name:
            return (c.text or "").strip()
    return ""


def _children(e: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in e if _strip(c.tag) == name]


def _parse_pom(path: Path, root: Path) -> Optional[Module]:
    try:
        tree = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    rel_dir = _rel(path.parent, root) if path.parent != root else "."
    m = Module(path=rel_dir, build_file=_rel(path, root), system="maven")
    m.artifact_id = _child_text(tree, "artifactId")
    m.group_id = _child_text(tree, "groupId")
    m.packaging = _child_text(tree, "packaging") or "jar"
    for parent in _children(tree, "parent"):
        m.parent_artifact = _child_text(parent, "artifactId")
        if not m.group_id:
            m.group_id = _child_text(parent, "groupId")
    for props in _children(tree, "properties"):
        for p in props:
            m.properties[_strip(p.tag)] = (p.text or "").strip()
    for mods in _children(tree, "modules"):
        m.submodules = [(c.text or "").strip() for c in mods]

    def deps_of(container: ET.Element, managed: bool):
        for deps in _children(container, "dependencies"):
            for d in _children(deps, "dependency"):
                m.dependencies.append(Dependency(
                    group=_child_text(d, "groupId"), artifact=_child_text(d, "artifactId"),
                    version="", raw_version=_child_text(d, "version"), scope=_child_text(d, "scope"),
                    managed=managed, pom=m.build_file,
                ))

    deps_of(tree, managed=False)
    for dm in _children(tree, "dependencyManagement"):
        deps_of(dm, managed=True)
    # Compiler level may also be set on the plugin rather than as a property.
    for build in _children(tree, "build"):
        for plugins in _children(build, "plugins"):
            for plugin in _children(plugins, "plugin"):
                if _child_text(plugin, "artifactId") == "maven-compiler-plugin":
                    for cfg in _children(plugin, "configuration"):
                        for key in ("release", "source", "target"):
                            val = _child_text(cfg, key)
                            if val:
                                m.properties.setdefault(f"maven.compiler.{key}", val)
    m.java_source = m.properties.get("maven.compiler.release") or m.properties.get("maven.compiler.source", "")
    m.java_target = m.properties.get("maven.compiler.release") or m.properties.get("maven.compiler.target", "")
    return m


def _resolve_maven(modules: List[Module]) -> Tuple[List[Dependency], Dict[str, str]]:
    """Resolve ``${prop}`` versions and managed versions through each module's parent chain."""
    by_artifact = {m.artifact_id: m for m in modules if m.artifact_id}

    def chain(m: Module) -> List[Module]:
        out, seen = [m], {m.artifact_id}
        cur = m
        while cur.parent_artifact and cur.parent_artifact in by_artifact and cur.parent_artifact not in seen:
            cur = by_artifact[cur.parent_artifact]
            seen.add(cur.artifact_id)
            out.append(cur)
        return out  # child first

    def effective_props(m: Module) -> Dict[str, str]:
        props: Dict[str, str] = {}
        for mod in reversed(chain(m)):     # root first, children override
            props.update(mod.properties)
        props.setdefault("project.version", "")
        return props

    def resolve(value: str, props: Dict[str, str], depth: int = 0) -> str:
        if depth > 5 or "${" not in value:
            return value
        def sub(match):
            return props.get(match.group(1), match.group(0))
        return resolve(_PROP_REF.sub(sub, value), props, depth + 1)

    def managed_version(m: Module, coord: str) -> Tuple[str, str]:
        """``(version, via)`` — via names the BOM when the version came from an imported one."""
        for mod in chain(m):
            for d in mod.dependencies:
                if d.managed and d.coord == coord and d.raw_version:
                    return resolve(d.raw_version, effective_props(mod)), ""
        group = coord.split(":", 1)[0]
        for mod in chain(m):
            for d in mod.dependencies:
                if not (d.managed and d.scope == "import"):
                    continue
                families = _BOM_FAMILIES.get(d.coord, ())
                if any(group == f or group.startswith(f + ".") for f in families):
                    return resolve(d.raw_version, effective_props(mod)), d.artifact
        return "", ""

    all_deps: List[Dependency] = []
    for m in modules:
        props = effective_props(m)
        for d in m.dependencies:
            via = ""
            if d.raw_version:
                version = resolve(d.raw_version, props)
            else:
                version, via = managed_version(m, d.coord)
            if "${" in version:
                version = ""
            pom = f"{d.pom} via {via}" if via else d.pom
            all_deps.append(Dependency(d.group, d.artifact, version, d.raw_version, d.scope, d.managed, pom))

    reactor_props: Dict[str, str] = {}
    roots = [m for m in modules if not m.parent_artifact or m.parent_artifact not in by_artifact]
    for m in roots + [m for m in modules if m not in roots]:
        for k, v in m.properties.items():
            reactor_props.setdefault(k, v)
    return all_deps, reactor_props


# ─── gradle ───────────────────────────────────────────────────────────────────

_GRADLE_LEVEL = re.compile(
    r"(sourceCompatibility|targetCompatibility)\s*=?\s*['\"]?((?:JavaVersion\.VERSION_)?[\d._]+)['\"]?")
_GRADLE_TOOLCHAIN = re.compile(r"languageVersion\s*(?:=|\.set\()\s*JavaLanguageVersion\.of\((\d+)\)")
_GRADLE_DEP = re.compile(
    r"\b(implementation|api|compile|compileOnly|runtimeOnly|testImplementation|testCompile|providedCompile)"
    r"\s*\(?\s*['\"]([\w.\-]+):([\w.\-]+)(?::([\w.\-]+))?['\"]")


def _parse_gradle(path: Path, root: Path) -> Module:
    rel_dir = _rel(path.parent, root) if path.parent != root else "."
    m = Module(path=rel_dir, build_file=_rel(path, root), system="gradle")
    text = _text(path) or ""
    for key, val in _GRADLE_LEVEL.findall(text):
        m.properties[key] = val
    tc = _GRADLE_TOOLCHAIN.search(text)
    if tc:
        m.properties["toolchain"] = tc.group(1)
    m.java_source = m.properties.get("toolchain") or m.properties.get("sourceCompatibility", "")
    m.java_target = m.properties.get("toolchain") or m.properties.get("targetCompatibility", "")
    for scope, group, artifact, version in _GRADLE_DEP.findall(text):
        m.dependencies.append(Dependency(group, artifact, version or "", version or "", scope, False, m.build_file))
    return m


# ─── java level ───────────────────────────────────────────────────────────────

def normalize_java_level(value: str) -> Optional[int]:
    """``1.8`` → 8, ``17`` → 17, ``JavaVersion.VERSION_11`` → 11, ``21`` → 21."""
    if not value:
        return None
    v = value.strip()
    m = re.search(r"VERSION_(\d+)(?:_(\d+))?", v)
    if m:
        v = f"{m.group(1)}.{m.group(2)}" if m.group(2) else m.group(1)
    m = re.match(r"^(\d+)(?:\.(\d+))?", v)
    if not m:
        return None
    major, minor = int(m.group(1)), m.group(2)
    if major == 1 and minor:
        return int(minor)
    return major


# ─── profile ──────────────────────────────────────────────────────────────────

def build_profile(source_dir: str, *, content_patterns: Sequence[str] = (),
                  decisions: Optional[Dict[str, str]] = None) -> Profile:
    root = Path(source_dir).resolve()
    modules: List[Module] = []
    files: List[str] = []
    counts: Counter = Counter()
    imports: Set[str] = set()
    xml_elements: Set[str] = set()
    descriptors: List[str] = []
    compiled = [(p, re.compile(p, re.MULTILINE)) for p in content_patterns]
    hits: Dict[str, List[str]] = {p: [] for p in content_patterns}

    for path in _walk(root):
        rel = _rel(path, root)
        files.append(rel)
        ext = path.suffix.lower()
        counts[ext or "(none)"] += 1
        if ext == ".java" and is_test_path(rel):
            counts["test_java"] += 1

        name = path.name
        if name == "pom.xml":
            m = _parse_pom(path, root)
            if m:
                modules.append(m)
        elif name in ("build.gradle", "build.gradle.kts"):
            modules.append(_parse_gradle(path, root))
        elif name == "build.xml":
            modules.append(Module(path=_rel(path.parent, root) if path.parent != root else ".",
                                  build_file=rel, system="ant"))

        if name in _DESCRIPTOR_NAMES or any(p.match(name) for p in _DESCRIPTOR_PATTERNS):
            descriptors.append(rel)

        if ext in _TEXT_EXT:
            text = _text(path)
            if text is None:
                continue
            if ext == ".java" and not is_test_path(rel):
                imports.update(_IMPORT.findall(text))
            if ext == ".xml":
                _collect_xml_elements(path, xml_elements)
            for pattern, rx in compiled:
                if len(hits[pattern]) < 25 and rx.search(text):
                    hits[pattern].append(rel)

    maven = [m for m in modules if m.system == "maven"]
    gradle = [m for m in modules if m.system == "gradle"]
    deps, props = _resolve_maven(maven) if maven else ([], {})
    for g in gradle:
        deps.extend(g.dependencies)
    gradle_props: Dict[str, str] = {}
    for g in gradle:
        for k, v in g.properties.items():
            gradle_props.setdefault(k, v)

    if maven:
        system = "maven"
    elif gradle:
        system = "gradle"
    elif any(m.system == "ant" for m in modules):
        system = "ant"
    else:
        system = "none"

    level = None
    for candidate in (props.get("maven.compiler.release"), props.get("maven.compiler.source"),
                      gradle_props.get("toolchain"), gradle_props.get("sourceCompatibility")):
        if candidate and normalize_java_level(candidate) is not None:
            level = str(normalize_java_level(candidate))
            break

    return Profile(
        source_dir=str(root), build_system=system, modules=modules, dependencies=deps,
        properties=props, gradle_properties=gradle_props, java_level=level,
        imports=sorted(imports), xml_elements=sorted(xml_elements), descriptors=sorted(descriptors),
        files=files, counts=dict(sorted(counts.items())), content_hits=hits,
        decisions=dict(decisions or {}),
    )


def _collect_xml_elements(path: Path, into: Set[str]) -> None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return
    n = 0
    for e in root.iter():
        tag = e.tag
        if tag.startswith("{"):
            ns, local = tag[1:].split("}", 1)
        else:
            ns, local = "", tag
        into.add(f"{ns}:{local}")
        n += 1
        if n > 2000:
            break
