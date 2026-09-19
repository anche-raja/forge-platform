"""Test generation: the fourth agent, and the gates around it.

The invariants these tests defend are the ones that make a generated test worth
having. A test is only written when it is JUnit 5, in the right package, with a
real @Test, and no credential in it — and those are decided in code, not by the
reviewer. A test that cannot pass those, or that runs and fails, is staged
rather than written: a broken test in src/test/java breaks every build after it.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from forge import service
from forge.phases import get_testgen_spec
from forge.testgen import checks, scan_test_targets, targets, writer
from forge.testgen import settings as testgen_settings
from tests.conftest import generated_test, mocked_testgen, write_config

SERVICE_JAVA = """\
package com.corp.user;

import jakarta.persistence.Entity;

public class UserService {
    private final UserRepository repository;

    public UserService(UserRepository repository) {
        this.repository = repository;
    }

    public String greet(String name) {
        return "hi " + name;
    }
}
"""

CONTROLLER_JAVA = """\
package com.corp.web;

import org.springframework.stereotype.Controller;

@Controller
public class AccountController {
    public String show() {
        return "account";
    }
}
"""


@pytest.fixture
def migrated(tmp_path):
    """An output tree as a migration leaves it: only what was written."""
    out = tmp_path / "out"
    path = out / "src/main/java/com/corp/user/UserService.java"
    path.parent.mkdir(parents=True)
    path.write_text(SERVICE_JAVA, encoding="utf-8")
    source = tmp_path / "proj"
    (source / "src/main/java/com/corp/user").mkdir(parents=True)
    (source / "pom.xml").write_text(
        "<project><dependencies><dependency><artifactId>junit-jupiter</artifactId></dependency>"
        "<dependency><artifactId>mockito-core</artifactId></dependency></dependencies></project>",
        encoding="utf-8")
    return source, out


def _run(tmp_path, migrated, **kw):
    source, out = migrated
    events = []
    cfg = write_config(tmp_path, **kw.pop("config_overrides", {}))
    with mocked_testgen(**kw.pop("mock", {})):
        result = service.generate_tests(str(source), str(out), cfg, on_event=events.append, **kw)
    return result, events


# ─── the spec ─────────────────────────────────────────────────────────────────

def test_rubric_weights_total_100_and_match_the_response_schema():
    """The same contract a pack's rubric has: weights sum to 100, in schema order."""
    spec = get_testgen_spec()
    assert spec.total_weight == 100
    declared = re.findall(r'"(\w+)":\s*<0-(\d+)>', spec.review_prompt)
    # Drop the score line itself; what remains is the checks block, in order.
    declared = [(name, int(value)) for name, value in declared if name != "score"]
    assert declared == [(name, weight) for name, weight in spec.checks]


def test_both_prompts_forbid_junit4_and_invented_api():
    spec = get_testgen_spec()
    assert "org.junit.Test" in spec.generate_prompt and "@RunWith" in spec.generate_prompt
    assert "Never invent API" in spec.generate_prompt
    assert "invented" in spec.review_prompt.lower()


def test_unknown_style_names_what_exists():
    with pytest.raises(ValueError, match="junit5"):
        get_testgen_spec("mockito4")


# ─── choosing the classes ─────────────────────────────────────────────────────

def _scan(tmp_path, files, **kw):
    out = tmp_path / "out"
    for rel, content in files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return scan_test_targets(str(out), str(tmp_path / "src"), **kw)


def test_a_migrated_class_becomes_a_target_with_its_destination(tmp_path, migrated):
    _source, out = migrated
    scan = scan_test_targets(str(out), str(tmp_path / "proj"))
    assert [t.type_name for t in scan.targets] == ["UserService"]
    target = scan.targets[0]
    assert target.kind == "service"
    assert target.test_rel_path == "src/test/java/com/corp/user/UserServiceTest.java"
    assert target.test_fqcn == "com.corp.user.UserServiceTest"


