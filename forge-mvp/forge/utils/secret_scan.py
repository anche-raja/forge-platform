"""Deterministic, local secret detection.

This is the gate between a source tree and every remote call the pipeline makes.
It runs in ``guardrails_pre`` *before* ``ApplyGuardrail`` and before any model
invocation, so a file carrying a credential is refused while its bytes are still
in this process.

Why it has to be local and complete
-----------------------------------
The pipeline used to ask Claude whether a file contained secrets. That check
works by sending the file to the model — the exact thing an enterprise secret
policy forbids. A model cannot be the control that decides whether the model is
allowed to see something. Neither can the Bedrock guardrail: ``ApplyGuardrail``
is a network call of its own, and its ``sensitive_information_policy`` covers
six entity types with no coverage of key material, connection strings or
generic tokens.

So detection is mechanical, in code, here — the same bargain
``java_checks.find_unmigrated_javax_imports`` makes for Rule 1. Recall is
weighted over precision on purpose: a false positive blocks one file from
migration and names it in the report, which a human can clear with
``secret_scan.allow``; a false negative sends a credential to a third party.

**Findings never quote the matched bytes.** ``guardrail_findings`` is persisted
to DynamoDB, the CloudWatch log group and ``migration-review.html``. Echoing
the secret into a finding would copy it into three more places. A kind and a
line number are enough for a human to go and look.
"""

import re
from collections import Counter
from math import log2
from typing import Iterable, List, Mapping, NamedTuple, Optional, Pattern


class SecretFinding(NamedTuple):
    line: int
    kind: str

    def describe(self) -> str:
        return f"{self.kind} at line {self.line}"


# Retained name: the key-material detectors were the first thing this module did.
KeyFinding = SecretFinding


DEFAULT_ENTROPY = {"enabled": True, "min_length": 20, "min_bits": 4.0}


# ─── 1. key material ─────────────────────────────────────────────────────────

_PEM_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")

_KEY_SPEC_CALL = re.compile(
    r"new\s+(SecretKeySpec|DESKeySpec|DESedeKeySpec|PBEKeySpec|IvParameterSpec)\s*\(([^;]{0,400})"
)

_STRING_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"')

# A JCA algorithm/transformation name is an ordinary argument to these
# constructors — new SecretKeySpec(key, "AES") is not a hardcoded key.
_ALGORITHM_LITERAL = re.compile(
    r"^(?:AES|DES|DESede|TripleDES|Blowfish|RC2|RC4|ARCFOUR|ChaCha20|"
    r"Hmac[A-Za-z0-9]+|AES/[\w/]+|DES/[\w/]+|DESede/[\w/]+|"
    r"PBKDF2With[\w]+|PBEWith[\w]+|[\w]*(?:NoPadding|PKCS5Padding|PKCS7Padding|OAEPPadding))$",
    re.IGNORECASE,
)

_BYTE_ARRAY_LITERAL = re.compile(r"\{\s*(?:\(byte\)\s*)?(?:0x[0-9a-fA-F]{1,2}|-?\d{1,3})\s*,")

_BYTE_ARRAY_ASSIGN = re.compile(
    r"byte\s*\[\s*\]\s*([A-Za-z_$][\w$]*)\s*=\s*(?:new\s+byte\s*\[\s*\]\s*)?\{([^}]{0,4000})\}"
)

_BYTE_ELEMENT = re.compile(r"(?:\(byte\)\s*)?(?:0x[0-9a-fA-F]{1,2}|-?\d{1,3})")

# AES key lengths in raw ASCII bytes — the classic "MySuperSecretKey" literal.
_AES_ASCII_LENGTHS = frozenset({16, 24, 32})


# ─── 2. tokens that identify themselves ──────────────────────────────────────
# Vendor-prefixed credentials. These are unambiguous: the prefix is the vendor's
# own marker, so there is no precision trade-off to make.

_TOKEN_PATTERNS: List[tuple] = [
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("PEM private key block", _PEM_PRIVATE_KEY),
    ("PGP private key block", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
    ("Azure storage key", re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey|SharedAccessSignature)\s*=\s*[A-Za-z0-9+/=]{20,}")),
    ("credentials embedded in a URL", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@\"'<>]{1,64}:[^\s/:@\"'<>]{1,64}@")),
]


# ─── 3. credential-named assignments ─────────────────────────────────────────

# Names that mean "credential" on their own.
_CRED_TOKENS = frozenset({
    "password", "passwd", "pwd", "passphrase", "secret", "secrets",
    "token", "tokens", "credential", "credentials", "apikey", "authorization",
    "keystore", "truststore", "salt", "iv", "hmac",
    "accesskey", "secretkey", "privatekey", "clientsecret", "sharedkey",
})

