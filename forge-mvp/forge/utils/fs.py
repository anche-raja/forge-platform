"""Filesystem constants shared by the scanner and the context extractors.

Kept dependency-free on purpose: ``forge.extract`` must not import
``forge.utils.file_scanner`` (which pulls in ``forge.phases`` → ``forge.packs`` →
the loader → ``forge.extract`` again), so the one thing both need lives here.
"""

# Build output, vendored code, VCS and IDE state — never migration input.
EXCLUDED_DIRS = frozenset({
    "target", "build", "out", "bin",
    "node_modules", ".git", ".svn", ".hg",
    ".idea", ".vscode", ".gradle", ".mvn",
    "generated", "generated-sources", "generated-test-sources",
})


def is_test_path(rel_path: str) -> bool:
    """Whether a forward-slash relative path lies under a test source root."""
    return "src/test" in rel_path
