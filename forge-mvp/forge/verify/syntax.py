"""Does the model's output even parse? Asked of javac, not of a model.

A model occasionally damages a file it otherwise migrated correctly -- a stray
``}`` or ``ßßß`` after a brace -- and a reviewer reading for meaning passes it.
On AMS three java8-to-java21 outputs scored PASS and did not compile. This
catches that class of damage before the review is paid for, and the graph
feeds the errors back through the normal retry loop.

Java is parsed by the JDK's own compiler, stopped after the parse stage:
symbols are never resolved, so no classpath is needed and a missing import is
not an error here (that is the project build's job). XML is checked for
well-formedness. Anything else is not checked.

Deterministic, local, no network: about 0.2 s per Java file.
"""

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Set, Tuple

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
TIMEOUT_SECONDS = 60
MAX_ERROR_LINES = 20

# Parse, then stop: -XDshould-stop.* keep javac from entering the attribution
# phase whether or not parsing succeeded, so an unresolvable import is never
# reported. -proc:none keeps annotation processors (Lombok, MapStruct) out.
_JAVAC_FLAGS = ("-proc:none", "-XDshould-stop.ifError=PARSE", "-XDshould-stop.ifNoError=PARSE")

_XML_SUFFIXES = (".xml", ".xmi", ".tld")


def enabled(config) -> bool:
    return bool(config.get("syntax_check", False)) if config is not None else False


def find_javac(config) -> str:
    """``<java_home>/bin/javac`` for the project's JDK, else ``javac`` on PATH, else ``""``."""
    from forge.verify.project_build import resolve_java_home

    home = resolve_java_home(config)
    if home:
        candidate = Path(home) / "bin" / "javac"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("javac") or ""


def _javac(javac: str, name: str, content: str, *, run=subprocess.run) -> List[str]:
    """javac's errors for one file, or [] when it parses."""
    with tempfile.TemporaryDirectory(prefix="forge-syntax-") as tmp:
        # Under its own basename: javac insists a public class lives in a file of its name.
        src = Path(tmp) / Path(name).name
        src.write_text(content, encoding="utf-8")
        proc = run([javac, *_JAVAC_FLAGS, "-d", tmp, str(src)],
                   capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
        if proc.returncode == 0:
            return []
        text = ((proc.stdout or "") + (proc.stderr or "")).replace(tmp + os.sep, "")
        lines = [ln for ln in text.splitlines() if ln.strip() and not re.fullmatch(r"\d+ errors?", ln.strip())]
        return lines or [f"javac exited {proc.returncode}"]


def _xml(name: str, content: str) -> List[str]:
    try:
        ET.fromstring(content.encode("utf-8"))
        return []
    except ET.ParseError as e:
        return [f"{Path(name).name}: {e}"]


# ─── What a pack must remove must be gone: its `no_match` rules, per unit ─────
#
# A pack's acceptance `no_match` rules say what may not survive it (the Struts
# pack: `com\.opensymphony` in any Java file). Acceptance runs over the whole
# tree, after the run, and only when asked; the chat never asks. On AMS the
# Struts pack rewrote an interceptor and its test and left their
# `com.opensymphony.xwork2` imports in place, reviewed PASS, and the build
# failed to compile them. The javax rule is checked in code for the same
# reason; every pack's leftover rule is now checked the same way, on each
# transformed file, before the review.

def _in_scope(name: str, scope: str) -> bool:
    """Whether a transformed file's path fits an acceptance rule's scope glob.

    The model's key may be absolute, or relative to anything; the rule's globs
    are tails (``**/*.java``, ``**/struts*.xml``), so the tail is what is
    matched.
    """
    if not scope:
        return True
    from fnmatch import fnmatch

    path = name.replace("\\", "/")
    tail = scope[3:] if scope.startswith("**/") else scope
    if "/" not in tail:
        return fnmatch(Path(path).name, tail)
    return fnmatch(path, "*/" + tail) or fnmatch(path, tail)


def leftover_errors(files: Mapping[str, str], checks: Iterable, decisions: Optional[Mapping[str, str]] = None) -> List[str]:
    """One error per ``no_match`` acceptance rule a transformed file still matches, first line named."""
    errors: List[str] = []
    for check in checks or ():
        if getattr(check, "kind", "") != "no_match":
            continue
        if decisions is not None and hasattr(check, "applies") and not check.applies(decisions):
            continue
        try:
            pattern = re.compile(str(check.value))
        except re.error:
            continue
        for name, content in files.items():
            if not isinstance(content, str) or not _in_scope(name, getattr(check, "scope", "") or ""):
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if pattern.search(line):
                    errors.append(f"{Path(name).name}:{number}: still contains `{check.value}`, which this pack "
                                  f"must remove (its acceptance forbids it): {line.strip()[:120]}")
                    break
    return errors


# ─── A unit may not take away the constructor its subclasses call ────────────
#
# On AMS spring-to-spring6 rewrote the abstract InstallOrderBaseAction to
# constructor injection -- `protected InstallOrderBaseAction(CustomerService)`,
# no no-arg constructor left -- while the three actions that extend it are
# their own units, each still relying on the implicit `super()`. Its own
# javadoc said "subclasses must forward their own injected CustomerService":
# the model knew, and a unit cannot edit the files around it. Every file
# reviewed PASS and the reactor did not compile.
#
# A prompt cannot see the other units. The tree can, and is read here: if the
# output stops accepting `super()` while the tree still extends the class, the
# transform is sent back to keep the no-arg constructor (field or setter
# injection), which is a change it can make inside its own unit.

_CLASS_DECL = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Z]\w*)")
_EXTENDS = re.compile(r"\bclass\s+([A-Z]\w*)[^{]*?\bextends\s+(?:[\w.]+\.)?([A-Z]\w*)", re.S)
_SUPER_CALL = re.compile(r"\bsuper\s*\(([^)]*)\)")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"')


