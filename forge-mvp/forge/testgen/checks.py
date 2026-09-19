"""Mechanical checks on a generated test, enforced in code.

Same rule as Rule 1 of the migration: what can be decided by a regex is not
asked of a model. "Is this JUnit 5?", "does it import javax.*?", "does it
contain a single @Test?" are invariants, and a reviewer that scores them is
slower, dearer and less reliable than this file.

They run *before* the review, so a test that fails one of them costs no review
call — it goes straight back to the generator with the failures as feedback.
"""

import re
from typing import List, Mapping, Optional

from forge.testgen.targets import TestTarget, read_surface
from forge.utils.java_checks import find_unmigrated_javax_imports

# JUnit 4, in every form the migration is supposed to have removed.
_JUNIT4_IMPORTS = re.compile(
    r"^\s*import\s+(?:static\s+)?(org\.junit\.(?:Test|Before|After|BeforeClass|AfterClass|Ignore|Assert|"
    r"rules?|runner|runners|experimental)[\w.]*)\s*;",
    re.MULTILINE,
)
_RUNWITH = re.compile(r"@RunWith\s*\(")
_TEST_ANNOTATION = re.compile(r"@(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b")
_DISABLED = re.compile(r"@Disabled\b")
_PLACEHOLDER = re.compile(r"\bTODO\b|\bFIXME\b|fail\s*\(\s*\"(?:not|Not) implemented")

# Non-determinism a unit test has no business containing.
_FORBIDDEN = (
    (re.compile(r"\bThread\.sleep\s*\("), "Thread.sleep in a unit test"),
    (re.compile(r"\bSystem\.exit\s*\("), "System.exit in a unit test"),
    (re.compile(r"\bMath\.random\s*\("), "Math.random makes the test non-deterministic"),
    (re.compile(r"\bnew\s+Random\s*\(\s*\)"), "unseeded Random makes the test non-deterministic"),
    (re.compile(r"\bSystem\.getenv\s*\("), "System.getenv makes the test depend on the environment"),
)


def check_test_source(content: str, target: TestTarget, *, is_primary: bool = True) -> List[str]:
    """Everything mechanically wrong with one generated file. Empty means clean."""
    problems: List[str] = []
    if not content.strip():
        return ["the generated file is empty"]

    surface = read_surface(content)
    if surface is None:
        problems.append("no type declaration found in the generated file")
    else:
        if is_primary and surface.type_name != target.test_class:
            problems.append(f"test class is named {surface.type_name}, expected {target.test_class}")
        if surface.package != target.package:
            problems.append(
                f"package is '{surface.package or '(default)'}', expected '{target.package or '(default)'}'"
            )

    junit4 = _JUNIT4_IMPORTS.findall(content)
    if junit4:
        problems.append("JUnit 4 imports remain: " + ", ".join(sorted(set(junit4))))
    if _RUNWITH.search(content):
        problems.append("@RunWith is JUnit 4; use @ExtendWith")

    javax = find_unmigrated_javax_imports(content)
    if javax:
        problems.append("Jakarta-EE javax.* imports remain: " + ", ".join(sorted(set(javax))))

    if is_primary and not _TEST_ANNOTATION.search(content):
        problems.append("no @Test method — the file tests nothing")
    if _DISABLED.search(content):
        problems.append("@Disabled — a generated test that never runs is not a test")
    if _PLACEHOLDER.search(content):
        problems.append("placeholder left in the test (TODO/FIXME/fail(\"not implemented\"))")

    for pattern, why in _FORBIDDEN:
        if pattern.search(content):
            problems.append(why)
    return problems


def check_output(files: Mapping[str, str], target: TestTarget) -> List[str]:
    """Check every file the generator returned, and that the test class is among them."""
    if not files:
        return ["the generator returned no files"]

    primary = primary_file(files, target)
    if primary is None:
        return [f"no file declares {target.test_class}; got " + ", ".join(sorted(files))]

    problems = list(check_test_source(files[primary], target, is_primary=True))
    for path, content in sorted(files.items()):
        if path == primary:
            continue
        problems += [f"{path}: {p}" for p in check_test_source(content, target, is_primary=False)]
    return problems


def primary_file(files: Mapping[str, str], target: TestTarget) -> Optional[str]:
    """The key holding the test class itself, found by what the content declares."""
    for path, content in sorted(files.items()):
        surface = read_surface(content or "")
        if surface is not None and surface.type_name == target.test_class:
            return path
    return None
