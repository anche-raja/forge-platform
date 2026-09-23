"""javac parses every transform before the reviewer reads it.

The javac tests run the real compiler and are skipped where there is none.
The graph tests replace the checker, so they pin the routing -- a broken
answer retries with javac's errors as the feedback and never costs a review
call -- on any machine.
"""

import contextlib
from unittest.mock import patch

import pytest

from forge.config import ForgeConfig
from forge.verify import syntax
from tests.conftest import CLEAN_JAVA, llm_reply, make_state, write_config

CONFIG = ForgeConfig(data={"target_java_version": "21"})
HAVE_JAVAC = bool(syntax.find_javac(CONFIG))
needs_javac = pytest.mark.skipif(not HAVE_JAVAC, reason="no javac on this machine")

LOADABLE = """package com.corp;
import java.util.Map;
public final class OrderType {
    private static Map<Long, OrderType> VALUES;
    public static OrderType lookup(Long id) {
        for (OrderType t : VALUES.values()) {
            if (id.equals(t.id)) {
                return t;
            }
        }%s
        return null;
    }
    private Long id;
}
"""


# ─── javac, for real ──────────────────────────────────────────────────────────

@needs_javac
@pytest.mark.parametrize("damage", ["ßßß", "}"])      # the two shapes seen live on AMS
def test_the_damage_seen_on_ams_fails_with_a_line_number(damage):
    verdict, errors = syntax.check_files({"src/OrderType.java": LOADABLE % damage}, CONFIG)
    assert verdict == syntax.FAIL
    assert errors[0].startswith("OrderType.java:"), errors
    assert "/" not in errors[0].split(":")[0], "temp paths are stripped"


@needs_javac
def test_clean_java_21_passes():
    source = """package com.corp;
public record Money(long cents, String currency) {
    public String label() {
        return switch (currency) {
            case "USD" -> "$" + cents / 100;
            default -> cents / 100 + " " + currency;
        };
    }
}
"""
    assert syntax.check_files({"Money.java": source}, CONFIG) == (syntax.PASS, [])


@needs_javac
def test_parse_only_so_a_missing_dependency_is_not_an_error():
    """Symbols are never resolved: that is the project build's job, not this one's."""
    source = "package p;\nimport com.nowhere.Missing;\npublic class A { Missing m; }\n"
    assert syntax.check_files({"A.java": source}, CONFIG) == (syntax.PASS, [])


def test_no_javac_is_a_skip_not_a_failure(monkeypatch):
    monkeypatch.setattr(syntax, "find_javac", lambda config: "")
    assert syntax.check_files({"A.java": "not java at all"}, CONFIG) == (syntax.SKIPPED, [])


def test_xml_is_checked_for_well_formedness_without_any_tool():
    assert syntax.check_files({"pom.xml": "<project><a></project>"}, CONFIG)[0] == syntax.FAIL
    assert syntax.check_files({"pom.xml": "<project><a/></project>"}, CONFIG) == (syntax.PASS, [])


def test_other_files_are_not_checked():
    assert syntax.check_files({"index.jsp": "<% broken"}, CONFIG) == (syntax.PASS, [])


# ─── the graph ────────────────────────────────────────────────────────────────

def _graph(stack, file_path, answers):
    """Patch every Bedrock touchpoint; the transform returns ``answers`` in turn."""
    boto = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
    stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
    up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
    rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
    post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
    # forge.graph binds DynamoDBSaver at import, so its copy is the one to replace.
    saver = stack.enter_context(patch("forge.graph.DynamoDBSaver"))
    from langgraph.checkpoint.memory import MemorySaver
    saver.return_value = MemorySaver()
    boto.client.return_value.apply_guardrail.return_value = {"action": "NONE", "assessments": []}
    up.return_value.invoke.side_effect = [llm_reply({"files": {file_path: a}, "manual_flags": []})
                                          for a in answers]
    rev.return_value.invoke.return_value = llm_reply({"score": 95, "verdict": "PASS", "feedback": "", "checks": {}})
    post.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
    return up, rev


def _checker(broken_marker="BROKEN"):
    def check(files, config):
        bad = [f"{p.rsplit('/', 1)[-1]}:3: error: illegal start of type"
               for p, text in files.items() if broken_marker in text]
        return (syntax.FAIL, bad) if bad else (syntax.PASS, [])
    return check


def _run(tmp_path, java_file, answers, *, check=None, **config):
    from forge.graph import build_graph

    cfg = write_config(tmp_path, syntax_check=True, **config)
    with contextlib.ExitStack() as stack:
        up, rev = _graph(stack, java_file, answers)
        stack.enter_context(patch("forge.verify.syntax.check_files", check or _checker()))
        result = build_graph(cfg).invoke(make_state(java_file, tmp_path),
                                         config={"configurable": {"thread_id": java_file}})
    return result, up, rev


def test_a_broken_answer_is_retried_with_the_errors_and_never_reviewed(tmp_path, java_file):
    result, up, rev = _run(tmp_path, java_file, [CLEAN_JAVA + "BROKEN", CLEAN_JAVA])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert fs["syntax_verdict"] == syntax.PASS and not fs.get("error")
    # Transform twice, review and post-check once: the broken answer cost no review.
    assert rev.return_value.invoke.call_count == 1
    assert result["bedrock_calls"] == 4
    retry_prompt = up.return_value.invoke.call_args_list[1][0][0][1].content
    assert "does not parse" in retry_prompt and "illegal start of type" in retry_prompt


def test_broken_every_time_ends_in_manual_review_with_the_error(tmp_path, java_file):
    result, _, rev = _run(tmp_path, java_file, [CLEAN_JAVA + "BROKEN"] * 3, max_retries=2)
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert fs["error"].startswith("Transform output does not parse")
    assert rev.return_value.invoke.call_count == 0
    assert any(f.startswith("syntax: ") for f in fs["guardrail_findings"])


def test_no_javac_skips_and_the_file_still_reaches_review(tmp_path, java_file):
    skip = lambda files, config: (syntax.SKIPPED, [])  # noqa: E731
    result, _, rev = _run(tmp_path, java_file, [CLEAN_JAVA], check=skip)
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["syntax_verdict"] == syntax.SKIPPED
    assert rev.return_value.invoke.call_count == 1
    assert any("syntax check skipped" in f for f in fs["guardrail_findings"])


def test_off_by_default_so_the_graph_is_unchanged(tmp_path, java_file):
    from forge.graph import build_graph

    cfg = write_config(tmp_path)          # no syntax_check key
    called = []
    with contextlib.ExitStack() as stack:
        _graph(stack, java_file, [CLEAN_JAVA])
        stack.enter_context(patch("forge.verify.syntax.check_files",
                                  lambda *a, **k: called.append(1) or (syntax.PASS, [])))
        result = build_graph(cfg).invoke(make_state(java_file, tmp_path),
                                         config={"configurable": {"thread_id": java_file}})
    assert result["current_file"]["status"] == "DONE" and not called