def _java_code(content: str) -> str:
    """The file with comments and string literals blanked, so neither can spell Java."""
    for pattern in (_BLOCK_COMMENT, _LINE_COMMENT, _STRING_LITERAL):
        content = pattern.sub(lambda m: " " * len(m.group(0)), content)
    return content


_ANNOTATION_TAIL = re.compile(r"@\w+(?:\s*\([^)]*\))?\s*$")


def _is_constructor_position(before: str) -> bool:
    """Whether what precedes the name can precede a constructor: not ``new X(``, not a method."""
    before = _ANNOTATION_TAIL.sub("", before.rstrip()).rstrip()
    return not before or before[-1] in "{};"


def _accepts_no_arg_super(code: str, type_name: str) -> bool:
    """Whether a subclass of ``type_name`` can still write ``super()``.

    True when the class declares no constructor at all (it keeps the implicit
    default one) or declares one taking nothing that is not private.
    """
    declared, no_arg = False, False
    pattern = re.compile(r"(?:(public|protected|private)\s+)?" + re.escape(type_name)
                         + r"\s*\(([^)]*)\)\s*(?:throws\s[^{;]+)?\{")
    for match in pattern.finditer(code):
        if not match.group(1) and not _is_constructor_position(code[:match.start()]):
            continue
        declared = True
        if not match.group(2).strip() and match.group(1) != "private":
            no_arg = True
    return no_arg if declared else True


