"""Deterministic Java source checks.

Rule 1 of the migration ("zero javax.* allowed in output") is a mechanical
invariant, not a judgement call. Asking an LLM to police it is both slower and
less reliable than a regex, so the pipeline enforces it here and leaves the
qualitative checks to the model.
"""

import re
from typing import List, Optional


# javax.* packages that ship with the JDK and must NOT be rewritten to jakarta.*.
# Anything under javax.* that is not matched by one of these is Jakarta EE and
# is expected to have been migrated.
#
# Note the deliberate precision around javax.xml: javax.xml.bind / .ws / .soap
# are Jakarta EE (jakarta.xml.bind, ...) while the parser and transform APIs
# below are JDK. A blanket "javax.xml" allowance would silently pass unmigrated
# JAXB imports.
_JDK_JAVAX_PREFIXES = (
    "javax.accessibility.",
    "javax.annotation.processing.",
    "javax.crypto.",
    "javax.imageio.",
    "javax.lang.model.",
    "javax.management.",
    "javax.naming.",
    "javax.net.",
    "javax.print.",
    "javax.rmi.",
    "javax.script.",
    "javax.security.auth.",
    "javax.security.cert.",
    "javax.security.sasl.",
    "javax.smartcardio.",
    "javax.sound.",
    "javax.sql.",
    "javax.swing.",
    "javax.tools.",
    "javax.xml.catalog.",
    "javax.xml.datatype.",
    "javax.xml.namespace.",
    "javax.xml.parsers.",
    "javax.xml.stream.",
    "javax.xml.transform.",
    "javax.xml.validation.",
    "javax.xml.xpath.",
    "javax.xml.XMLConstants",
)

_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?(javax\.[\w.]*\*?)\s*;", re.MULTILINE)


def is_jdk_javax(fqcn: str) -> bool:
    """True when a javax.* import is a JDK package that must be left alone."""
    return fqcn.startswith(_JDK_JAVAX_PREFIXES)


def find_unmigrated_javax_imports(source: str) -> List[str]:
    """Return javax.* imports that should have become jakarta.* but did not.

    JDK javax packages (javax.crypto, javax.sql, ...) are excluded — rewriting
    those would break the code.
    """
    return [m for m in _IMPORT_RE.findall(source) if not is_jdk_javax(m)]


# ─── package declaration / scope ─────────────────────────────────────────────

_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)


def declared_package(source: str) -> Optional[str]:
    """The file's declared Java package, or None if it has no package statement.

    None covers both the default package and non-Java files (Struts XML configs
    have no Java package at all).
    """
    match = _PACKAGE_RE.search(source)
    return match.group(1) if match else None


def in_scope(source: str, prefix: str) -> bool:
    """Whether a file belongs to the codebase being migrated.

    An empty prefix disables the check. A file with no package declaration is
    always in scope — absence of evidence is not grounds for skipping it, which
    is what keeps XML configs and default-package classes migratable.

    Matching is on a package boundary, not a raw string prefix: scope 'com.corp'
    covers 'com.corp' and 'com.corp.user' but NOT 'com.corporate.billing'.
    """
    if not prefix:
        return True
    package = declared_package(source)
    if package is None:
        return True
    return package == prefix or package.startswith(prefix + ".")
