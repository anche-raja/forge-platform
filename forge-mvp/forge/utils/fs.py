"""Filesystem rules shared by every walk of a source tree.

Kept dependency-free on purpose: ``forge.extract`` must not import
``forge.utils.file_scanner`` (which pulls in ``forge.phases`` → ``forge.packs`` →
the loader → ``forge.extract`` again), so the one thing both need lives here.

**FORGE never reads its own output as source.** The chat writes a migration into
the repository it is migrating (``<repo>/.migrated``), so every walk of a source
tree passes through :func:`prune_dirs`. A directory is FORGE output when its name
is one FORGE uses for output (:data:`FORGE_OUTPUT_DIR_NAMES`), or when it holds
a file only FORGE writes (:data:`FORGE_OUTPUT_MARKERS`) — the second rule is what
catches an output directory the user named themselves, and the first is what
catches a brand-new one before any run has left a marker in it. Without both, a
second run would scan the first run's output as source: every unit twice,
copied into the chained view, profiled by discovery and built by the project
build.
"""

import os
from typing import Iterable, List

# Where the chat writes a migration by default: inside the repository, beside
# the code it migrates. The CLI's default (`./migrated`, relative to the working
# directory) is left to the marker rule rather than pruned by name, because
# `migrated` is a plausible package directory in somebody's own code.
FORGE_OUTPUT_DIR_NAMES = frozenset({".migrated"})

# Build output, vendored code, VCS and IDE state — never migration input. FORGE's
# own output directory names are in here too, so a walk that only knows this set
# still never descends into one.
EXCLUDED_DIRS = frozenset({
    "target", "build", "out", "bin",
    "node_modules", ".git", ".svn", ".hg",
    ".idea", ".vscode", ".gradle", ".mvn",
    # Agent tooling keeps whole checkouts here (.claude/worktrees/...); read as
    # source they doubled every piece of discovery evidence on AMS.
    ".claude",
    "generated", "generated-sources", "generated-test-sources",
}) | FORGE_OUTPUT_DIR_NAMES

# Files (and one directory) that only FORGE writes, at the root of an output
# directory. Any one of them marks the directory holding it as FORGE output.
# Deliberately FORGE-specific names only: `project-build.json` or a
# `migration-report.md` could plausibly be somebody's own file.
FORGE_OUTPUT_MARKERS = (
    ".forge-writes.json",           # the run manifest (forge/utils/run_manifest.py)
    "manual-review-queue.json",
    "migration-summary.json",
    "migration-review.html",
    ".forge-staging",               # held units (forge/utils/file_writer.py:STAGING_DIR)
)


def is_forge_output_dir(path) -> bool:
    """True when ``path`` is a directory FORGE writes migrations into."""
    path = os.fspath(path)
    if os.path.basename(os.path.normpath(path)) in FORGE_OUTPUT_DIR_NAMES:
        return True
    return any(os.path.exists(os.path.join(path, marker)) for marker in FORGE_OUTPUT_MARKERS)


def keep_dir(dirpath, name: str, also: Iterable[str] = ()) -> bool:
    """Whether a walk of a source tree may descend into ``dirpath/name``."""
    if name in EXCLUDED_DIRS or name in also:
        return False
    return not is_forge_output_dir(os.path.join(os.fspath(dirpath), name))


def prune_dirs(dirpath, dirs: List[str], *, also: Iterable[str] = ()) -> None:
    """Prune an ``os.walk`` ``dirs`` list in place: excluded names and FORGE output.

    Order is preserved; a caller that walks in sorted order sorts afterwards.
    Only the children are judged, never ``dirpath`` itself — so walking an
    output directory on purpose (landing, the merged view's overlay) still
    works.
    """
    also = frozenset(also)
    dirs[:] = [d for d in dirs if keep_dir(dirpath, d, also)]


def is_test_path(rel_path: str) -> bool:
    """Whether a forward-slash relative path lies under a test source root."""
    return "src/test" in rel_path