def _subclass_index(source_dir: str) -> Dict[str, List[str]]:
    """``{superclass name: [paths of the files that extend it]}`` for one source tree."""
    cached = _SUBCLASSES.get(source_dir)
    if cached is not None:
        return cached
    index: Dict[str, List[str]] = {}
    root = Path(source_dir)
    for path in root.rglob("*.java") if root.is_dir() else ():
        if any(part in (".git", "target", "build", "node_modules") for part in path.parts):
            continue
        try:
            code = _java_code(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        for _, parent in _EXTENDS.findall(code):
            index.setdefault(parent, []).append(str(path))
    _SUBCLASSES[source_dir] = index
    return index


_SUBCLASSES: Dict[str, Dict[str, List[str]]] = {}


def forget_subclasses(source_dir: Optional[str] = None) -> None:
    """Drop the cached index (one tree, or all of them)."""
    _SUBCLASSES.pop(source_dir, None) if source_dir else _SUBCLASSES.clear()


def subclass_break_errors(files: Mapping[str, str], source_dir: Optional[str] = None) -> List[str]:
    """One error per class this unit stops accepting ``super()`` from while the tree extends it."""
    if not source_dir or not Path(source_dir).is_dir():
        return []
    errors, index = [], _subclass_index(source_dir)
    own = {Path(name).name for name in files}
    for name, content in files.items():
        if not isinstance(content, str) or not str(name).endswith(".java"):
            continue
        code = _java_code(content)
        for type_name in dict.fromkeys(_CLASS_DECL.findall(code)):
            if not index.get(type_name) or _accepts_no_arg_super(code, type_name):
                continue
            for path in index[type_name]:
                if Path(path).name in own:   # the same unit rewrote the subclass too
                    continue
                subclass = _java_code(Path(path).read_text(encoding="utf-8", errors="replace"))
                if any(args.strip() for args in _SUPER_CALL.findall(subclass)):
                    continue                  # it already forwards arguments
                errors.append(
                    f"{Path(name).name}: {type_name} no longer accepts `super()`, but "
                    f"{Path(path).name} extends it and calls the implicit no-arg constructor. "
                    f"This unit cannot change {Path(path).name}, so keep a no-arg constructor "
                    f"(inject on the field or through a setter instead).")
                break
    return errors


# ─── Maven POMs: an undefined ${property} fails the model build before javac runs ──
#
# On AMS the build pack imported org.junit:junit-bom and pinned junit-jupiter at
# ``${junit-bom.version}``, a property it never defined. Well-formed XML, a
# passing review, and Maven refused the whole reactor at POM parsing. Whether a
# referenced property is defined is mechanical, so it is checked here, with the
# same retry-with-feedback loop a parse error gets.

_PROPERTY_REF = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")
_XML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# Defined by Maven itself, or conventionally supplied on the command line
# (CI-friendly versions), so never "undefined" here.
_BUILTIN_PREFIXES = ("project.", "pom.", "parent.", "maven.", "java.", "user.", "os.", "env.",
                     "settings.", "session.", "file.", "path.", "line.", "sun.")
_BUILTIN_NAMES = frozenset({"basedir", "revision", "sha1", "changelist"})
MAX_PARENT_DEPTH = 8


def is_pom(name: str) -> bool:
    return Path(name).name.lower() == "pom.xml"


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _builtin(prop: str) -> bool:
    return prop in _BUILTIN_NAMES or prop.startswith(_BUILTIN_PREFIXES)


def pom_defined_properties(content: str) -> Set[str]:
    """Every property a pom defines, in its own or any profile's ``<properties>``.

    Empty when it does not parse -- well-formedness is reported separately.
    """
    try:
        root = ET.fromstring(content.encode("utf-8"))
    except ET.ParseError:
        return set()
    return {_local(child.tag) for el in root.iter() if _local(el.tag) == "properties" for child in el}


def pom_property_errors(name: str, content: str, inherited: Iterable[str] = ()) -> List[str]:
    """One error per ``${property}`` this pom references and neither it nor ``inherited`` defines."""
    defined = pom_defined_properties(content) | set(inherited)
    referenced = set(_PROPERTY_REF.findall(_XML_COMMENT.sub("", content)))
    return [
        f"{Path(name).name}: ${{{prop}}} is referenced but defined nowhere in this pom or its parents. "
        "A dependency a BOM manages carries no <version> at all; anything else needs the property "
        "defined in <properties>."
        for prop in sorted(referenced) if prop not in defined and not _builtin(prop)
    ]


def repair_pom(content: str) -> Tuple[str, List[str]]:
    """``(content, what was repaired)``: the mechanical fixes a rubric already prescribes.

    A ``<dependencyManagement>`` entry with no ``<version>`` is removed -- the
    pack's own instruction for it ("delete the entry; the imported BOM
    manages it") -- unless it carries ``<exclusions>``, which a deletion would
    lose. On AMS the transform produced that entry on attempt after attempt
    and the parent BOM ended held, which held every child judged against the
    unrewritten parent. Doing the deletion here costs no retry, and the
    repair is named in the findings and the report.
    """
    if not content.lstrip().startswith("<"):
        return content, []
    # Text-level so the rest of the file (comments, formatting) is untouched.
    pattern = re.compile(r"[ \t]*<dependency>(?:(?!</dependency>).)*?</dependency>[ \t]*\n?", re.S)
    repaired: List[str] = []

    def in_management(pos: int) -> bool:
        opened = content.rfind("<dependencyManagement>", 0, pos)
        closed = content.rfind("</dependencyManagement>", 0, pos)
        return opened != -1 and opened > closed

    def replace(match: "re.Match[str]") -> str:
        block = match.group(0)
        if not in_management(match.start()) or "<version>" in block or "<exclusions>" in block:
            return block
        group = re.search(r"<groupId>\s*([^<]+?)\s*</groupId>", block)
        artifact = re.search(r"<artifactId>\s*([^<]+?)\s*</artifactId>", block)
        repaired.append(f"removed the versionless <dependencyManagement> entry "
                        f"{group.group(1) if group else '?'}:{artifact.group(1) if artifact else '?'}")
        return ""

    fixed = pattern.sub(replace, content)
    return (fixed, repaired) if repaired else (content, [])


def pom_structure_errors(name: str, content: str) -> List[str]:
    """Shapes Maven's model builder refuses, checked without any parent in view.

    A ``<dependencyManagement>`` entry with no ``<version>``: Maven takes the
    first declaration for an artifact, so an entry declared before a BOM
    import manages the artifact with nothing, and every pom that then uses it
    without a version fails at POM parsing. On AMS the build pack, told that a
    BOM-managed dependency carries no version, dropped the version from the
    parent's managed ``junit-jupiter`` entry too, and the whole reactor failed.
    """
    try:
        root = ET.fromstring(content.encode("utf-8"))
    except ET.ParseError:
        return []
    errors: List[str] = []
    for management in (el for el in root.iter() if _local(el.tag) == "dependencyManagement"):
        deps = next((c for c in management if _local(c.tag) == "dependencies"), None)
        for dep in (deps if deps is not None else ()):
            if _local(dep.tag) != "dependency":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in dep}
            if not fields.get("version"):
                errors.append(
                    f"{Path(name).name}: <dependencyManagement> entry {fields.get('groupId', '?')}:"
                    f"{fields.get('artifactId', '?')} has no <version>. Maven takes the first declaration, "
                    "so it manages the artifact with nothing and every module using it fails at POM parsing. "
                    "Delete the entry (the imported BOM manages it) or give it the BOM's version."
                )
    return errors


