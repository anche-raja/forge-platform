"""Which migrated classes get a generated test — decided in code, never by a model.

Test generation runs over the *output* tree, because that is what "the new code"
means: a migration writes only the files it changed, and a class it never
touched already has whatever tests it always had.

Every exclusion here is mechanical and every one is reported. A silently
skipped class is indistinguishable from a class nobody thought about, and the
report is the only place a human finds out which is which.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from forge.utils.fs import EXCLUDED_DIRS, is_test_path
from forge.utils.java_checks import declared_package
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# Pipeline artifacts and staged-but-unapproved units live in output_dir too.
_ARTIFACT_DIRS = (".forge-staging",)

KINDS = ("controller", "service", "repository", "entity", "config", "plain")

# A file whose type name ends in one of these already is a test.
_TEST_SUFFIXES = ("Test", "Tests", "TestCase", "IT", "ITCase")

_TYPE_RE = re.compile(
    r"^[ \t]*(?:public\s+|protected\s+|private\s+)?"
    r"((?:final\s+|abstract\s+|sealed\s+|non-sealed\s+|static\s+|strictfp\s+)*)"
    r"(class|interface|enum|record|@interface)\s+(\w+)",
    re.MULTILINE,
)

# One line, `public`/`protected`, something that ends in `name(...)`. Deliberately
# forgiving: this feeds a prompt, not a compiler, and a missed signature costs a
# hint while a wrong one would be an invitation to invent API.
_MEMBER_RE = re.compile(
    r"^[ \t]*(public|protected)\s+((?:static\s+|final\s+|synchronized\s+|native\s+|abstract\s+|default\s+)*)"
    r"([\w.$<>\[\]\s,?]*?)\s*(\w+)\s*\(([^()]*)\)",
    re.MULTILINE,
)

_ANNOTATION_RE = re.compile(r"^[ \t]*@(\w+)", re.MULTILINE)

_CONTROLLER_ANNOTATIONS = {"Controller", "RestController"}
_CONFIG_ANNOTATIONS = {"Configuration", "SpringBootApplication", "EnableWebSecurity", "EnableWebMvc"}
_REPOSITORY_ANNOTATIONS = {"Repository", "Mapper"}
_SERVICE_ANNOTATIONS = {"Service", "Component", "ControllerAdvice", "RestControllerAdvice"}
_ENTITY_ANNOTATIONS = {"Entity", "Embeddable", "MappedSuperclass"}

_REPOSITORY_SUFFIXES = ("Repository", "Dao", "DAO", "Mapper")
_SERVICE_SUFFIXES = ("Service", "ServiceImpl", "Manager", "Handler", "Facade", "Processor")
_ENTITY_SUFFIXES = ("Entity", "Dto", "DTO", "Form", "Request", "Response", "Model", "Vo", "VO")
_CONFIG_SUFFIXES = ("Config", "Configuration", "Properties")
_CONTROLLER_SUFFIXES = ("Controller", "Resource", "Endpoint")


@dataclass(frozen=True)
class JavaSurface:
    """What can be read off a Java file without compiling it."""

    package: str
    type_name: str
    declaration: str          # class | interface | enum | record | @interface
    is_abstract: bool
    annotations: Tuple[str, ...]
    signatures: Tuple[str, ...] = field(default=())

    @property
    def fqcn(self) -> str:
        return f"{self.package}.{self.type_name}" if self.package else self.type_name

    @property
    def has_public_api(self) -> bool:
        return bool(self.signatures) or self.declaration == "record"


class TestTarget(NamedTuple):
    path: str                 # absolute, in the output tree
    rel_path: str             # forward-slash, relative to the output tree
    package: str
    type_name: str
    kind: str
    test_rel_path: str        # src/test/java/<pkg>/<Type>Test.java

    @property
    def test_class(self) -> str:
        return f"{self.type_name}Test"

    @property
    def test_fqcn(self) -> str:
        return f"{self.package}.{self.test_class}" if self.package else self.test_class


def target_from_unit(unit: Mapping) -> TestTarget:
    """The target a graph node is working on, rebuilt from the unit dict.

    The graph threads plain dicts through the checkpointer, so the object is
    reconstructed at each node rather than carried.
    """
    return TestTarget(
        path=unit["file_path"], rel_path=unit["rel_path"], package=unit.get("package", "") or "",
        type_name=unit["type_name"], kind=unit.get("kind", "plain") or "plain",
        test_rel_path=unit["test_rel_path"],
    )


class SkippedTarget(NamedTuple):
    rel_path: str
    reason: str


class TargetScan(NamedTuple):
    targets: List[TestTarget]
    skipped: List[SkippedTarget]


# ─── reading a Java file ──────────────────────────────────────────────────────

def read_surface(content: str) -> Optional[JavaSurface]:
    """Parse the one thing a test needs to know: what type this is and what it exposes."""
    match = _TYPE_RE.search(content)
    if match is None:
        return None
    modifiers, declaration, type_name = match.group(1) or "", match.group(2), match.group(3)
    head = content[: match.start()]
    return JavaSurface(
        package=declared_package(content) or "",
        type_name=type_name,
        declaration=declaration,
        is_abstract="abstract" in modifiers,
        annotations=tuple(sorted(set(_ANNOTATION_RE.findall(head)))),
        signatures=public_signatures(content, type_name),
    )


def public_signatures(content: str, type_name: str) -> Tuple[str, ...]:
    """Public and protected constructors and methods, as one-line signatures.

    The generation prompt's first rule is "never invent API", which only works
    if the model is told what the API is. These strings are that telling — for
    the class under test and for every collaborator that could be resolved.
    """
    out: List[str] = []
    seen = set()
    for visibility, _mods, return_type, name, params in _MEMBER_RE.findall(content):
        rt = " ".join(return_type.split())
        if not rt and name != type_name:
            continue                     # `public Foo(...)` is a constructor; anything else is noise
        if rt in ("class", "interface", "enum", "record", "new", "return"):
            continue
        params = " ".join(params.split())
        sig = f"{visibility} {rt} {name}({params})".replace("  ", " ") if rt else f"{visibility} {name}({params})"
        if sig not in seen:
            seen.add(sig)
            out.append(sig)
    return tuple(out)


def classify(surface: JavaSurface) -> str:
    """The kind of class this is, which decides how it is tested.

    Annotations first — they are what the framework itself goes on — then the
    naming convention, which is all a plain J2EE class offers.
    """
    annotations = set(surface.annotations)
    if annotations & _CONTROLLER_ANNOTATIONS:
        return "controller"
    if annotations & _CONFIG_ANNOTATIONS:
        return "config"
    if annotations & _ENTITY_ANNOTATIONS:
        return "entity"
    if annotations & _REPOSITORY_ANNOTATIONS:
        return "repository"
    if annotations & _SERVICE_ANNOTATIONS:
        return "service"
    name = surface.type_name
    if name.endswith(_CONTROLLER_SUFFIXES):
        return "controller"
    if name.endswith(_CONFIG_SUFFIXES):
        return "config"
    if name.endswith(_REPOSITORY_SUFFIXES):
        return "repository"
    if name.endswith(_SERVICE_SUFFIXES):
        return "service"
    if surface.declaration == "record" or name.endswith(_ENTITY_SUFFIXES):
        return "entity"
    return "plain"


def java_rel_path(package: str, type_name: str, root: str = "src/test/java") -> str:
    """``root/<package as directories>/<TypeName>.java`` — the destination rule."""
    parts = [root] + (package.split(".") if package else []) + [f"{type_name}.java"]
    return "/".join(p for p in parts if p)


def test_rel_path_for(package: str, type_name: str) -> str:
    """Where the test for ``type_name`` belongs. ``type_name`` is the class under test."""
    return java_rel_path(package, f"{type_name}Test")


# ─── the scan ─────────────────────────────────────────────────────────────────

def walk_java(root: Path):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS and d not in _ARTIFACT_DIRS)
        for name in sorted(files):
            if name.endswith(".java"):
                yield Path(dirpath) / name


def existing_test_index(*roots: str) -> Dict[str, str]:
    """Every test class already in the project, by simple name.

    Keyed by name rather than by path because a project's tests do not always
    mirror the package of what they test, and overwriting a human's test is the
    one thing this pipeline must never do.
    """
    index: Dict[str, str] = {}
    for root in roots:
        if not root:
            continue
        base = Path(root)
        if not base.is_dir():
            continue
        for path in walk_java(base):
            rel = str(path.relative_to(base)).replace("\\", "/")
            stem = path.stem
            if is_test_path(rel) or stem.endswith(_TEST_SUFFIXES):
                index.setdefault(stem, rel)
    return index


def scan_test_targets(output_dir: str, source_dir: str, *, only: Optional[Sequence[str]] = None,
                      overwrite: bool = False, kinds: Sequence[str] = ()) -> TargetScan:
    """The classes in ``output_dir`` that should get a generated test.

    ``only`` restricts the scan to specific files — the paths a run just wrote,
    so chaining test generation onto a migration costs nothing for the files
    that migration did not touch.
    """
    out_root = Path(output_dir).resolve()
    if not out_root.is_dir():
        return TargetScan([], [])

    wanted: Optional[set] = None
    if only is not None:
        wanted = set()
        for p in only:
            try:
                wanted.add(Path(p).resolve())
            except OSError:
                continue

    existing = existing_test_index(str(out_root), source_dir)
    targets: List[TestTarget] = []
    skipped: List[SkippedTarget] = []

    for path in walk_java(out_root):
        rel = str(path.relative_to(out_root)).replace("\\", "/")
        if wanted is not None and path.resolve() not in wanted:
            continue
        if is_test_path(rel):
            continue                     # a test tree is not something to write tests for
        if path.stem in ("package-info", "module-info"):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append(SkippedTarget(rel, f"unreadable: {e}"))
            continue

        surface = read_surface(content)
        if surface is None:
            skipped.append(SkippedTarget(rel, "no type declaration found"))
            continue
        if surface.type_name.endswith(_TEST_SUFFIXES):
            continue
        if surface.declaration in ("interface", "@interface"):
            skipped.append(SkippedTarget(rel, f"{surface.declaration} — no behaviour to test"))
            continue
        if surface.is_abstract:
            skipped.append(SkippedTarget(rel, "abstract class — test its concrete subclasses"))
            continue
        if not surface.has_public_api:
            skipped.append(SkippedTarget(rel, "no public or protected members"))
            continue

        kind = classify(surface)
        if kinds and kind not in kinds:
            skipped.append(SkippedTarget(rel, f"kind '{kind}' is not in test_generation.kinds"))
            continue
        if not overwrite and f"{surface.type_name}Test" in existing:
            skipped.append(SkippedTarget(rel, f"a test already exists: {existing[surface.type_name + 'Test']}"))
            continue

        targets.append(TestTarget(
            path=str(path),
            rel_path=rel,
            package=surface.package,
            type_name=surface.type_name,
            kind=kind,
            test_rel_path=test_rel_path_for(surface.package, surface.type_name),
        ))

    _log.info("Test targets: %d to generate, %d skipped", len(targets), len(skipped))
    return TargetScan(targets, skipped)