@pytest.mark.parametrize("content,reason", [
    ("package p;\npublic interface Repo { String find(); }\n", "no behaviour to test"),
    ("package p;\npublic abstract class Base { public void go() {} }\n", "abstract class"),
    ("package p;\npublic class Empty {}\n", "no public or protected members"),
])
def test_classes_that_cannot_be_unit_tested_are_skipped_with_a_reason(tmp_path, content, reason):
    scan = _scan(tmp_path, {"src/main/java/p/X.java": content})
    assert scan.targets == []
    assert reason in scan.skipped[0].reason


def test_an_existing_test_is_never_overwritten(tmp_path):
    scan = _scan(tmp_path, {
        "src/main/java/com/corp/user/UserService.java": SERVICE_JAVA,
        "src/test/java/com/corp/user/UserServiceTest.java": "package com.corp.user;\nclass UserServiceTest {}\n",
    })
    assert scan.targets == []
    assert "a test already exists" in scan.skipped[0].reason


def test_overwrite_regenerates_over_an_existing_test(tmp_path):
    scan = _scan(tmp_path, {
        "src/main/java/com/corp/user/UserService.java": SERVICE_JAVA,
        "src/test/java/com/corp/user/UserServiceTest.java": "package com.corp.user;\nclass UserServiceTest {}\n",
    }, overwrite=True)
    assert [t.type_name for t in scan.targets] == ["UserService"]


def test_only_narrows_the_scan_to_the_files_a_run_wrote(tmp_path):
    out = tmp_path / "out"
    kept = out / "src/main/java/com/corp/user/UserService.java"
    kept.parent.mkdir(parents=True)
    kept.write_text(SERVICE_JAVA, encoding="utf-8")
    other = out / "src/main/java/com/corp/web/AccountController.java"
    other.parent.mkdir(parents=True)
    other.write_text(CONTROLLER_JAVA, encoding="utf-8")

    assert len(scan_test_targets(str(out), str(tmp_path)).targets) == 2
    narrowed = scan_test_targets(str(out), str(tmp_path), only=[str(kept)])
    assert [t.type_name for t in narrowed.targets] == ["UserService"]
    assert scan_test_targets(str(out), str(tmp_path), only=[]).targets == [], "a run that wrote nothing tests nothing"


@pytest.mark.parametrize("content,kind", [
    (CONTROLLER_JAVA, "controller"),
    ("package p;\n@Service\npublic class Thing { public void go() {} }\n", "service"),
    ("package p;\n@Entity\npublic class Row { public String getName() { return null; } }\n", "entity"),
    ("package p;\n@Configuration\npublic class AppSetup { public String bean() { return null; } }\n", "config"),
    ("package p;\npublic class OrderRepository { public void save() {} }\n", "repository"),
    ("package p;\npublic class Adder { public int add(int a, int b) { return a + b; } }\n", "plain"),
])
def test_kind_is_decided_from_annotations_then_naming(content, kind):
    surface = targets.read_surface(content)
    assert targets.classify(surface) == kind


def test_collaborator_signatures_are_read_off_the_source():
    surface = targets.read_surface(SERVICE_JAVA)
    assert "public UserService(UserRepository repository)" in surface.signatures
    assert "public String greet(String name)" in surface.signatures
    assert not any("class" in s.split()[1] for s in surface.signatures)


# ─── the mechanical checks ────────────────────────────────────────────────────

TARGET = targets.TestTarget(
    path="/out/src/main/java/com/corp/user/UserService.java",
    rel_path="src/main/java/com/corp/user/UserService.java",
    package="com.corp.user", type_name="UserService", kind="service",
    test_rel_path="src/test/java/com/corp/user/UserServiceTest.java",
)

GOOD_TEST = generated_test("com.corp.user.UserServiceTest")


def test_a_clean_junit5_test_has_no_findings():
    assert checks.check_test_source(GOOD_TEST, TARGET) == []