def pom_management(content: str) -> Tuple[Set[str], Set[str]]:
    """``(managed "group:artifact" keys, imported BOM groupIds)`` from a pom's ``<dependencyManagement>``."""
    try:
        root = ET.fromstring(content.encode("utf-8"))
    except ET.ParseError:
        return set(), set()
    managed: Set[str] = set()
    boms: Set[str] = set()
    for management in (el for el in root.iter() if _local(el.tag) == "dependencyManagement"):
        deps = next((c for c in management if _local(c.tag) == "dependencies"), None)
        for dep in (deps if deps is not None else ()):
            if _local(dep.tag) != "dependency":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in dep}
            key = f"{fields.get('groupId', '')}:{fields.get('artifactId', '')}"
            if fields.get("scope") == "import" and fields.get("type") == "pom":
                boms.add(fields.get("groupId", ""))
            else:
                managed.add(key)
    return managed, boms


def _covered_by_bom(group: str, bom_groups: Iterable[str]) -> bool:
    """A BOM's groupId covers its own group and the groups beneath it (``org.junit`` -> ``org.junit.jupiter``)."""
    return any(group == b or group.startswith(b + ".") for b in bom_groups if b)


def pom_unmanaged_errors(name: str, content: str, inheritance: Mapping[str, Set[str]]) -> List[str]:
    """One error per versionless ``<dependencies>`` entry that nothing in the reactor manages.

    Managed means: this pom's own ``<dependencyManagement>``, a parent's, or an
    imported BOM (own or a parent's) whose group covers the artifact. Anything
    else fails Maven's model build. On AMS the build pack renamed the web pom's
    ``log4j-slf4j-impl`` to ``log4j-slf4j2-impl`` while the parent still managed
    only the old artifact, and the reactor failed at POM parsing.
    """
    try:
        root = ET.fromstring(content.encode("utf-8"))
    except ET.ParseError:
        return []
    own_managed, own_boms = pom_management(content)
    managed = own_managed | set(inheritance.get("managed", ()))
    boms = own_boms | set(inheritance.get("bom_groups", ()))
    errors: List[str] = []
    # ElementTree has no parent pointers: the managed lists are found from
    # their <dependencyManagement> and every other <dependencies> is real.
    managed_lists = {id(d) for m in root.iter() if _local(m.tag) == "dependencyManagement"
                     for d in m if _local(d.tag) == "dependencies"}
    for deps in (el for el in root.iter() if _local(el.tag) == "dependencies" and id(el) not in managed_lists):
        for dep in deps:
            if _local(dep.tag) != "dependency":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in dep}
            if fields.get("version"):
                continue
            group, artifact = fields.get("groupId", ""), fields.get("artifactId", "")
            if f"{group}:{artifact}" in managed or _covered_by_bom(group, boms):
                continue
            errors.append(
                f"{Path(name).name}: {group}:{artifact} has no <version> and nothing manages it -- not this "
                "pom's <dependencyManagement>, not a parent's, and no imported BOM covers its group. Give it a "
                "version here, or manage it in the parent (renaming an artifact needs the parent to manage the "
                "new coordinates too)."
            )
    return errors


