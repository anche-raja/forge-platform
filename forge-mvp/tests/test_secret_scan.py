"""The secret gate: no credential reaches a model, or Bedrock, or the network.

The enterprise invariant these tests defend is narrow and absolute — a file
carrying a secret is refused while its bytes are still in this process. So the
assertions are about what was *not* called, as much as about the verdict.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.config import ForgeConfig
from forge.state import make_file_status
from forge.utils.secret_scan import find_key_material, find_secrets


def _kinds(source, settings=None):
    return [f.kind for f in find_secrets(source, settings)]


# ─── key material ────────────────────────────────────────────────────────────

def test_ascii_aes_key_literal_is_found():
    src = 'public class C {\n    private static final String AES_KEY = "MySuperSecretKey";\n}\n'
    findings = find_key_material(src)
    assert findings and findings[0].line == 2
    assert "AES_KEY" in findings[0].kind


@pytest.mark.parametrize("decl", [
    'private static final String encryptionKey = "0123456789abcdef";',   # 16 ASCII
    'String SECRET = "aabbccddeeff00112233445566778899";',               # 32 hex
    'static String keyMaterial = "dGhpcyBpcyBhIGJhc2U2NCBrZXk=";',       # base64
    'private String passphrase = "correcthorsebatt";',
    'byte[] AES_KEY = {0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77};',
    'private static final byte[] iv = new byte[]{1, 2, 3, 4, 5, 6, 7, 8};',
])
def test_key_shaped_declarations_are_found(decl):
    assert find_key_material("class C {\n    " + decl + "\n}\n")


def test_literal_into_secretkeyspec_is_found():
    src = 'Key k = new SecretKeySpec("hardcoded-aes-key".getBytes("UTF-8"), "AES");\n'
    assert any("SecretKeySpec" in k for k in _kinds(src))


def test_byte_array_into_secretkeyspec_is_found():
    src = 'Key k = new SecretKeySpec(new byte[]{0x1, 0x2, 0x3, 0x4}, "AES");\n'
    assert any("SecretKeySpec" in k for k in _kinds(src))


def test_pem_private_key_is_found():
    assert "PEM private key block" in _kinds('String pem = "-----BEGIN RSA PRIVATE KEY-----" + body;\n')


# ─── vendor-prefixed tokens ──────────────────────────────────────────────────

# Every fixture is joined from two halves at run time. The detector is handed the
# whole token, but no contiguous credential-shaped literal exists in this file:
# GitHub push protection rejects a commit containing one, and it rejected the
# first version of this test — fair evidence that these fixtures are realistic.
@pytest.mark.parametrize("prefix,rest,kind", [
    ("AKIA", "IOSFODNN7EXAMPLE", "AWS access key id"),
    ("ghp", "_1234567890abcdefghijklmnopqrstuvwx", "GitHub token"),
    ("xoxb", "-123456789012-abcdefghijklmnop", "Slack token"),
    ("sk", "_live_abcdefghijklmnopqrstuvwx", "Stripe key"),
    ("eyJ", "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u", "JSON Web Token"),
    ("mysql://appuser", ":hunter2@db01/orders", "credentials embedded in a URL"),
    ("AccountKey", "=abcdefghijklmnopqrstuvwxyz012345", "Azure storage key"),
])
def test_vendor_prefixed_tokens_are_found(prefix, rest, kind):
    assert kind in _kinds('String t = "' + prefix + rest + '";\n')


# ─── credential-named assignments, across file shapes ────────────────────────

@pytest.mark.parametrize("snippet", [
    'private String dbPassword = "Tr0ub4dor&3";',                                  # java
    '<property name="password" value="Tr0ub4dor&amp;3"/>',                         # spring xml
    '<datasource password="Tr0ub4dor3" user="app"/>',                              # xml attribute
    '<password>Tr0ub4dor3</password>',                                             # xml element
    'jdbc.password=Tr0ub4dor3',                                                    # properties
    'clientSecret: Tr0ub4dor3',                                                    # yaml
])
def test_credentials_are_found_in_every_file_shape(snippet):
    assert find_secrets(snippet + "\n"), snippet


@pytest.mark.parametrize("snippet", [
    'jdbc.password=${db.password}',              # a property reference
    'jdbc.password=@db.password@',               # maven filtering
    '<property name="password" value=""/>',      # empty
    'String password = "changeme";',
    'String password = "CHANGE_ME";',
    'String password = "xxxxxxxx";',
    'String password = "your-password-here";',
    'jdbc.password=ENC(hGe3Xk2mPq8vLs1t)',       # jasypt, already encrypted
    'String passwordLabel = "enter-your-password";',
    'String secretKeyRef = "com.corp.crypto.KeyVault";',
])
def test_placeholders_and_references_are_not_secrets(snippet):
    assert find_secrets(snippet + "\n") == [], snippet


# Issue #14: two real files were BLOCKED with no secret in them.

def test_placeholder_in_a_multiline_xml_element_is_not_a_secret():
    """Liberty's <properties> puts one attribute per line, so the property-line
    reading sees the quotes and the closing ``/>`` around the placeholder."""
    src = (
        '<dataSource id="appDS" jndiName="jdbc/appDS">\n'
        '    <properties URL="${app.datasource.url}"\n'
        '                user="${app.datasource.user}"\n'
        '                password="${app.datasource.password}"/>\n'
        '</dataSource>\n'
    )
    assert find_secrets(src) == []


@pytest.mark.parametrize("snippet", [
    '                password="Tr0ub4dor3"/>',       # the same line with a literal
    '                password="Tr0ub4dor3">',
    'password: "Tr0ub4dor3"',                          # quoted yaml
    "password: 'Tr0ub4dor3'",
])
def test_unquoting_a_line_still_catches_a_literal_password(snippet):
    assert find_secrets(snippet + "\n"), snippet


def test_quoted_yaml_placeholder_is_not_a_secret():
    assert find_secrets('password: "${DB_PASSWORD}"\n') == []


@pytest.mark.parametrize("decl", [
    'private static final String CORE_SEED_LOCATION = "classpath*:db/seed/core/*.sql";',
    # 32 characters, the AES length, so key shape alone used to flag it
    'private static final String ROLLING_SEED_LOCATION = "classpath*:db/seed/rolling/*.sql";',
    'private String seedData = "customers-2024";',
    'String seedScript = "file:/opt/app/seed.sql";',
    'String DB_PASSWORD_FILE = "classpath:secrets/db.properties";',
])
def test_seed_data_and_resource_locations_are_not_secrets(decl):
    assert find_secrets("class C {\n    " + decl + "\n}\n") == [], decl


@pytest.mark.parametrize("decl", [
    'private String totpSeed = "Tr0ub4dor3xK9m";',       # qualified: an OTP shared secret
    'String RANDOM_SEED = "Tr0ub4dor3xK9m";',
    'String walletSeedPhrase = "Tr0ub4dor3xK9m";',
    'static final String SEED = "0123456789abcdef";',   # unqualified, but key-shaped
])
def test_a_seed_that_is_key_material_is_still_found(decl):
    assert find_secrets("class C {\n    " + decl + "\n}\n"), decl


# ─── entropy ─────────────────────────────────────────────────────────────────

def test_high_entropy_literal_is_found_without_a_credential_name():
    src = 'String s = "Xk7pQ2mNvR4tZ9wL3bY6cF8dH1jA5sG0";\n'
    assert "high-entropy literal" in _kinds(src)


def test_entropy_can_be_switched_off():
    src = 'String s = "Xk7pQ2mNvR4tZ9wL3bY6cF8dH1jA5sG0";\n'
    assert _kinds(src, {"entropy": {"enabled": False}}) == []


@pytest.mark.parametrize("snippet", [
    'String id = "550e8400-e29b-41d4-a716-446655440000";',                        # uuid
    'String sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";',  # checksum
    'String cls = "org.springframework.web.servlet.DispatcherServlet";',          # fqcn
    'String path = "/WEB-INF/classes/applicationContext.xml";',                   # path
    'String mime = "application/x-www-form-urlencoded";',                         # mime type
    'String q = "SELECT id, name FROM users WHERE tenant = ?";',                  # sql
])
def test_dense_but_innocent_literals_are_not_flagged(snippet):
    assert find_secrets(snippet + "\n") == [], snippet


# ─── precision guards ────────────────────────────────────────────────────────

@pytest.mark.parametrize("decl", [
    'String CACHE_KEY = "user";',
    'private static final String KEY = "app.secret.key";',
    'String sortKey = "lastName";',
    'int monkeyCount = 3;',
    'String ENCODING = "UTF-8";',
    'private String note = "a passphrase is required here";',
])
def test_ordinary_declarations_are_not_flagged(decl):
    assert find_secrets("class C {\n    " + decl + "\n}\n") == [], decl


def test_algorithm_name_argument_is_not_a_key():
    assert find_secrets('Key k = new SecretKeySpec(keyBytes, "AES");\n') == []
    assert find_secrets('Cipher c = Cipher.getInstance("AES/CBC/PKCS5Padding");\n') == []


def test_camelcase_tokenisation_does_not_match_substrings():
    assert find_secrets('String monkeyBusinessValue = "lastNameField";\n') == []


def test_allowlist_clears_a_known_safe_literal():
    src = 'String testVector = "Xk7pQ2mNvR4tZ9wL3bY6cF8dH1jA5sG0"; // NIST vector\n'
    assert find_secrets(src)
    assert find_secrets(src, {"allow": [r"NIST vector"]}) == []


def test_a_broken_allowlist_regex_does_not_widen_the_gate():
    src = 'String s = "Xk7pQ2mNvR4tZ9wL3bY6cF8dH1jA5sG0";\n'
    assert find_secrets(src, {"allow": ["*unclosed("]})


def test_findings_never_quote_the_secret():
    """guardrail_findings reaches DynamoDB, CloudWatch and the HTML report."""
    secret = "Tr0ub4dor3xK9mVp2Q"
    src = 'class C { static final String dbPassword = "' + secret + '"; }\n'
    findings = find_secrets(src)
    assert findings
    for finding in findings:
        assert secret not in finding.describe()
        assert secret not in finding.kind


# ─── the pipeline node ───────────────────────────────────────────────────────

@pytest.fixture
def config(tmp_path):
    agents_yaml = tmp_path / "agents.yaml"
    agents_yaml.write_text(
        "transform_model: us.anthropic.claude-opus-4-8\n"
        "review_model: us.amazon.nova-pro-v1:0\n"
        "aws_region: us-east-1\n"
        "dynamodb_table: t\n"
        "dynamodb_checkpoint_table: c\n"
        "guardrail_id: test-guardrail-id\n"
        "guardrail_version: '1'\n"
        "pass_threshold: 80\nretry_threshold: 50\nmax_retries: 2\n"
        "scope_package_prefix: ''\n"
        "complexity_block_threshold: 2000\n"
        "preflight_model_check: false\n"
        "secret_scan:\n  enabled: true\n  action: block\n"
    )
    return ForgeConfig(str(agents_yaml))


@pytest.fixture
def secret_file(tmp_path):
    src = tmp_path / "Crypto.java"
    src.write_text(
        "package com.corp;\n"
        "import javax.crypto.spec.SecretKeySpec;\n"
        "public class Crypto {\n"
        '    private static final String AES_KEY = "MySuperSecretKey";\n'
        "}\n"
    )
    return str(src)


@pytest.fixture
def clean_file(tmp_path):
    src = tmp_path / "Clean.java"
    src.write_text(
        "package com.corp;\n"
        "import javax.servlet.http.HttpServlet;\n"
        "public class Clean extends HttpServlet {\n"
        "}\n"
    )
    return str(src)


def _state(java_file):
    return {
        "current_file": make_file_status(java_file, "java21"), "phase": "java21",
        "dry_run": False, "source_dir": str(Path(java_file).parent),
        "output_dir": "./migrated", "target_java_version": "21",
        "target_spring_version": "3", "files_processed": 0, "files_passed": 0,
        "files_retried": 0, "files_manual": 0, "files_blocked": 0,
        "bedrock_calls": 0, "estimated_cost_usd": 0.0, "messages": [],
    }


def _run(config, java_file, guardrail_action="NONE"):
    with (
        patch("forge.guardrails.bedrock_guardrails.boto3") as mock_boto3,
        patch("forge.agents.guardrails_pre.ChatBedrockConverse") as MockLLM,
    ):
        client = MagicMock()
        client.apply_guardrail.return_value = {"action": guardrail_action, "assessments": []}
        mock_boto3.client.return_value = client
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(
            content='{"verdict": "PASS", "findings": [], "reason": "clean"}'
        )
        MockLLM.return_value = llm

        from forge.agents.guardrails_pre import GuardrailsPreAgent
        result = GuardrailsPreAgent(config).run(_state(java_file))
        return result, client, llm


def test_a_secret_is_blocked_with_zero_network_traffic(config, secret_file):
    result, client, llm = _run(config, secret_file)
    fs = result["current_file"]

    assert fs["status"] == "BLOCKED"
    assert fs["guardrail_pre_verdict"] == "SECRET_BLOCKED_LOCALLY"
    # The whole point: nothing left the machine.
    client.apply_guardrail.assert_not_called()
    llm.invoke.assert_not_called()
    assert result.get("bedrock_calls", 0) == 0
    # And the secret is in nothing that gets persisted.
    assert "MySuperSecretKey" not in fs["error"]
    assert all("MySuperSecretKey" not in f for f in fs["guardrail_findings"])


def test_no_model_is_asked_anything_before_the_transform(config, clean_file):
    """A clean file still reaches Bedrock's guardrail, but no model at all."""
    result, client, llm = _run(config, clean_file)

    assert result["current_file"]["status"] == "TRANSFORMING"
    client.apply_guardrail.assert_called_once()
    llm.invoke.assert_not_called()
    assert result.get("bedrock_calls", 0) == 0