@pytest.mark.parametrize("mutation,expected", [
    ("import org.junit.Test;\n", "JUnit 4 imports remain"),
    ("@RunWith(MockitoJUnitRunner.class)\n", "@RunWith is JUnit 4"),
    ("import javax.persistence.Entity;\n", "javax.* imports remain"),
    ("Thread.sleep(100);\n", "Thread.sleep"),
    ("double d = Math.random();\n", "Math.random"),
    ("@Disabled\n", "@Disabled"),
    ("// TODO finish this\n", "placeholder"),
])
def test_mechanical_failures_are_caught_in_code(mutation, expected):
    problems = checks.check_test_source(GOOD_TEST.replace("import org.junit.jupiter.api.Test;",
                                                          "import org.junit.jupiter.api.Test;\n" + mutation), TARGET)
    assert any(expected in p for p in problems), problems


def test_a_test_with_no_test_method_is_not_a_test():
    stripped = GOOD_TEST.replace("    @Test\n", "")
    assert any("no @Test method" in p for p in checks.check_test_source(stripped, TARGET))


def test_the_wrong_class_name_or_package_is_a_failure():
    renamed = GOOD_TEST.replace("class UserServiceTest", "class UserServiceTests")
    assert any("expected UserServiceTest" in p for p in checks.check_test_source(renamed, TARGET))
    moved = GOOD_TEST.replace("package com.corp.user;", "package com.elsewhere;")
    assert any("expected 'com.corp.user'" in p for p in checks.check_test_source(moved, TARGET))


def test_the_primary_file_is_found_by_what_it_declares_not_by_its_key():
    files = {"whatever.java": GOOD_TEST}
    assert checks.primary_file(files, TARGET) == "whatever.java"
    assert checks.check_output(files, TARGET) == []
    assert "no file declares UserServiceTest" in checks.check_output({"a.java": "class Other {}"}, TARGET)[0]


# ─── where a generated test lands ─────────────────────────────────────────────

def test_the_destination_comes_from_the_content_not_the_model_key(tmp_path):
    written = writer.write_test_files({"../../../etc/evil.java": GOOD_TEST}, TARGET, str(tmp_path))
    assert written == [str((tmp_path / "src/test/java/com/corp/user/UserServiceTest.java").resolve())]
    assert Path(written[0]).read_text(encoding="utf-8") == GOOD_TEST


def test_a_staged_test_goes_under_forge_staging(tmp_path):
    staged = writer.stage_test_files({"x.java": GOOD_TEST}, TARGET, str(tmp_path))
    assert ".forge-staging" in staged[0]
    assert not (tmp_path / "src/test/java/com/corp/user/UserServiceTest.java").exists()


# ─── the graph ────────────────────────────────────────────────────────────────

def test_a_passing_unit_is_written_into_the_test_tree(tmp_path, migrated):
    result, events = _run(tmp_path, migrated)
    _source, out = migrated
    assert result.totals["generated"] == 1 and result.totals["held"] == 0
    assert (out / "src/test/java/com/corp/user/UserServiceTest.java").is_file()
    assert result.units[0]["status"] == "GENERATED"
    assert result.units[0]["review_score"] == 90
    assert [e["type"] for e in events] == ["testgen_start", "testgen_unit", "testgen_summary"]
    assert result.exit_code == 0


def test_two_model_calls_per_class_one_to_write_and_one_to_grade(tmp_path, migrated):
    result, _ = _run(tmp_path, migrated)
    assert result.record["totals"]["bedrock_calls"] == 2


def test_a_low_review_score_stages_the_test_instead_of_writing_it(tmp_path, migrated):
    result, _ = _run(tmp_path, migrated, mock={"review_score": 40})
    _source, out = migrated
    assert result.totals["held"] == 1 and result.totals["generated"] == 0
    assert not (out / "src/test/java/com/corp/user/UserServiceTest.java").exists()
    assert (out / ".forge-staging/src/test/java/com/corp/user/UserServiceTest.java").is_file()
    assert "below the pass threshold" in result.units[0]["hold_reason"]
    assert result.exit_code == 1