# "key" is far too common a word to treat as a credential on its own — sortKey,
# cacheKey, primaryKey and rowKey are all ordinary code. It counts only when
# something alongside it says the key is cryptographic or an API credential.
_KEY_TOKEN = frozenset({"key", "keys"})

_KEY_QUALIFIERS = frozenset({
    "api", "access", "secret", "private", "public", "encryption", "encrypt",
    "decrypt", "signing", "sign", "master", "crypto", "cipher", "auth",
    "shared", "consumer", "license", "activation", "aes", "des", "rsa", "hmac",
})

# "seed" is the same trade as "key": a TOTP or wallet seed is a secret, but
# CORE_SEED_LOCATION and seedData name the rows a schema is loaded with. It
# counts only beside a qualifier that makes it cryptographic, or — through the
# key-material rules below — when the value has key shape.
_SEED_TOKEN = frozenset({"seed", "seeds"})

_SEED_QUALIFIERS = _KEY_QUALIFIERS | frozenset({
    "key", "random", "rng", "prng", "secure", "entropy", "otp", "totp", "hotp",
    "mfa", "phrase", "mnemonic", "wallet",
})

# The key-material detectors keep the broad reading of "key" and "seed", because
# they carry a second constraint the name alone does not: the value must have
# key shape.
_KEY_NAME_TOKENS = _CRED_TOKENS | _KEY_TOKEN | _SEED_TOKEN

_TOKEN_SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")

# Java / Groovy: name = "value"
_STRING_ASSIGN = re.compile(r'\b([A-Za-z_$][\w$]*)\s*=\s*"((?:[^"\\\n]|\\.)*)"')

# XML attribute: password="value"  /  <property name="password" value="..."/>
_XML_ATTR = re.compile(r'\b([A-Za-z_][\w.\-]*)\s*=\s*"([^"\n]*)"')
_SPRING_PROPERTY = re.compile(
    r'<property\s+name\s*=\s*"([^"\n]+)"\s+value\s*=\s*"([^"\n]*)"', re.IGNORECASE
)

# XML element: <password>value</password>
_XML_ELEMENT = re.compile(r"<([A-Za-z_][\w.\-]*)>\s*([^<\s][^<]{0,4000}?)\s*</\1>")

# .properties / .yml line: jdbc.password=value  |  password: value
_PROPERTY_LINE = re.compile(
    r"^[ \t]*([A-Za-z_][\w.\-]*)[ \t]*[:=][ \t]*(\S[^\r\n]*?)[ \t]*$", re.MULTILINE
)

_MIN_CRED_VALUE_LEN = 6


# ─── suppression ─────────────────────────────────────────────────────────────
# Everything that is shaped like a secret but is not one. Ordinary code is full
# of these, and a false positive costs a file its migration.

_PLACEHOLDER = re.compile(
    r"^(?:"
    r"|"                                          # empty
    r"\$\{[^}]*\}|\$[A-Za-z_]\w*|"                # ${db.password}, $PASSWORD
    r"#\{[^}]*\}|\{\{[^}]*\}\}|\{[^}]*\}|"        # SpEL, mustache, format slots
    r"@[\w.\-]+@|"                                # @db.password@ maven filtering
    r"%[sdvf]|"
    r"ENC\([^)]*\)|"                              # jasypt — already encrypted
    r"null|none|nil|n/?a|true|false|"
    r"[xX*.\-_0?]+|"                              # xxxx, ****, ----, 0000
    r"change[_\- ]?(?:me|it|this)|changeit|"
    r"to[_\- ]?be[_\- ]?set|(?:set|replace|insert|fill)[_\- ]?(?:me|here|in)?|"
    # Both require a separator: "my-password" is a placeholder, but
    # "MySuperSecretKey" is a real hardcoded key and must not be swallowed here.
    r"your[_\- ][\w\-]{0,24}|my[_\- ][\w\-]{0,24}|"
    r"example|sample|dummy|fake|stub|mock|test|testing|localhost|"
    r"password|passwd|pwd|passphrase|secret|token|apikey|api[_\-]key|"
    r"credential|credentials|user|username|admin|root|guest|default|"
    r"true|false|yes|no|on|off"
    r")$",
    re.IGNORECASE,
)

