"""Gate 0 — deterministic, local secret scanning.

Runs BEFORE any Bedrock / LLM call so a file containing secrets is blocked
without ever being sent to a model. Pure-Python, no network, no external deps —
this is the only place that can *prevent* (not just detect-after-the-fact)
secret exfiltration to the transform/review models.

Returns redacted findings; never logs the raw secret value.
"""

import math
import re

# ─── High-precision, provider-specific / structural credential patterns ──────────
_PATTERNS = [
    ("aws_access_key_id",      re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret_access_key",  re.compile(r"(?i)aws.{0,20}?(?:secret|key).{0,4}['\"]([A-Za-z0-9/+=]{40})['\"]")),
    ("private_key_block",      re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("github_token",           re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token",            re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("slack_webhook",          re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("google_api_key",         re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt",                    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer_token",           re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
]

# Generic "secret-named field = string literal" (Java / .properties / yaml / xml).
_ASSIGN = re.compile(
    r"""(?ix)
    \b(pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret|
       private[_-]?key|auth[_-]?token|credential)\b
    \s*[=:]\s*
    ['"]([^'"]{6,})['"]
    """
)

# Obvious non-secrets (placeholders / examples / env refs) to ignore in assignments.
_PLACEHOLDER = re.compile(
    r"(?i)^(?:changeme|change_me|password|secret|token|none|null|example|placeholder|"
    r"your[_-].*|replace.*|test|dummy|xxx+|todo|\*+|<.*>|\$\{.*\}|#\{.*\})$"
)


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[:2] + "***"
    return s[:4] + "***" + s[-2:]


def scan_secrets(text: str) -> list:
    """Scan source text for likely secrets.

    Returns a list of {type, line, match} dicts (match is redacted). Empty list
    means clean. Conservative-but-fail-closed: well-known token shapes always
    flag; generic secret-named assignments flag only when the value is a
    high-entropy, space-free literal that isn't an obvious placeholder.
    """
    findings = []
    for i, line in enumerate(text.splitlines(), start=1):
        for name, pat in _PATTERNS:
            m = pat.search(line)
            if m:
                findings.append({"type": name, "line": i, "match": _redact(m.group(0))})

        am = _ASSIGN.search(line)
        if am:
            value = am.group(2).strip()
            if (
                value
                and " " not in value                 # tokens don't contain spaces
                and not _PLACEHOLDER.match(value)
                and not value.startswith(("${", "#{"))  # env / property references
                and _shannon(value) >= 3.0
                and len(value) >= 8
            ):
                findings.append({
                    "type": f"hardcoded_{am.group(1).lower().replace('-', '_')}",
                    "line": i,
                    "match": _redact(value),
                })
    return findings