def test_a_junit4_test_is_retried_then_held_and_never_reaches_the_reviewer(tmp_path, migrated):
    def junit4(fqcn):
        return generated_test(fqcn).replace("import org.junit.jupiter.api.Test;", "import org.junit.Test;")

    with mocked_testgen(content=junit4) as mocks:
        cfg = write_config(tmp_path)
        source, out = migrated
        result = service.generate_tests(str(source), str(out), cfg)

    unit = result.units[0]
    assert unit["status"] == "HELD" and unit["retry_count"] == 1
    assert any("JUnit 4" in f for f in unit["check_failures"])
    assert mocks["generate"].return_value.invoke.call_count == 2, "the retry re-ran the generator"
    assert mocks["review"].return_value.invoke.call_count == 0, "a mechanical failure costs no review call"
    assert not (out / "src/test/java/com/corp/user/UserServiceTest.java").exists()


def test_the_retry_prompt_carries_the_mechanical_failures_first(tmp_path, migrated):
    def junit4(fqcn):
        return generated_test(fqcn).replace("import org.junit.jupiter.api.Test;", "import org.junit.Test;")

    with mocked_testgen(content=junit4) as mocks:
        source, out = migrated
        service.generate_tests(str(source), str(out), write_config(tmp_path))

    retry_prompt = mocks["generate"].return_value.invoke.call_args_list[1][0][0][1].content
    assert "PREVIOUS ATTEMPT FEEDBACK (retry 1)" in retry_prompt
    assert "Mechanical failures" in retry_prompt
    assert retry_prompt.index("Mechanical failures") < len(retry_prompt)


def test_a_class_carrying_a_secret_is_blocked_before_any_model_call(tmp_path, migrated):
    _source, out = migrated
    path = out / "src/main/java/com/corp/user/UserService.java"
    path.write_text(SERVICE_JAVA.replace(
        "    public String greet(String name) {",
        '    private static final String AES_KEY = "MySuperSecretKey";\n\n    public String greet(String name) {'),
        encoding="utf-8")

    with mocked_testgen() as mocks:
        source, out = migrated
        result = service.generate_tests(str(source), str(out), write_config(tmp_path))

    unit = result.units[0]
    assert unit["status"] == "BLOCKED" and "AES_KEY" in unit["error"]
    assert mocks["generate"].return_value.invoke.call_count == 0
    assert result.record["totals"]["bedrock_calls"] == 0


def test_a_secret_invented_into_the_test_is_a_mechanical_failure(tmp_path, migrated):
    def leaky(fqcn):
        return generated_test(fqcn).replace(
            "        assertEquals(2, 1 + 1);",
            '        String encryptionKey = "0123456789abcdef";\n        assertEquals(2, 1 + 1);')

    with mocked_testgen(content=leaky):
        source, out = migrated
        result = service.generate_tests(str(source), str(out), write_config(tmp_path))

    unit = result.units[0]
    assert unit["status"] == "HELD"
    assert any("generated test contains" in f for f in unit["check_failures"])


def test_a_generator_that_does_not_return_json_holds_the_unit(tmp_path, migrated):
    with mocked_testgen(payload={"nonsense": True}):
        source, out = migrated
        result = service.generate_tests(str(source), str(out), write_config(tmp_path))
    unit = result.units[0]
    assert unit["status"] == "HELD" and "no files" in "".join(unit["check_failures"])


def test_a_dry_run_writes_the_report_but_no_test_file(tmp_path, migrated):
    result, _ = _run(tmp_path, migrated, dry_run=True)
    _source, out = migrated
    assert not (out / "src/test/java/com/corp/user/UserServiceTest.java").exists()
    assert (out / "test-generation-report.md").is_file()
    record = json.loads((out / "generated-tests.json").read_text(encoding="utf-8"))
    assert record["dry_run"] is True
    assert record["units"][0]["files"], "a dry run carries the test it would have written"


# ─── running the generated tests ──────────────────────────────────────────────

def test_running_tests_is_skipped_when_it_is_not_enabled(tmp_path, migrated):
    result, _ = _run(tmp_path, migrated)
    assert result.units[0]["test_verdict"] == "SKIPPED"


