"""Phase registry: prompt selection, file eligibility, and the struts-spring6 path."""

import contextlib
import re
from pathlib import Path

import pytest

from forge.phases import PHASE_NAMES, PHASES, get_phase
from forge.utils.file_scanner import scan_java_files
from tests.conftest import llm_reply, make_state, write_config
from tests.test_phase0_closeout import _graph_mocks


# ─── registry integrity ──────────────────────────────────────────────────────

def test_unknown_phase_is_rejected_with_options():
    with pytest.raises(ValueError, match="Unknown phase"):
        get_phase("java99")


@pytest.mark.parametrize("name", PHASE_NAMES)
def test_every_phase_is_well_formed(name):
    spec = PHASES[name]
    assert spec.name == name
    assert spec.description.isascii(), "descriptions are printed by argparse to a cp1252 console"
    assert "valid JSON" in spec.transform_prompt
    assert '"files"' in spec.transform_prompt
    assert '"score"' in spec.review_prompt


@pytest.mark.parametrize("name", PHASE_NAMES)
def test_review_rubric_sums_to_100(name):
    """A reviewer whose checks do not total 100 makes the pass/retry thresholds
    meaningless."""
    weights = [int(n) for n in re.findall(r"\((\d+) pts\)", PHASES[name].review_prompt)]
    assert weights, f"{name} rubric declares no point weights"
    assert sum(weights) == 100, f"{name} rubric sums to {sum(weights)}: {weights}"


@pytest.mark.parametrize("name", PHASE_NAMES)
def test_jdk_javax_exception_is_stated_in_both_prompts(name):
    """Telling the model 'zero javax.* allowed' without the JDK carve-out invites
    it to rewrite javax.crypto to jakarta.crypto and break the build."""
    spec = PHASES[name]
    for prompt in (spec.transform_prompt, spec.review_prompt):
        assert "javax.crypto" in prompt and "javax.sql" in prompt


# ─── file eligibility ────────────────────────────────────────────────────────

def _tree(tmp_path):
    files = {
        "src/main/java/com/corp/LoginAction.java": "package com.corp;",
        "src/main/resources/struts-config.xml": "<struts-config/>",
        "src/main/resources/validation.xml": "<form-validation/>",
        "pom.xml": "<project/>",
        "build.xml": "<project/>",
        "src/test/java/com/corp/LoginActionTest.java": "package com.corp;",
    }
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def _rel(root, paths):
    return sorted(str(Path(p).relative_to(root)).replace("\\", "/") for p in paths)


def _scan(tmp_path, phase, scope=""):
    """Scanned file paths, relative to the project root."""
    root = tmp_path.resolve()
    return _rel(root, scan_java_files(str(root), phase, scope).files)


def _scan_skipped(tmp_path, phase, scope=""):
    """Paths the scanner declined, relative to the project root."""
    root = tmp_path.resolve()
    return _rel(root, [sk.path for sk in scan_java_files(str(root), phase, scope).skipped])


def test_java21_takes_only_java(tmp_path):
    assert _scan(_tree(tmp_path), "java21") == ["src/main/java/com/corp/LoginAction.java"]


def test_struts_phase_includes_struts_xml_but_not_pom(tmp_path):
    got = _scan(_tree(tmp_path), "struts-spring6")
    assert "src/main/resources/struts-config.xml" in got
    assert "src/main/resources/validation.xml" in got
    assert "pom.xml" not in got and "build.xml" not in got


def test_test_sources_excluded_in_every_phase(tmp_path):
    root = _tree(tmp_path)
    for phase in PHASE_NAMES:
        assert not any("src/test" in f for f in _scan(root, phase))


# ─── struts phase through the graph ──────────────────────────────────────────

SPRING_CONTROLLER = """\
package com.corp.web;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
public class LoginController {
    @GetMapping("/login")
    public String login(@Valid Object form) { return "login"; }
}
"""


def test_struts_phase_uses_its_own_prompts(tmp_path, java_file):
    """The transform and review calls must carry the struts rubric, not java21's."""
    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=90)
        up.return_value.invoke.return_value = llm_reply(
            {"files": {java_file: SPRING_CONTROLLER}, "manual_flags": []}
        )
        from forge.graph import build_graph

        app = build_graph(write_config(tmp_path))
        state = make_state(java_file, tmp_path, phase="struts-spring6")
        result = app.invoke(state, config={"configurable": {"thread_id": java_file}})

    assert result["current_file"]["status"] == "DONE"
    system_prompt = up.return_value.invoke.call_args[0][0][0].content
    assert "Struts" in system_prompt
    assert "ActionForm" in system_prompt


def test_deleted_files_are_recorded_and_reported(tmp_path, java_file):
    """struts-spring6 replaces XML config with @Configuration; the superseded
    files must be surfaced rather than silently orphaned."""
    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=90)
        up.return_value.invoke.return_value = llm_reply({
            "files": {java_file: SPRING_CONTROLLER},
            "deleted_files": ["src/main/resources/struts-config.xml"],
            "manual_flags": [],
        })
        from forge.graph import build_graph

        app = build_graph(write_config(tmp_path))
        result = app.invoke(
            make_state(java_file, tmp_path, phase="struts-spring6"),
            config={"configurable": {"thread_id": java_file}},
        )

    fs = result["current_file"]
    assert fs["deleted_files"] == ["src/main/resources/struts-config.xml"]

    from forge.utils.report import generate_report
    out = tmp_path / "report.md"
    generate_report(str(out), "struts-spring6", str(tmp_path), [fs], bedrock_calls=4, estimated_cost_usd=0.01)
    body = out.read_text(encoding="utf-8")
    assert "XML configs replaced by Java configuration" in body
    assert "struts-config.xml" in body


def test_java21_remains_the_default_phase(tmp_path, java_file):
    """Existing behaviour must be untouched by the registry refactor."""
    from tests.conftest import CLEAN_JAVA

    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=95)
        from forge.graph import build_graph

        app = build_graph(write_config(tmp_path))
        result = app.invoke(
            make_state(java_file, tmp_path, phase="java21"),
            config={"configurable": {"thread_id": java_file}},
        )

    assert result["current_file"]["status"] == "DONE"
    system_prompt = up.return_value.invoke.call_args[0][0][0].content
    assert "Struts" not in system_prompt
    assert "Rule 1 — Namespace migration" in system_prompt
