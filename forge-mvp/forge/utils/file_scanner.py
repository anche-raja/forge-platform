import os
import re
from pathlib import Path
from typing import List, NamedTuple

from forge.packs.glob import glob_match
from forge.phases import get_phase
from forge.utils.java_checks import declared_package, in_scope
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# Re-exported under the historical name; the definition lives in forge.utils.fs
# so the context extractors can share it without importing this module.
from forge.utils.fs import EXCLUDED_DIRS as _EXCLUDED_DIRS  # noqa: E402


class SkippedFile(NamedTuple):
    path: str
    package: str
    reason: str


class ScanResult(NamedTuple):
    files: List[str]
    skipped: List[SkippedFile]


def _wants_tests(spec) -> bool:
    """Whether this phase deliberately targets test sources.

    Test sources are excluded by default — they are not what a migration is
    judged on. A pack that exists to migrate them says so with its globs.
    """
    return any("src/test" in g for g in getattr(spec, "globs", ()))


def runnable_phases() -> List[str]:
    """Phases and packs that can be run as a bare ``--phase`` today.

    A pack needing a context extractor is excluded until that extractor exists.
    """
    # all_phase_names() rather than the import-time PHASE_NAMES snapshot, so
    # this stays correct when the pack directory is pointed somewhere else.
    from forge.phases import all_phase_names, get_phase

    out = []
    for name in all_phase_names():
        spec = get_phase(name)
        if not getattr(spec, "needs_selectors", False):
            out.append(name)
    return out


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

    # A pack whose applies_to is entirely named selectors ("which files are
    # Struts actions?") cannot be answered by walking the tree — the routing
    # table answers it. Scanning anyway would return zero files and report a
    # clean run over an untouched codebase, which is the worst possible outcome.
    if getattr(spec, "needs_selectors", False):
        raise ValueError(
            f"Pack '{phase}' selects files by {', '.join(spec.selectors)}, which only the "
            f"'{spec.context}' context extractor can resolve, and that is not built yet.\n"
            + (
                f"It also matches {', '.join(spec.globs)} directly — but running only those "
                "would migrate the configuration and skip the classes it refers to, which is "
                "worse than not running at all.\n"
                if spec.globs else ""
            )
            + "Runnable today: " + ", ".join(runnable_phases()) + "."
        )

    source_path = Path(source_dir).resolve()
    results: List[str] = []
    skipped: List[SkippedFile] = []

    for root, dirs, files in os.walk(source_path):
        # Prune build/vendor/VCS dirs in-place so os.walk doesn't descend.
        dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]

        for fname in files:
            abs_path = Path(root) / fname
            rel_path = str(abs_path.relative_to(source_path)).replace("\\", "/")
            if "src/test" in rel_path and not _wants_tests(spec):
                continue

            # Packs match on the path ("**/WEB-INF/web.xml"); a PhaseSpec reads
            # the basename off it. Passing the relative path satisfies both.
            matched = spec.includes(rel_path)
            matchers = [
                pattern for glob, pattern in getattr(spec, "content_matchers", ())
                if glob_match(glob, rel_path)
            ]
            if not matched and not matchers:
                continue

            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                if "DO NOT EDIT" in content[:500]:
                    continue
            except OSError:
                continue

            # A content matcher answers "is this the security config?" from the
            # file's own bytes — no extractor, so the pack stays runnable.
            if not matched and not any(re.search(p, content) for p in matchers):
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