def test_risk_is_scored_even_though_the_file_is_blocked(config, secret_file):
    """A blocked file still carries its risk tier into the queue a human reads."""
    fs = _run(config, secret_file)[0]["current_file"]
    assert fs["risk_tier"] != "UNSCORED"


def test_oversized_file_is_blocked_locally(config, tmp_path):
    big = tmp_path / "Big.java"
    big.write_text("package com.corp;\n" + "// filler\n" * 2500, encoding="utf-8")

    result, client, llm = _run(config, str(big))
    fs = result["current_file"]

    assert fs["status"] == "BLOCKED"
    assert fs["guardrail_pre_verdict"] == "TOO_LARGE"
    assert "complexity_block_threshold" in fs["error"]
    client.apply_guardrail.assert_not_called()
    llm.invoke.assert_not_called()


def test_warn_action_records_the_finding_and_continues(config, secret_file):
    warn = config.with_overrides({"secret_scan": {"action": "warn"}})
    result, client, llm = _run(warn, secret_file)
    fs = result["current_file"]

    assert fs["status"] == "TRANSFORMING"
    assert fs["guardrail_findings"]
    client.apply_guardrail.assert_called_once()


def test_disabled_scan_sends_the_file_onward(config, secret_file):
    off = config.with_overrides({"secret_scan": {"enabled": False}})
    fs = _run(off, secret_file)[0]["current_file"]
    assert fs["status"] == "TRANSFORMING"
    assert fs["guardrail_findings"] == []


def test_opt_in_preflight_calls_the_model_and_is_billed(config, clean_file):
    on = config.with_overrides({"preflight_model_check": True})
    result, client, llm = _run(on, clean_file)

    assert result["current_file"]["status"] == "TRANSFORMING"
    llm.invoke.assert_called_once()
    assert result["bedrock_calls"] == 1


def test_the_optional_prompt_never_asks_about_secrets_or_pii(config):
    """The regression this whole change exists to prevent."""
    from forge.agents.guardrails_pre import _SYSTEM

    checks = [ln for ln in _SYSTEM.splitlines() if ln.strip()[:2] in ("1.", "2.", "3.", "4.")]
    assert checks, "expected a numbered checklist in the pre-flight prompt"
    for line in checks:
        lowered = line.lower()
        for banned in ("secret", "credential", "password", "pii", "token", "api key", "package"):
            assert banned not in lowered, f"pre-flight must not ask about {banned}: {line}"
