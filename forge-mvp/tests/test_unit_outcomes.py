"""How a unit ends when a model's answer is not the answer the pipeline expected.

Every case here was a unit in the first full ten-pack AMS run (2026-09-23)
that reached a human for a reason other than a bad migration: a broken JSON
envelope from the transform (#20), a file the model said needs no change (#17).
"""

import contextlib
from unittest.mock import MagicMock, patch

from tests.conftest import CLEAN_JAVA, llm_reply, make_state, write_config


def _reply(answer):
    """A dict is a JSON reply; a string is sent as-is, however broken."""
    if isinstance(answer, dict):
        return llm_reply(answer)
    m = MagicMock()
    m.content = answer
    m.usage_metadata = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
    return m


def _files(java_file, text=CLEAN_JAVA):
    return {"files": {java_file: text}, "manual_flags": []}


_PASS = {"score": 95, "verdict": "PASS", "feedback": "", "checks": {}}


def _run(tmp_path, java_file, transforms, *, reviews=(_PASS,), guardrail=None, phase="java21",
         dry_run=True, **config):
    """One unit through the real graph; every Bedrock touchpoint scripted.

    ``transforms`` and ``reviews`` are answered in turn (the last review
    repeats); ``guardrail`` maps "INPUT"/"OUTPUT" to an ApplyGuardrail response.
    """
    from forge.graph import build_graph

    cfg = write_config(tmp_path, **config)
    guardrail = guardrail or {}
    with contextlib.ExitStack() as stack:
        boto = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
        stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
        up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
        post = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.graph.DynamoDBSaver"))
        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()

        boto.client.return_value.apply_guardrail.side_effect = (
            lambda **kw: guardrail.get(kw["source"], {"action": "NONE", "assessments": []}))
        up.return_value.invoke.side_effect = [_reply(a) for a in transforms]
        answers = [_reply(r) for r in reviews]
        rev.return_value.invoke.side_effect = lambda messages: answers.pop(0) if len(answers) > 1 else answers[0]
        post.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        result = build_graph(cfg).invoke(make_state(java_file, tmp_path, phase=phase, dry_run=dry_run),
                                         config={"configurable": {"thread_id": java_file}})
    return result, up.return_value.invoke, rev.return_value.invoke


# ─── #20: a transform reply that cannot be read is retried ───────────────────

_BROKEN = '{"files": {"UserAction.java": "public class UserAction { String s = "unescaped"; }"}}'


def test_a_broken_json_reply_is_retried_and_the_retry_asks_for_valid_json(tmp_path, java_file):
    result, upgrade, review = _run(tmp_path, java_file, [_BROKEN, _files(java_file)])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert not fs.get("error") and not fs.get("transform_malformed")
    # Two transforms, one review: the broken answer cost no review call.
    assert upgrade.call_count == 2 and review.call_count == 1
    retry_prompt = upgrade.call_args_list[1][0][0][1].content
    assert "Failed to parse transform output as JSON" in retry_prompt
    assert "exactly one valid JSON object" in retry_prompt


def test_a_wrong_shape_is_retried_the_same_way(tmp_path, java_file):
    wrong = {"files": {java_file: {"language": "java"}}}
    result, upgrade, _ = _run(tmp_path, java_file, [wrong, _files(java_file)])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert "wrong shape" in upgrade.call_args_list[1][0][0][1].content


def test_broken_every_time_ends_in_manual_review_with_the_parse_error(tmp_path, java_file):
    result, upgrade, review = _run(tmp_path, java_file, [_BROKEN] * 3, max_retries=2)
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW" and fs["retry_count"] == 2
    assert fs["error"].startswith("Failed to parse transform output as JSON")
    assert upgrade.call_count == 3 and review.call_count == 0
    assert result["files_manual"] == 1


