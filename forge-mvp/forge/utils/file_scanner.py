import os
from pathlib import Path
from typing import List, NamedTuple

from forge.phases import get_phase
from forge.utils.java_checks import declared_package, in_scope
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

_EXCLUDED_DIRS = {
    "target", "build", "out", "bin",
    "node_modules", ".git", ".svn", ".hg",
    ".idea", ".vscode", ".gradle", ".mvn",
    "generated", "generated-sources", "generated-test-sources",
}


class SkippedFile(NamedTuple):
    path: str
    package: str
    reason: str


class ScanResult(NamedTuple):
    files: List[str]
    skipped: List[SkippedFile]


def scan_java_files(
    source_dir: str,
    phase: str = "java21",
    scope_package_prefix: str = "",
) -> ScanResult:
    """Return the files eligible for migration in this phase, plus what was skipped.

    Which files qualify is phase-specific: java21 takes .java only, while
    struts-spring6 also picks up the Struts XML descriptors it has to convert.
    Config files are matched by exact name so a phase does not sweep up every
    pom.xml in the tree.

    `scope_package_prefix` answers "is this file ours to migrate?" — it filters
    out vendored or third-party sources that happen to live under source_dir.
    It never renames anything: a package declaration is read, never rewritten.
    Empty (the default) disables the filter. Skipping here rather than mid-
    pipeline means an out-of-scope file costs zero Bedrock calls.
    """
    spec = get_phase(phase)
    source_path = Path(source_dir).resolve()
    results: List[str] = []
    skipped: List[SkippedFile] = []

    for root, dirs, files in os.walk(source_path):
        # Prune build/vendor/VCS dirs in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]

        for fname in files:
            if not spec.includes(fname):
                continue
            abs_path = Path(root) / fname
            rel_path = str(abs_path.relative_to(source_path))

            if "src/test" in rel_path.replace("\\", "/"):
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                if "DO NOT EDIT" in content[:500]:
                    continue
            except OSError:
                continue

            # Reuses the content already read above — no extra I/O.
            if not in_scope(content, scope_package_prefix):
                skipped.append(SkippedFile(
                    path=str(abs_path),
                    package=declared_package(content) or "",
                    reason=f"package outside scope prefix '{scope_package_prefix}'",
                ))
                continue

            results.append(str(abs_path))

    if skipped:
        _log.info(
            "Skipped %d file(s) outside scope prefix '%s'",
            len(skipped), scope_package_prefix,
        )

    return ScanResult(files=sorted(results), skipped=sorted(skipped))
