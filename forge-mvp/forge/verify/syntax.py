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
from typing import Dict, List, Mapping, Tuple

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


def check_files(files: Mapping[str, str], config, *, run=subprocess.run) -> Tuple[str, List[str]]:
    """``(verdict, errors)`` over a transform's ``{path: content}``.

    FAIL when any checked file does not parse. SKIPPED when a Java file needed
    javac and there is none -- an environment gap, never a verdict on the
    migration. PASS otherwise, including when nothing was checkable.
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
            errors += _xml(name, content)
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