def test_a_broken_reply_after_a_good_one_is_not_reviewed_as_the_old_output(tmp_path, java_file):
    """The retry's broken answer must not leave the previous attempt's output
    for the reviewer to grade and the writer to write."""
    low = {"score": 60, "verdict": "RETRY", "feedback": "fix X", "checks": {}}
    result, upgrade, review = _run(tmp_path, java_file, [_files(java_file), _BROKEN, _BROKEN],
                                   reviews=[low], max_retries=2)
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert fs["error"].startswith("Failed to parse transform output as JSON")
    assert fs["transform_output"] is None
    assert review.call_count == 1


def test_a_broken_reply_skips_the_syntax_check(tmp_path, java_file):
    from forge.verify import syntax

    seen = []
    with patch("forge.verify.syntax.check_files",
               lambda files, config: seen.append(dict(files)) or (syntax.PASS, [])):
        result, _, _ = _run(tmp_path, java_file, [_BROKEN, _files(java_file)], syntax_check=True)
    assert result["current_file"]["status"] == "DONE"
    assert seen == [{java_file: CLEAN_JAVA}]


# ─── #17: an empty files map means "no change needed" ────────────────────────

_NOTHING = {"files": {}, "manual_flags": []}


def test_an_empty_files_map_is_done_unchanged_with_no_review_and_nothing_written(tmp_path, java_file):
    # review-all would hold every unit that reached the gate; build verification
    # would compile whatever was written. An unchanged unit reaches neither.
    result, upgrade, review = _run(tmp_path, java_file, [_NOTHING], dry_run=False,
                                   decisions={"risk_ceiling": "review-all"},
                                   build_verification={"enabled": True, "mode": "javac"})
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["unchanged"] is True
    assert fs["written_paths"] == [] and not fs.get("held_paths")
    assert fs["review_score"] is None and not fs.get("error")
    assert fs["build_verdict"] is None and fs["guardrail_post_verdict"] is None
    assert upgrade.call_count == 1 and review.call_count == 0
    assert result["files_passed"] == 1 and result["bedrock_calls"] == 1
    assert not (tmp_path / "migrated").exists()


def test_a_changed_file_is_not_marked_unchanged(tmp_path, java_file):
    result, _, _ = _run(tmp_path, java_file, [_files(java_file)])
    assert result["current_file"]["status"] == "DONE"
    assert result["current_file"]["unchanged"] is False


def test_a_reply_with_no_files_key_is_malformed_not_unchanged(tmp_path, java_file):
    """Only an explicit {} says "nothing to change"; an absent key says nothing."""
    result, upgrade, _ = _run(tmp_path, java_file, [{"manual_flags": []}, _files(java_file)])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and not fs["unchanged"] and fs["retry_count"] == 1
    assert "no 'files' key" in upgrade.call_args_list[1][0][0][1].content


def test_an_empty_answer_to_a_retry_is_not_unchanged(tmp_path, java_file):
    """The reviewer sent it back because it needs changes; "nothing to change"
    would drop that feedback on the floor and mark the unit DONE."""
    low = {"score": 60, "verdict": "RETRY", "feedback": "fix X", "checks": {}}
    result, upgrade, review = _run(tmp_path, java_file, [_files(java_file), _NOTHING, _files(java_file)],
                                   reviews=[low, _PASS])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and not fs["unchanged"] and fs["retry_count"] == 2
    assert "asked to change the file" in upgrade.call_args_list[2][0][0][1].content
    assert review.call_count == 2


def test_an_empty_answer_after_an_unreadable_one_is_unchanged(tmp_path, java_file):
    result, _, review = _run(tmp_path, java_file, [_BROKEN, _NOTHING])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["unchanged"] is True and not fs.get("error")
    assert review.call_count == 0


def test_a_generated_unit_must_produce_its_file(tmp_path):
    from forge.agents.java_upgrade import JavaUpgradeAgent
    from forge.state import make_file_status

    target = str(tmp_path / "server.xml")
    state = make_state(target, tmp_path)
    state["current_file"] = {**make_file_status(target, "java21"), "generate": True}
    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as llm:
        llm.return_value.invoke.return_value = llm_reply(_NOTHING)
        fs = JavaUpgradeAgent(write_config(tmp_path)).run(state)["current_file"]
    assert fs["transform_malformed"] is True and not fs.get("unchanged")
    assert "generated" in fs["error"]
