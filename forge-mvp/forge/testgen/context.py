"""What the generator is told about the world around the class under test.

Deterministic and model-free, like every other context in FORGE. Two facts
matter, and both are answers to "what may this test call?":

* the public signatures of the collaborators the class declares — the prompt's
  first rule is *never invent API*, which is only fair if the API is supplied;
* the test libraries already on the project's test classpath — a test that
  imports a dependency the build does not have is a broken build, so an absent
  library is named as absent rather than left to be guessed.

The block is bounded by ``context_max_chars``; collaborators are dropped from
the end, and what was dropped is stated rather than silently lost.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from forge.testgen.targets import TestTarget, read_surface, walk_java
from forge.utils.fs import is_test_path, keep_dir

_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)
_TYPE_USE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")

MAX_COLLABORATORS = 8

# Artifact ids a generated test may rely on, and the import they license.
KNOWN_TEST_LIBRARIES: Tuple[Tuple[str, str], ...] = (
    ("junit-jupiter", "org.junit.jupiter.*"),
    ("junit-jupiter-api", "org.junit.jupiter.api.*"),
    ("junit-jupiter-params", "@ParameterizedTest"),
    ("mockito-core", "org.mockito.*"),
    ("mockito-junit-jupiter", "@ExtendWith(MockitoExtension.class)"),
    ("assertj-core", "org.assertj.core.api.Assertions"),
    ("hamcrest", "org.hamcrest.*"),
    ("spring-test", "MockMvc, @ExtendWith(SpringExtension.class)"),
    ("spring-boot-starter-test", "JUnit 5 + Mockito + AssertJ + spring-test"),
    ("jakarta.validation-api", "jakarta.validation.Validator"),
    ("hibernate-validator", "a Validator implementation"),
    ("h2", "an in-memory database"),
)

_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")


# ─── the project's test classpath ─────────────────────────────────────────────

def available_test_libraries(source_dir: str, output_dir: Optional[str] = None) -> List[str]:
    """Artifact ids from KNOWN_TEST_LIBRARIES that the build files mention.

    A substring match on the build file is enough: this decides what to *offer*
    the model, and the acceptance of a missing dependency is the human's, not
    this function's.
    """
    text = ""
    for root in (output_dir, source_dir):
        if not root:
            continue
        base = Path(root)
        if not base.is_dir():
            continue
        for name in _BUILD_FILES:
            # One level down is a module -- unless it is excluded or FORGE's own
            # output (a `.migrated` inside the repository holds copies of these).
            nested = [p for p in base.glob(f"*/{name}") if keep_dir(base, p.parent.name)]
            for path in list(base.glob(name)) + nested:
                try:
                    text += path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
    return [artifact for artifact, _ in KNOWN_TEST_LIBRARIES if artifact in text]


# ─── collaborators ────────────────────────────────────────────────────────────

def build_source_index(output_dir: str, source_dir: str) -> Dict[str, str]:
    """Simple type name → file path, output overlaid on source, tests excluded.

    The output copy wins: a collaborator the migration rewrote must be
    advertised with the signatures it has *now*, not the ones it had.
    """
    index: Dict[str, str] = {}
    for root in (source_dir, output_dir):          # output second so it overwrites
        if not root:
            continue
        base = Path(root)
        if not base.is_dir():
            continue
        for path in walk_java(base):
            rel = str(path.relative_to(base)).replace("\\", "/")
            if is_test_path(rel):
                continue
            index[path.stem] = str(path)
    return index


def collaborators_of(content: str, index: Dict[str, str], self_name: str,
                     limit: int = MAX_COLLABORATORS) -> List[str]:
    """Type names this class refers to that resolve to a file in the project.

    Imported names first — an import is a declaration of intent — then
    same-package types used by bare name, which have no import to go on.
    """
    found: List[str] = []
    seen = {self_name}
    for fqcn in _IMPORT_RE.findall(content):
        simple = fqcn.rsplit(".", 1)[-1]
        if simple in seen or simple not in index:
            continue
        if fqcn.startswith(("java.", "javax.", "jakarta.", "org.springframework.")):
            continue          # platform types; the index only holds project files anyway
        seen.add(simple)
        found.append(simple)
    for simple in _TYPE_USE_RE.findall(content):
        if simple in seen or simple not in index:
            continue
        seen.add(simple)
        found.append(simple)
    return found[:limit]


def render_context(target: TestTarget, content: str, index: Dict[str, str], libraries: Sequence[str],
                   max_chars: int) -> str:
    """The context block appended to the generation prompt. Deterministic and bounded."""
    lines: List[str] = ["## Project context"]
    if libraries:
        lines.append("Test libraries already on this project's test classpath: " + ", ".join(libraries) + ".")
        lines.append("Anything not in that list is NOT available — do not import it; name it in \"dependencies\".")
    else:
        lines.append("No test libraries were found in the build files. Assume JUnit 5 and Mockito 5 only, "
                     "and list everything else you need in \"dependencies\".")

    names = collaborators_of(content, index, target.type_name)
    rendered: List[str] = []
    omitted: List[str] = []
    budget = max_chars - sum(len(line) + 1 for line in lines) - 200
    for name in names:
        surface = _surface_of(index.get(name))
        if surface is None or not surface.signatures:
            continue
        block = [f"{surface.fqcn} ({surface.declaration})"]
        block += [f"  {sig}" for sig in surface.signatures[:20]]
        text = "\n".join(block)
        if len(text) + 1 > budget:
            omitted.append(name)
            continue
        budget -= len(text) + 1
        rendered.append(text)

    if rendered:
        lines.append("")
        lines.append("## Collaborator API — call nothing that is not listed here")
        lines.extend(rendered)
    if omitted:
        lines.append(f"(omitted for length: {', '.join(sorted(omitted))})")
    return "\n".join(lines)


def _surface_of(path: Optional[str]):
    if not path:
        return None
    try:
        return read_surface(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