def test_a_missing_toolchain_is_skipped_not_failed(tmp_path, migrated):
    source, out = migrated
    with mocked_testgen(), patch("shutil.which", return_value=None):
        result = service.generate_tests(str(source), str(out), write_config(tmp_path), run_tests=True)
    unit = result.units[0]
    assert unit["test_verdict"] == "SKIPPED" and "not found on PATH" in unit["test_output_log"]
    assert unit["status"] == "GENERATED", "an environment gap is not a bad test"


def test_a_failing_test_is_taken_back_out_of_the_tree_and_held(tmp_path, migrated):
    source, out = migrated
    failed = {"verdict": "FAIL", "output": "UserServiceTest.greet:12 expected <hi a> but was <null>", "command": "mvn"}
    with mocked_testgen(), patch("forge.testgen.runner.TestRunner.run", return_value=failed):
        result = service.generate_tests(str(source), str(out), write_config(tmp_path), run_tests=True)

    unit = result.units[0]
    assert unit["status"] == "HELD" and unit["test_verdict"] == "FAIL"
    assert unit["retry_count"] == 1, "a failing test is retried once on this config"
    assert not (out / "src/test/java/com/corp/user/UserServiceTest.java").exists()
    assert (out / ".forge-staging/src/test/java/com/corp/user/UserServiceTest.java").is_file()
    assert "expected <hi a>" in unit["test_output_log"]


def test_the_failure_output_reaches_the_retry_prompt(tmp_path, migrated):
    source, out = migrated
    failed = {"verdict": "FAIL", "output": "expected <hi a> but was <null>", "command": "mvn"}
    with mocked_testgen() as mocks, patch("forge.testgen.runner.TestRunner.run", return_value=failed):
        service.generate_tests(str(source), str(out), write_config(tmp_path), run_tests=True)

    retry_prompt = mocks["generate"].return_value.invoke.call_args_list[1][0][0][1].content
    assert "expected <hi a> but was <null>" in retry_prompt
    assert "Do NOT change the class under test" in retry_prompt


# ─── the artifacts ────────────────────────────────────────────────────────────

def test_the_report_names_the_dependencies_and_the_skipped_classes(tmp_path, migrated):
    _source, out = migrated
    (out / "src/main/java/com/corp/user/Marker.java").write_text(
        "package com.corp.user;\npublic interface Marker {}\n", encoding="utf-8")
    result, _ = _run(tmp_path, migrated)

    report = (out / "test-generation-report.md").read_text(encoding="utf-8")
    assert "org.junit.jupiter:junit-jupiter" in report
    assert "Marker.java" in report and "no behaviour to test" in report
    assert result.record["skipped"][0]["reason"].startswith("interface")


def test_the_record_is_json_safe_and_carries_the_verdicts(tmp_path, migrated):
    result, _ = _run(tmp_path, migrated)
    record = json.loads(json.dumps(result.record, default=str))
    unit = record["units"][0]
    assert unit["status"] == "GENERATED" and unit["review_verdict"] == "PASS"
    assert unit["test_rel_path"] == "src/test/java/com/corp/user/UserServiceTest.java"
    assert json.loads(json.dumps(result.to_json()))["totals"]["generated"] == 1


def test_nothing_to_generate_still_writes_the_report(tmp_path):
    empty = tmp_path / "out"
    empty.mkdir()
    result = service.generate_tests(str(tmp_path / "src"), str(empty), write_config(tmp_path))
    assert result.totals["total"] == 0
    assert (empty / "test-generation-report.md").is_file()
    assert result.exit_code == 0


# ─── chaining onto a migration ────────────────────────────────────────────────

LEGACY = "package com.corp.user;\nimport javax.persistence.Entity;\npublic class UserAction {}\n"


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp/user"
    base.mkdir(parents=True)
    (base / "UserAction.java").write_text(LEGACY, encoding="utf-8")
    return tmp_path / "proj"


def _migrate_with_tests(tmp_path, project, **kw):
    from tests.conftest import mocked_aws

    events = []
    cfg = write_config(tmp_path)
    with mocked_aws(), mocked_testgen(), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        result = service.run_migration(str(project), "javax-to-jakarta", str(tmp_path / "out"), cfg,
                                       with_tests=True, on_event=events.append, **kw)
    return result, events


