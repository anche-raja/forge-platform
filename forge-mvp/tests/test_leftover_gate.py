"""What a pack must remove must be gone: its `no_match` rules are checked per unit, before the review.

On AMS the Struts pack rewrote an interceptor and its test and left their
``com.opensymphony.xwork2`` imports in place; the reviewer passed both and the
build failed to compile them. The pack's own acceptance rule forbade exactly
that, but acceptance runs over the tree only when asked, and the chat never
asks.
"""

import contextlib

from forge.packs.spec import AcceptanceCheck
from forge.phases import get_phase
from forge.verify import syntax
from tests.conftest import CLEAN_JAVA, make_state, write_config
from tests.test_syntax import _graph

STRUTS = get_phase("struts2-modernize")
LEFTOVER = ("package org.example.web;\nimport com.opensymphony.xwork2.ActionInvocation;\n"
            "public class AddressValidationInterceptor {}\n")
CLEAN = "package org.example.web;\nimport org.apache.struts2.ActionInvocation;\npublic class AddressValidationInterceptor {}\n"


def test_a_leftover_the_pack_forbids_is_named_with_its_line():
    errors = syntax.leftover_errors({"/x/src/main/java/org/example/web/AddressValidationInterceptor.java": LEFTOVER},
                                    STRUTS.acceptance)
    assert len(errors) == 1
    assert errors[0].startswith("AddressValidationInterceptor.java:2: still contains `com\\.opensymphony`")


def test_a_clean_file_and_a_file_outside_the_rules_scope_pass():
    assert syntax.leftover_errors({"A.java": CLEAN}, STRUTS.acceptance) == []
    assert syntax.leftover_errors({"notes/README.md": "com.opensymphony.xwork2 was here"}, STRUTS.acceptance) == []
    assert syntax.leftover_errors({"WEB-INF/struts-old.xml": "<x>com.opensymphony</x>"}, STRUTS.acceptance) != []
    assert syntax.leftover_errors({"WEB-INF/other.xml": "<x>com.opensymphony</x>"}, STRUTS.acceptance) == []


def test_a_rule_gated_on_a_decision_applies_only_when_it_holds():
    gated = AcceptanceCheck(kind="no_match", value="struts", scope="**/*.jsp",
                            when=(("web_framework", "migrate-to-spring"),))
    files = {"home.jsp": "<%@ taglib prefix=\"s\" uri=\"/struts-tags\" %>"}
    assert syntax.leftover_errors(files, [gated], {"web_framework": "modernize-in-place"}) == []
    assert syntax.leftover_errors(files, [gated], {"web_framework": "migrate-to-spring"}) != []
    assert syntax.leftover_errors(files, [gated]) != [], "with no decisions given every rule applies"


def test_other_acceptance_kinds_and_a_bad_regex_are_ignored():
    checks = [AcceptanceCheck(kind="build", value="mvn package"), AcceptanceCheck(kind="no_match", value="[unclosed")]
    assert syntax.leftover_errors({"A.java": "anything"}, checks) == []


def _run(tmp_path, java_file, answers):
    from forge.graph import build_graph

    cfg = write_config(tmp_path, syntax_check=True)
    with contextlib.ExitStack() as stack:
        up, rev = _graph(stack, java_file, answers)
        result = build_graph(cfg).invoke(make_state(java_file, tmp_path, phase="struts2-modernize"),
                                         config={"configurable": {"thread_id": java_file}})
    return result, up, rev


def test_the_graph_sends_a_leftover_back_with_the_line_and_never_reviews_it(tmp_path, java_file):
    result, up, rev = _run(tmp_path, java_file, [LEFTOVER, CLEAN])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert fs["syntax_verdict"] == syntax.PASS and not fs.get("error")
    assert rev.return_value.invoke.call_count == 1, "the leftover cost no review"
    retry_prompt = up.return_value.invoke.call_args_list[1][0][0][1].content
    assert "still contains `com\\.opensymphony`" in retry_prompt and "must remove" in retry_prompt


def test_a_pack_with_no_leftover_rules_is_unchanged(tmp_path, java_file):
    from forge.graph import build_graph

    cfg = write_config(tmp_path, syntax_check=True)
    with contextlib.ExitStack() as stack:
        _graph(stack, java_file, [CLEAN_JAVA])
        result = build_graph(cfg).invoke(make_state(java_file, tmp_path),
                                         config={"configurable": {"thread_id": java_file}})
    assert result["current_file"]["status"] == "DONE" and result["current_file"]["retry_count"] == 0