# A configuration key, a fully-qualified class name, a path, a label.
_PROPERTY_NAME = re.compile(r"^[a-z0-9]+(?:[.\-][a-z0-9]+)+$")
_FQCN = re.compile(r"^(?:[A-Za-z_$][\w$]*\.){2,}[A-Za-z_$][\w$]*$")
_PATHLIKE = re.compile(r"^[./~]|^[\w.\-*]+(?:/[\w.\-*]+)+$|^[A-Za-z]:[\\/]")
# A Spring resource location is a path behind a scheme: classpath*:db/seed/*.sql.
_RESOURCE_PREFIX = re.compile(r"^(?:classpath\*?|file|jar):", re.IGNORECASE)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[:.?!,;]$")
_MIME_OR_HEADER = re.compile(r"^[\w.\-]+/[\w.\-+]+$")

_BASE64ISH = re.compile(r"^[A-Za-z0-9+/_\-]+={0,2}$")
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _tokens(name: str) -> set:
    """Tokens of a camelCase / SNAKE_CASE identifier.

    Tokenised rather than substring-matched so that ``monkeyCount`` does not
    read as a key and ``AES_KEY`` does.
    """
    return {t.lower() for t in _TOKEN_SPLIT.findall(name)}


def _is_credential_named(name: str) -> bool:
    """Whether the identifier itself claims to hold a credential."""
    t = _tokens(name)
    if t & _CRED_TOKENS:
        return True
    if t & _SEED_TOKEN and t & _SEED_QUALIFIERS:
        return True
    return bool(t & _KEY_TOKEN) and bool(t & _KEY_QUALIFIERS)


def _is_key_named(name: str) -> bool:
    """The looser reading, for the key-material rules that also check shape."""
    return bool(_tokens(name) & _KEY_NAME_TOKENS)


def _is_obviously_not_a_secret(value: str) -> bool:
    if not value or any(c.isspace() for c in value):
        return True
    if _PLACEHOLDER.fullmatch(value):
        return True
    if _PROPERTY_NAME.fullmatch(value) or _FQCN.fullmatch(value):
        return True
    if _PATHLIKE.search(_RESOURCE_PREFIX.sub("", value, count=1)) or _UUID.fullmatch(value):
        return True
    if _MIME_OR_HEADER.fullmatch(value) or _SENTENCE_END.search(value):
        return True
    if "://" in value:  # a bare URL; one carrying credentials is caught by rule 2
        return True
    if len(set(value)) <= 2:  # "aaaaaaaa", "ababab"
        return True
    return False


# A value read off a whole line keeps its quotes and whatever closes the line:
# password="${db.password}"/> on its own line of a multi-line XML element.
_QUOTED_VALUE = re.compile(r'^(["\'])(.*)\1\s*(?:/?>|[;,])?$')


def _unquote(value: str) -> str:
    """The value inside its quotes, so the placeholder test sees ``${...}``."""
    m = _QUOTED_VALUE.match(value)
    return m.group(2) if m else value


def _entropy_bits(value: str) -> float:
    counts = Counter(value)
    n = len(value)
    return -sum((c / n) * log2(c / n) for c in counts.values())


def _looks_like_key_literal(value: str) -> bool:
    """Whether a literal has the shape of raw cryptographic key material."""
    if _is_obviously_not_a_secret(value):
        return False
    if len(value) in _AES_ASCII_LENGTHS:
        return True
    if len(value) >= 32 and len(value) % 2 == 0 and _HEX.fullmatch(value):
        return True
    if len(value) >= 24 and _BASE64ISH.fullmatch(value):
        return True
    return False


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


# ─── detectors ───────────────────────────────────────────────────────────────

def find_key_material(source: str) -> List[SecretFinding]:
    """Cryptographic key material only — PEM blocks, key specs, raw keys.

    Kept as its own entry point because it is the narrowest, highest-confidence
    subset and is useful on its own.
    """
    out: List[SecretFinding] = []

    for m in _PEM_PRIVATE_KEY.finditer(source):
        out.append(SecretFinding(_line_of(source, m.start()), "PEM private key block"))

    for m in _KEY_SPEC_CALL.finditer(source):
        constructor, args = m.group(1), m.group(2)
        literals = [lit[1:-1] for lit in _STRING_LITERAL.findall(args)]
        has_key_literal = any(lit and not _ALGORITHM_LITERAL.match(lit) for lit in literals)
        if has_key_literal or _BYTE_ARRAY_LITERAL.search(args):
            out.append(SecretFinding(_line_of(source, m.start()), f"literal passed to {constructor}"))

    for m in _STRING_ASSIGN.finditer(source):
        name, value = m.group(1), m.group(2)
        if _is_key_named(name) and _looks_like_key_literal(value):
            out.append(SecretFinding(_line_of(source, m.start()), f"key-shaped literal assigned to '{name}'"))

    for m in _BYTE_ARRAY_ASSIGN.finditer(source):
        name, body = m.group(1), m.group(2)
        if _is_key_named(name) and len(_BYTE_ELEMENT.findall(body)) >= 8:
            out.append(SecretFinding(_line_of(source, m.start()), f"literal byte array assigned to '{name}'"))

    return _dedupe(out)


