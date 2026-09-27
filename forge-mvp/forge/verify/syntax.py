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


def pom_inherited_properties(pom_path: str, source_dir: Optional[str] = None,
                             output_dir: Optional[str] = None) -> Optional[Set[str]]:
    """Properties the parents of the pom at ``pom_path`` define, following ``<relativePath>`` on disk.

    A parent this run already rewrote into ``output_dir`` counts with its new
    properties too, so a child is judged against what the parent will be, not
    only what it was. ``None`` when a parent cannot be found on disk -- an
    external parent resolved from a repository -- because its properties are
    unknowable here, and guessing would send correct files back for a retry.
    """
    path = Path(pom_path)
    known: Set[str] = set()
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
            known |= pom_defined_properties(text)
        path = candidate
    return None


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
                inherited_properties: Optional[Iterable[str]] = None) -> Tuple[str, List[str]]:
    """``(verdict, errors)`` over a transform's ``{path: content}``.

    FAIL when any checked file does not parse, or when a ``pom.xml`` references
    a property nobody defines. SKIPPED when a Java file needed javac and there
    is none -- an environment gap, never a verdict on the migration. PASS
    otherwise, including when nothing was checkable.

    The property check runs only when the caller says what the pom inherits
    (``inherited_properties``, from :func:`pom_inherited_properties`): with no
    parent information a child's reference cannot be judged, and ``None`` --
    the default -- skips it rather than guess.
    """
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
