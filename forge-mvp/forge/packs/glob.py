"""Glob matching for pack ``applies_to`` and ``acceptance`` scopes.

``fnmatch`` is the obvious choice and the wrong one: its ``*`` matches path
separators, so ``*.java`` would match ``src/main/Foo.java``. Pack globs use the
usual build-tool semantics instead — ``*`` stops at a separator, ``**`` does not.
"""

import re
from functools import lru_cache

_TOKEN = re.compile(r"\*\*/|\*\*|\*|\?|[^*?]+")


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    out = ["(?s:"]
    for tok in _TOKEN.findall(pattern):
        if tok == "**/":
            # Match any number of leading directories, including none, so
            # "**/*.java" matches "Foo.java" as well as "a/b/Foo.java".
            out.append("(?:[^/]*/)*")
        elif tok == "**":
            out.append(".*")
        elif tok == "*":
            out.append("[^/]*")
        elif tok == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(tok))
    out.append(r")\Z")
    return re.compile("".join(out))


def glob_match(pattern: str, path: str) -> bool:
    """Whether `path` (forward slashes, relative) matches `pattern`."""
    return glob_to_regex(pattern).match(path.replace("\\", "/")) is not None
