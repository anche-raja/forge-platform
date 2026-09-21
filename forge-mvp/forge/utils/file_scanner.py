import os
import re
from pathlib import Path
from typing import List, NamedTuple, Sequence, Tuple

from forge.extract import get_context, get_extractor
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
    # Target paths a pack creates rather than edits (Liberty server.xml). They
    # do not exist yet, so they are not in `files`; the transform is given the
    # extracted context instead of a source.
    generated: Tuple[str, ...] = ()


def _excluded_by(rel_path: str, exclude_globs: Sequence[str]) -> str:
    """The first scope glob that claims this path, or ``""``."""
    for pattern in exclude_globs:
        if glob_match(pattern, rel_path):
            return pattern
    return ""


def _wants_tests(spec) -> bool:
    """Whether this phase deliberately targets test sources.

    Test sources are excluded by default — they are not what a migration is
    judged on. A pack that exists to migrate them says so with its globs.
    """
    return any("src/test" in g for g in getattr(spec, "globs", ()))


def runnable_phases() -> List[str]:
    """Phases and packs that can be run as a bare ``--phase`` today.

    A pack needing a context extractor is included once that extractor exists.
    """
    # all_phase_names() rather than the import-time PHASE_NAMES snapshot, so
    # this stays correct when the pack directory is pointed somewhere else.
    from forge.phases import all_phase_names, get_phase

    out = []
    for name in all_phase_names():
        spec = get_phase(name)
        if not getattr(spec, "needs_selectors", False) or _extractor_for(spec) is not None:
            out.append(name)
    return out


def _extractor_for(spec):
    """The registered extractor that resolves every selector the spec uses, or None.

    The loader already rejects a pack whose registered extractor lacks one of
    its selectors, so here "registered" and "resolves everything" coincide.
    """
    if not getattr(spec, "needs_selectors", False):
        return None
    extractor = get_extractor(getattr(spec, "context", "none"))
    if extractor is None or not all(extractor.provides(s) for s in spec.selectors):
        return None
    return extractor


def scan_java_files(
    source_dir: str,
    phase: str = "java21",
    scope_package_prefix: str = "",
    exclude_globs: Sequence[str] = (),
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

    `exclude_globs` answers the other half of that question — "leave this
    directory alone" — against the path rather than the package, using the same
    matcher `file_glob` detect rules use. It is the `scope.exclude_globs` field
    of `forge-profile.yaml`. Excluding can only ever shrink the unit set, so it
    needs no ceiling; an excluded path is reported, never silently dropped.
    """
    spec = get_phase(phase)

    # A pack whose applies_to is entirely named selectors ("which files are
    # Struts actions?") cannot be answered by walking the tree — the routing
    # table answers it. Scanning anyway would return zero files and report a
    # clean run over an untouched codebase, which is the worst possible outcome.
    extractor = _extractor_for(spec)
    if getattr(spec, "needs_selectors", False) and extractor is None:
        raise ValueError(
            f"Pack '{phase}' selects files by {', '.join(spec.selectors)}, which only the "
            f"'{spec.context}' context extractor can resolve, and no such extractor is registered.\n"
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

            excluded_by = _excluded_by(rel_path, exclude_globs)
            if excluded_by:
                # Only report a path the phase would otherwise have taken —
                # every other file in the tree is already none of its business.
                if spec.includes(rel_path):
                    skipped.append(SkippedFile(path=str(abs_path), package="",
                                               reason=f"excluded by scope glob '{excluded_by}'"))
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

    generated: List[str] = []
    if extractor is not None and getattr(spec, "needs_selectors", False):
        # The extractor answers "which files?" per module. The same filters
        # that governed the glob walk apply — a selector is not a way around
        # the scope prefix or the test-source exclusion.
        seen = set(results)
        for module in extractor.find_modules(str(source_path)):
            ctx = get_context(spec.context, str(source_path), module).data
            for name in spec.selectors:
                selection = extractor.selectors[name](ctx, module)
                for path in selection.files:
                    if path in seen:
                        continue
                    rel = str(Path(path).resolve().relative_to(source_path)).replace("\\", "/")
                    if "src/test" in rel and not _wants_tests(spec):
                        continue
                    # A selector is not a way around the scope globs either.
                    excluded_by = _excluded_by(rel, exclude_globs)
                    if excluded_by:
                        skipped.append(SkippedFile(path=path, package="",
                                                   reason=f"excluded by scope glob '{excluded_by}'"))
                        continue
                    try:
                        content = Path(path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if not in_scope(content, scope_package_prefix):
                        skipped.append(SkippedFile(
                            path=path, package=declared_package(content) or "",
                            reason=f"package outside scope prefix '{scope_package_prefix}'",
                        ))
                        continue
                    seen.add(path)
                    results.append(path)
                # A target the pack would create is only "generated" while it
                # is absent. Once it exists it is a file to migrate like any
                # other — the transform must read it, not be told to invent it.
                for target in selection.generated:
                    if Path(target).exists():
                        if target not in seen:
                            seen.add(target)
                            results.append(target)
                    else:
                        generated.append(target)

    if skipped:
        _log.info(
            "Skipped %d file(s) outside scope prefix '%s'",
            len(skipped), scope_package_prefix,
        )

    return ScanResult(files=sorted(results), skipped=sorted(skipped), generated=tuple(sorted(set(generated))))