def test_a_run_generates_tests_for_the_files_it_wrote(tmp_path, project):
    result, events = _migrate_with_tests(tmp_path, project)
    out = tmp_path / "out"
    assert result.testgen is not None and result.testgen.totals["generated"] == 1
    assert (out / "src/test/java/com/corp/user/UserActionTest.java").is_file()
    assert result.paths["testgen"] == str(out / "test-generation-report.md")
    assert [e["type"] for e in events] == [
        "start", "file", "testgen_start", "testgen_unit", "testgen_summary", "summary",
    ], "tests are generated after the migration, before the run summary"
    assert json.loads(json.dumps(result.summary()))["testgen"]["totals"]["generated"] == 1


def test_a_dry_run_has_nothing_to_write_tests_for(tmp_path, project):
    result, _ = _migrate_with_tests(tmp_path, project, dry_run=True)
    assert result.testgen.totals["total"] == 0, "nothing was written, so there is no new code to test"


def test_the_cli_prints_the_test_generation_lines(tmp_path, project, capsys):
    import sys

    import migrate
    from tests.conftest import mocked_aws

    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    out = tmp_path / "out"
    with mocked_aws(), mocked_testgen(), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"), \
         patch.object(sys, "argv", ["migrate.py", str(project), "--phase", "javax-to-jakarta",
                                    "--output-dir", str(out), "--config", str(cfg), "--generate-tests"]):
        migrate.main()

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines == [
        "FORGE — phase: javax-to-jakarta | files: 1 | dry-run: False",
        "[1/1] UserAction.java → DONE, score: 95",
        "Test generation (junit5) — 1 class(es), 0 skipped",
        "[1/1] UserAction (plain) → GENERATED, score: 90",
        "Tests: 1 written | 0 held | 0 blocked | 0 passed | 0 failed",
        "Test dependencies needed: org.junit.jupiter:junit-jupiter",
        f"Test report: {out / 'test-generation-report.md'}",
        "Summary: 1 passed | 0 manual | 0 blocked | 3 Bedrock calls",
        f"Report: {out / 'migration-report.md'}",
    ]


def test_generate_tests_only_runs_over_an_existing_output_dir(tmp_path, migrated, capsys):
    import sys

    import migrate

    source, out = migrated
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    with mocked_testgen(), patch.object(sys, "argv", [
            "migrate.py", str(source), "--generate-tests-only", "--output-dir", str(out), "--config", str(cfg)]):
        code = migrate.main()

    assert code == 0
    assert (out / "src/test/java/com/corp/user/UserServiceTest.java").is_file()
    assert "Test generation (junit5) — 1 class(es), 0 skipped" in capsys.readouterr().out


def test_generate_tests_only_exits_non_zero_when_something_needs_a_human(tmp_path, migrated):
    import sys

    import migrate

    source, out = migrated
    cfg = tmp_path / "agents.yaml"
    write_config(tmp_path)
    with mocked_testgen(review_score=30), patch.object(sys, "argv", [
            "migrate.py", str(source), "--generate-tests-only", "--output-dir", str(out), "--config", str(cfg)]):
        assert migrate.main() == 1, "a held test must gate CI"


# ─── configuration ────────────────────────────────────────────────────────────

def test_settings_fall_back_to_the_migration_models(tmp_path):
    settings = testgen_settings.TestGenSettings.from_config(write_config(tmp_path, test_generation={"enabled": True}))
    assert settings.model == "us.anthropic.claude-opus-4-8"
    assert settings.review_model == "us.amazon.nova-pro-v1:0"
    assert settings.run.enabled is False


def test_kinds_restricts_which_classes_are_generated_for(tmp_path):
    out = tmp_path / "out"
    p = out / "src/main/java/com/corp/web/AccountController.java"
    p.parent.mkdir(parents=True)
    p.write_text(CONTROLLER_JAVA, encoding="utf-8")
    scan = scan_test_targets(str(out), str(tmp_path), kinds=("service",))
    assert scan.targets == []
    assert "kind 'controller' is not in test_generation.kinds" in scan.skipped[0].reason