def pom_inheritance(pom_path: str, source_dir: Optional[str] = None,
                    output_dir: Optional[str] = None) -> Optional[Dict[str, Set[str]]]:
    """What the parents of the pom at ``pom_path`` define, following ``<relativePath>`` on disk.

    ``{"properties", "managed", "bom_groups"}``: the properties they define,
    the ``group:artifact`` keys they manage, the groupIds of the BOMs they
    import. A parent this run already rewrote into ``output_dir`` counts with
    its new values too, so a child is judged against what the parent will be,
    not only what it was. ``None`` when a parent cannot be found on disk -- an
    external parent resolved from a repository -- because what it defines is
    unknowable here, and guessing would send correct files back for a retry.
    """
    path = Path(pom_path)
    known: Dict[str, Set[str]] = {"properties": set(), "managed": set(), "bom_groups": set()}
    for _ in range(MAX_PARENT_DEPTH):
        try:
            root = ET.fromstring(path.read_bytes())
        except (OSError, ET.ParseError):
            return None
        parent = next((c for c in root if _local(c.tag) == "parent"), None)
        if parent is None:
            return known
        rel = next(((c.text or "") for c in parent if _local(c.tag) == "relativePath"), None)
        if rel is not None and not rel.strip():
            return None          # an empty relativePath means "from the repository only"
        candidate = path.parent / (rel.strip() if rel else "../pom.xml")
        if candidate.is_dir():
            candidate = candidate / "pom.xml"
        if not candidate.is_file() or not _is_declared_parent(candidate, parent):
            return None
        for text in _pom_texts(candidate, source_dir, output_dir):
            known["properties"] |= pom_defined_properties(text)
            managed, boms = pom_management(text)
            known["managed"] |= managed
            known["bom_groups"] |= boms
        path = candidate
    return None


def pom_inherited_properties(pom_path: str, source_dir: Optional[str] = None,
                             output_dir: Optional[str] = None) -> Optional[Set[str]]:
    """The ``properties`` half of :func:`pom_inheritance`, kept for callers that want only that."""
    inheritance = pom_inheritance(pom_path, source_dir, output_dir)
    return None if inheritance is None else inheritance["properties"]


def _is_declared_parent(candidate: Path, parent: ET.Element) -> bool:
    """Maven takes the pom at relativePath only when its coordinates are the declared ones.

    Otherwise the parent comes from a repository: a sibling project's pom one
    directory up is not this pom's parent, and its properties prove nothing.
    """
    try:
        root = ET.fromstring(candidate.read_bytes())
    except (OSError, ET.ParseError):
        return False

    def text(el, name):
        found = next((c for c in el if _local(c.tag) == name), None) if el is not None else None
        return (found.text or "").strip() if found is not None and found.text else ""

    own_parent = next((c for c in root if _local(c.tag) == "parent"), None)
    group = text(root, "groupId") or text(own_parent, "groupId")
    version = text(root, "version") or text(own_parent, "version")
    return (group, text(root, "artifactId"), version) == (text(parent, "groupId"), text(parent, "artifactId"),
                                                            text(parent, "version"))