def _find_known_tokens(source: str) -> List[SecretFinding]:
    out: List[SecretFinding] = []
    for kind, pattern in _TOKEN_PATTERNS:
        for m in pattern.finditer(source):
            out.append(SecretFinding(_line_of(source, m.start()), kind))
    return out


def _find_credential_assignments(source: str) -> List[SecretFinding]:
    out: List[SecretFinding] = []

    def consider(name: str, value: str, offset: int, shape: str) -> None:
        if not _is_credential_named(name):
            return
        value = _unquote(value)
        if len(value) < _MIN_CRED_VALUE_LEN or _is_obviously_not_a_secret(value):
            return
        out.append(SecretFinding(_line_of(source, offset), f"credential assigned to '{name}' ({shape})"))

    for m in _SPRING_PROPERTY.finditer(source):
        consider(m.group(1), m.group(2), m.start(), "spring property")
    for m in _STRING_ASSIGN.finditer(source):
        consider(m.group(1), m.group(2), m.start(), "assignment")
    for m in _XML_ATTR.finditer(source):
        consider(m.group(1), m.group(2), m.start(), "xml attribute")
    for m in _XML_ELEMENT.finditer(source):
        consider(m.group(1), m.group(2), m.start(), "xml element")
    for m in _PROPERTY_LINE.finditer(source):
        consider(m.group(1), m.group(2), m.start(), "property line")

    return out


def _find_high_entropy(source: str, settings: Mapping) -> List[SecretFinding]:
    """Long, dense, mixed-charset literals with no other explanation.

    Restricted to base64-shaped values: a long run of pure hex is far more often
    a checksum or a test vector than a credential, and flagging those would bury
    the real findings. Hex is still caught when it is credential-named or has
    key shape.
    """
    min_len = int(settings.get("min_length", DEFAULT_ENTROPY["min_length"]))
    min_bits = float(settings.get("min_bits", DEFAULT_ENTROPY["min_bits"]))

    out: List[SecretFinding] = []
    for m in _STRING_LITERAL.finditer(source):
        value = m.group(0)[1:-1]
        if len(value) < min_len or _is_obviously_not_a_secret(value):
            continue
        if not _BASE64ISH.fullmatch(value) or _HEX.fullmatch(value):
            continue
        if not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
            continue
        if _entropy_bits(value) < min_bits:
            continue
        out.append(SecretFinding(_line_of(source, m.start()), "high-entropy literal"))
    return out


def _dedupe(findings: Iterable[SecretFinding]) -> List[SecretFinding]:
    seen = set()
    out = []
    for f in sorted(findings, key=lambda f: (f.line, f.kind)):
        if f in seen:
            continue
        seen.add(f)
        out.append(f)
    return out


def _compile_allow(patterns: Optional[Iterable[str]]) -> List[Pattern]:
    compiled = []
    for raw in patterns or ():
        try:
            compiled.append(re.compile(raw))
        except re.error:
            # A bad allowlist entry must not silently widen the gate, and must
            # not stop the run either — it is simply not honoured.
            continue
    return compiled


def find_secrets(source: str, settings: Optional[Mapping] = None) -> List[SecretFinding]:
    """Every secret this module can find, with line numbers and no secret bytes.

    ``settings`` is the ``secret_scan`` block of agents.yaml. ``allow`` holds
    regexes for literals a team has cleared (a test vector, a sample token); a
    line whose text matches one is dropped from the results.
    """
    settings = settings or {}
    entropy_cfg = {**DEFAULT_ENTROPY, **(settings.get("entropy") or {})}

    findings = list(find_key_material(source))
    findings += _find_known_tokens(source)
    findings += _find_credential_assignments(source)
    if entropy_cfg.get("enabled", True):
        findings += _find_high_entropy(source, entropy_cfg)

    allow = _compile_allow(settings.get("allow"))
    if allow:
        lines = source.splitlines()
        kept = []
        for f in findings:
            text = lines[f.line - 1] if 0 < f.line <= len(lines) else ""
            if any(p.search(text) for p in allow):
                continue
            kept.append(f)
        findings = kept

    return _dedupe(findings)
