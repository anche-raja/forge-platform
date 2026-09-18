"""Scope filtering: "is this file ours to migrate?"

scope_package_prefix answers exactly that, and nothing else. It reads a package
declaration; it never rewrites one. Renaming a package in a migration would
break every import, component-scan base package, and reflective lookup in the
codebase.

Both recorded live runs died because this question was put to an LLM inside
guardrails_post, which treated a mismatch as blocking — the second at a passing
score of 80. It is a string comparison, so it now happens in the scanner before
any model is called.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.utils.file_scanner import scan_java_files
from forge.utils.java_checks import declared_package, in_scope
from tests.conftest import write_config

SCOPE = "com.corp"


# ─── the predicate ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("package,expected,why", [
    ("com.corp.user",       True,  "nested under the prefix"),
    ("com.corp",            True,  "exactly the prefix"),
    ("com.corp.a.b.c",      True,  "deeply nested"),
    ("com.vendor.lib",      False, "different root"),
    ("com.corporate.x",     True,  "BOUNDARY: com.corporate is not com.corp"),
    ("comcorp.x",           False, "no separator"),
])
def test_scope_matches_on_package_boundary(package, expected, why):
    source = f"package {package};\npublic class A {{}}\n"
    # com.corporate must NOT be swallowed by a com.corp scope.
    want = False if package == "com.corporate.x" else expected
    assert in_scope(source, SCOPE) is want, why


def test_absent_package_declaration_is_always_in_scope():
    """Absence of evidence is not grounds for skipping: a default-package class
    and a Struts XML config both lack a Java package."""
    assert declared_package("public class A {}") is None
    assert in_scope("public class A {}", SCOPE) is True
    assert in_scope("<struts-config/>", SCOPE) is True


def test_empty_prefix_disables_the_filter():
    assert in_scope("package com.anything.at.all;", "") is True


def test_package_is_read_never_rewritten():
    """The predicate is pure — it must not alter the source it inspects."""
    source = "package com.vendor.lib;\npublic class A {}\n"
    before = source
    in_scope(source, SCOPE)
    assert source == before


# ─── the scanner ─────────────────────────────────────────────────────────────

@pytest.fixture
def project(tmp_path):
    files = {
        "src/main/java/com/corp/user/InScope.java": "package com.corp.user;\npublic class InScope {}\n",
        "src/main/java/com/corp/Exact.java": "package com.corp;\npublic class Exact {}\n",
        "src/main/java/com/corporate/Boundary.java": "package com.corporate;\npublic class Boundary {}\n",
        "src/main/java/com/vendor/lib/Vendored.java": "package com.vendor.lib;\npublic class Vendored {}\n",
        "src/main/java/Default.java": "public class Default {}\n",
        "src/main/resources/struts-config.xml": "<struts-config/>",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _scan(root, phase="java21", scope=""):
    return scan_java_files(str(root.resolve()), phase, scope)


def test_no_prefix_scans_everything(project):
    result = _scan(project)
    assert len(result.files) == 5      # every .java, no XML in java21
    assert result.skipped == []


def test_prefix_keeps_only_our_code(project):
    result = _scan(project, scope=SCOPE)
    names = sorted(Path(f).name for f in result.files)
    # Default.java has no package to judge, so it survives.
    assert names == ["Default.java", "Exact.java", "InScope.java"]


def test_out_of_scope_files_are_reported_not_silently_dropped(project):
    result = _scan(project, scope=SCOPE)
    skipped = {Path(sk.path).name: sk for sk in result.skipped}
    assert set(skipped) == {"Vendored.java", "Boundary.java"}
    assert skipped["Vendored.java"].package == "com.vendor.lib"
    assert "com.corp" in skipped["Vendored.java"].reason


def test_boundary_package_is_skipped_not_matched(project):
    """com.corporate must not be pulled into a com.corp scope by a raw
    string-prefix match."""
    result = _scan(project, scope=SCOPE)
    assert "Boundary.java" in {Path(sk.path).name for sk in result.skipped}
    assert "Boundary.java" not in {Path(f).name for f in result.files}


def test_struts_xml_survives_scope_filtering(project):
    """XML descriptors have no Java package; the struts phase still needs them."""
    result = _scan(project, phase="struts-spring6", scope=SCOPE)
    assert "struts-config.xml" in {Path(f).name for f in result.files}


def test_out_of_scope_file_costs_zero_bedrock_calls(project):
    """The whole point: skip before the pipeline, not inside it."""
    result = _scan(project, scope=SCOPE)
    vendored = project / "src/main/java/com/vendor/lib/Vendored.java"
    assert str(vendored.resolve()) not in result.files


# ─── the prompt must never ask again ─────────────────────────────────────────

def test_preflight_prompt_never_mentions_packages(tmp_path):
    """Regression guard: scope was removed from the model's job. If it comes
    back, both recorded failures come back with it.

    The pre-flight model call is opt-in now, so the guard has to enable it to
    see the prompt at all."""
    config = write_config(tmp_path, scope_package_prefix=SCOPE, preflight_model_check=True)
    src = tmp_path / "A.java"
    src.write_text("package com.vendor.lib;\npublic class A {}\n", encoding="utf-8")

    with (
        patch("forge.guardrails.bedrock_guardrails.boto3") as boto3,
        patch("forge.agents.guardrails_pre.ChatBedrockConverse") as LLM,
    ):
        client = MagicMock()
        client.apply_guardrail.return_value = {"action": "NONE", "assessments": []}
        boto3.client.return_value = client
        reply = MagicMock()
        reply.content = '{"verdict": "PASS", "findings": [], "reason": ""}'
        LLM.return_value.invoke.return_value = reply

        from forge.agents.guardrails_pre import GuardrailsPreAgent
        from tests.conftest import make_state

        GuardrailsPreAgent(config).run(make_state(str(src), tmp_path))

    system, human = LLM.return_value.invoke.call_args[0][0]

    # The prefix must never reach the model at all.
    assert "scope_package_prefix" not in human.content
    assert SCOPE not in human.content

    # None of the numbered checks may be about packages or naming. The prompt
    # does say "Do not comment on package names" — a prohibition, not a request —
    # so assert on what the model is *asked to do*, not on vocabulary.
    checks = [ln for ln in system.content.splitlines() if ln.strip()[:2] in ("1.", "2.", "3.", "4.")]
    assert checks, "expected a numbered checklist in the pre-flight prompt"
    for line in checks:
        assert "package" not in line.lower()
        assert "scope" not in line.lower()
        assert "convention" not in line.lower()

    # And the prohibition itself must stay put. Matched on the two halves rather
    # than one contiguous phrase, because the list it sits in now names secrets
    # and PII too and wraps across lines.
    prohibition = " ".join(system.content.lower().split())
    assert "do not comment on" in prohibition
    assert "package names" in prohibition


# ─── explicit file selection overrides the filter ────────────────────────────

def test_single_file_bypasses_scope_filter(tmp_path):
    """--file names a file explicitly; that intent beats a config default, and
    the scanner (which owns the filter) is never consulted."""
    import sys

    import migrate

    config = write_config(tmp_path, scope_package_prefix=SCOPE)
    out_of_scope = tmp_path / "Vendored.java"
    out_of_scope.write_text("package com.vendor.lib;\npublic class Vendored {}\n", encoding="utf-8")

    with (
        patch("forge.utils.file_scanner.scan_java_files") as scan,
        patch("forge.graph.build_graph") as build,
        patch("forge.state_store.dynamodb.DynamoDBStateManager"),
        patch("forge.config.ForgeConfig", return_value=config),
        patch("forge.utils.telemetry.MetricsEmitter"),
    ):
        final = {
            "current_file": {"file_path": str(out_of_scope), "status": "DONE",
                             "review_score": 90, "retry_count": 0, "guardrail_findings": []},
            "bedrock_calls": 4, "estimated_cost_usd": 0.01,
        }
        build.return_value.invoke.return_value = final

        argv = ["migrate.py", str(tmp_path), "--phase", "java21", "--dry-run",
                "--file", str(out_of_scope), "--output-dir", str(tmp_path / "out")]
        with patch.object(sys, "argv", argv):
            migrate.main()

    scan.assert_not_called()
    build.return_value.invoke.assert_called_once()