def _pom_texts(pom: Path, source_dir: Optional[str], output_dir: Optional[str]) -> List[str]:
    """The pom on disk, plus this run's rewritten copy of it when there is one."""
    texts: List[str] = []
    try:
        texts.append(pom.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return texts
    if source_dir and output_dir:
        try:
            rel = pom.resolve().relative_to(Path(source_dir).resolve())
            rewritten = Path(output_dir) / rel
            if rewritten.is_file():
                texts.append(rewritten.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError):
            pass
    return texts


def check_files(files: Mapping[str, str], config, *, run=subprocess.run,
                inherited_properties: Optional[Iterable[str]] = None,
                inheritance: Optional[Mapping[str, Set[str]]] = None) -> Tuple[str, List[str]]:
    """``(verdict, errors)`` over a transform's ``{path: content}``.

    FAIL when any checked file does not parse, or when a ``pom.xml`` references
    a property nobody defines. SKIPPED when a Java file needed javac and there
    is none -- an environment gap, never a verdict on the migration. PASS
    otherwise, including when nothing was checkable.

    The property and unmanaged-dependency checks run only when the caller says
    what the pom inherits (``inheritance``, from :func:`pom_inheritance`; or
    ``inherited_properties`` alone for the property check): with no parent
    information a child's reference cannot be judged, and ``None`` -- the
    default -- skips them rather than guess.
    """
    if inheritance is not None and inherited_properties is None:
        inherited_properties = inheritance.get("properties", set())
    errors: List[str] = []
    skipped = False
    javac = None
    for name, content in files.items():
        if not isinstance(content, str):
            continue
        lower = name.lower()
        if lower.endswith(".java"):
            if javac is None:
                javac = find_javac(config)
            if not javac:
                skipped = True
                continue
            try:
                errors += _javac(javac, name, content, run=run)
            except subprocess.TimeoutExpired:
                errors.append(f"{Path(name).name}: javac did not finish parsing in {TIMEOUT_SECONDS}s")
            except (OSError, subprocess.SubprocessError):
                # javac would not start: the environment, not the migration.
                skipped = True
        elif lower.endswith(_XML_SUFFIXES):
            malformed = _xml(name, content)
            errors += malformed
            if not malformed and is_pom(name):
                errors += pom_structure_errors(name, content)
                if inherited_properties is not None:
                    errors += pom_property_errors(name, content, inherited_properties)
                if inheritance is not None:
                    errors += pom_unmanaged_errors(name, content, inheritance)
    if errors:
        return FAIL, errors[:MAX_ERROR_LINES]
    return (SKIPPED if skipped else PASS), []


# javac rejecting a newer language feature is the toolchain, not damage: a
# Java 21 `switch` pattern parsed by a JDK 17 javac says "preview feature".
_LANGUAGE_LEVEL = re.compile(r"preview feature|not supported in -source|use -source \d+ or higher"
                             r"|--enable-preview|not supported in this release", re.I)
_JAVAC_ERROR = re.compile(r"^(?P<file>.+?\.java):\d+: error: ")


def check_tree(root: str, rels, config, *, run=subprocess.run) -> Tuple[str, Dict[str, List[str]]]:
    """``(verdict, {rel: errors})`` for files already on disk under ``root``.

    One javac over every Java file (an argfile, so any number of them): about
    a quarter of a second for the 297 Java files of the AMS output, where one
    call per file would take a minute. Only files with errors appear in the
    map. SKIPPED when Java files needed javac and there is none, or javac
    failed in a way no file accounts for -- never a verdict on the files.
    """
    root_path = Path(root)
    # A path javac cannot open is "file not found" with no line number, which
    # would read as an unattributable failure and skip the whole check.
    present = [str(r) for r in rels if (root_path / r).is_file()]
    java = [r for r in present if r.lower().endswith(".java")]
    xml = [r for r in present if r.lower().endswith(_XML_SUFFIXES)]
    found: Dict[str, List[str]] = {}
    for rel in xml:
        try:
            ET.fromstring((root_path / rel).read_bytes())
        except ET.ParseError as e:
            found[rel] = [f"{rel}: {e}"]
        except OSError:
            continue

    if not java:
        return (FAIL if found else PASS), found
    javac = find_javac(config)
    if not javac:
        return (FAIL if found else SKIPPED), found
    with tempfile.TemporaryDirectory(prefix="forge-syntax-") as tmp:
        argfile = Path(tmp) / "files.txt"
        argfile.write_text("\n".join('"' + str(r).replace("\\", "\\\\").replace('"', '\\"') + '"' for r in java),
                           encoding="utf-8")
        try:
            proc = run([javac, *_JAVAC_FLAGS, "-Xmaxerrs", "100000", "-d", tmp, f"@{argfile}"],
                       cwd=str(root_path), capture_output=True, text=True, timeout=TIMEOUT_SECONDS * 5)
        except (OSError, subprocess.SubprocessError):
            return (FAIL if found else SKIPPED), found
    java_errors: Dict[str, List[str]] = {}
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
        m = _JAVAC_ERROR.match(line)
        if m:
            java_errors.setdefault(m.group("file").replace("\\", "/"), []).append(line.strip())
    if proc.returncode != 0 and not java_errors:
        return (FAIL if found else SKIPPED), found
    for rel, errs in java_errors.items():
        if all(_LANGUAGE_LEVEL.search(e) for e in errs):
            continue
        found[rel] = errs[:MAX_ERROR_LINES]
    return (FAIL if found else PASS), found
