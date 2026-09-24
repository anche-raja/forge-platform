"""How a unit ends when a model's answer is not the answer the pipeline expected.

Every case here was a unit in the first full ten-pack AMS run (2026-09-23)
that reached a human for a reason other than a bad migration: a broken JSON
envelope from the transform (#20), a file the model said needs no change (#17),
an unreadable reviewer reply (#19).
"""

import contextlib
from unittest.mock import MagicMock, patch

import pytest

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


_CLEAN = {"verdict": "PASS", "findings": [], "reason": ""}


def _run(tmp_path, java_file, transforms, *, reviews=(_PASS,), guardrail=None, phase="java21",
         dry_run=True, pre=_CLEAN, post=_CLEAN, build=None, patches=(), **config):
    """One unit through the real graph; every Bedrock touchpoint scripted.

    ``transforms`` and ``reviews`` are answered in turn (the last review
    repeats); ``guardrail`` maps "INPUT"/"OUTPUT" to an ApplyGuardrail response;
    ``build`` is the verdict every build verification returns.
    """
    from forge.graph import build_graph

    cfg = write_config(tmp_path, **config)
    guardrail = guardrail or {}
    with contextlib.ExitStack() as stack:
        boto = stack.enter_context(patch("forge.guardrails.bedrock_guardrails.boto3"))
        pre_llm = stack.enter_context(patch("forge.agents.guardrails_pre.ChatBedrockConverse"))
        up = stack.enter_context(patch("forge.agents.java_upgrade.ChatBedrockConverse"))
        rev = stack.enter_context(patch("forge.review.java_reviewer.ChatBedrockConverse"))
        post_llm = stack.enter_context(patch("forge.agents.guardrails_post.ChatBedrockConverse"))
        saver = stack.enter_context(patch("forge.graph.DynamoDBSaver"))
        if build is not None:
            verifier = stack.enter_context(patch("forge.graph.BuildVerifier"))
            verifier.return_value.verify.return_value = build
        for target, kwargs in patches:
            stack.enter_context(patch(target, **kwargs))
        from langgraph.checkpoint.memory import MemorySaver
        saver.return_value = MemorySaver()

        boto.client.return_value.apply_guardrail.side_effect = (
            lambda **kw: guardrail.get(kw["source"], {"action": "NONE", "assessments": []}))
        up.return_value.invoke.side_effect = [_reply(a) for a in transforms]
        answers = [_reply(r) for r in reviews]
        rev.return_value.invoke.side_effect = lambda messages: answers.pop(0) if len(answers) > 1 else answers[0]
        pre_llm.return_value.invoke.return_value = llm_reply(pre)
        post_llm.return_value.invoke.return_value = llm_reply(post)
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


# ─── #19: an unreadable review is asked for again once ───────────────────────

_BAD_REVIEW = '{"score": 90, "verdict": "PASS", feedback: ""}'


def test_an_unreadable_review_is_asked_again_and_the_second_answer_counts(tmp_path, java_file):
    result, upgrade, review = _run(tmp_path, java_file, [_files(java_file)], reviews=[_BAD_REVIEW, _PASS])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["review_score"] == 95 and not fs.get("error")
    assert fs["retry_count"] == 0 and upgrade.call_count == 1
    assert review.call_count == 2
    second = review.call_args_list[1][0][0][1].content
    assert "could not be read" in second and "Review this transformed code" in second
    # transform + two reviews + post-check; both reviews are charged.
    assert result["bedrock_calls"] == 4


def test_unreadable_twice_goes_to_manual_review_with_the_reason(tmp_path, java_file):
    result, upgrade, review = _run(tmp_path, java_file, [_files(java_file)], reviews=[_BAD_REVIEW])
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW" and fs["review_score"] == 0
    assert fs["error"].startswith("Failed to parse reviewer response after 2 attempts")
    assert review.call_count == 2 and upgrade.call_count == 1


def test_a_score_that_is_not_a_number_is_unreadable_not_a_crash(tmp_path, java_file):
    odd = {"score": "ninety", "verdict": "PASS", "feedback": "", "checks": {}}
    result, _, review = _run(tmp_path, java_file, [_files(java_file)], reviews=[odd, _PASS])
    assert result["current_file"]["status"] == "DONE" and review.call_count == 2


# ─── #26: every unit a human is handed says why ──────────────────────────────

_INTERVENED = {"action": "GUARDRAIL_INTERVENED", "assessments": []}
_LOW = {"score": 30, "verdict": "MANUAL", "feedback": "logic dropped", "checks": {}}
_MID = {"score": 60, "verdict": "RETRY", "feedback": "fix X", "checks": {}}
_PERFECT = {"score": 100, "verdict": "PASS", "feedback": "", "checks": {}}
_DIRTY = CLEAN_JAVA.replace("jakarta.servlet", "javax.servlet")


def _secret(*_a, **_k):
    finding = MagicMock()
    finding.describe.return_value = "aws-access-key at line 3"
    return [finding]


def _syntax_fail(files, config):
    from forge.verify import syntax
    return syntax.FAIL, ["UserAction.java:3: error: illegal start of type"]


# (id, expected status, _run kwargs, extra assertion on the error)
_STOPS = [
    ("guardrail-input", "BLOCKED", dict(guardrail={"INPUT": _INTERVENED}), "source file"),
    ("secret-scan", "BLOCKED", dict(patches=[("forge.agents.guardrails_pre.find_secrets",
                                              {"side_effect": _secret})]), "secret scan"),
    ("too-large", "BLOCKED", dict(complexity_block_threshold=3), "complexity_block_threshold"),
    ("preflight-block-no-reason", "BLOCKED",
     dict(preflight_model_check=True, pre={"verdict": "BLOCK", "findings": [], "reason": ""}),
     "pre-flight"),
    ("source-unreadable", "MANUAL_REVIEW",
     dict(patches=[("forge.agents.java_upgrade.open", {"create": True, "side_effect": OSError("gone")})]),
     "Cannot read file"),
    ("transform-malformed", "MANUAL_REVIEW", dict(transforms=[_BROKEN] * 3), "parse transform output"),
    ("syntax-fail", "MANUAL_REVIEW",
     dict(syntax_check=True, patches=[("forge.verify.syntax.check_files", {"side_effect": _syntax_fail})]),
     "does not parse"),
    ("review-below-retry", "MANUAL_REVIEW", dict(reviews=[_LOW]), "Review score 30 is below retry_threshold"),
    ("review-retries-spent", "MANUAL_REVIEW", dict(reviews=[_MID]),
     "Review score 60 is below pass_threshold 80 after 2 retries: fix X"),
    ("review-unreadable", "MANUAL_REVIEW", dict(reviews=[_BAD_REVIEW]), "Failed to parse reviewer response"),
    ("guardrail-output", "MANUAL_REVIEW", dict(reviews=[_PERFECT], guardrail={"OUTPUT": _INTERVENED}),
     "transform output"),
    ("javax-left", "MANUAL_REVIEW", dict(transforms=[_DIRTY], reviews=[_PERFECT]), "Unmigrated javax"),
    ("post-check-block-no-reason", "MANUAL_REVIEW",
     dict(post_model_check="block", post={"verdict": "BLOCK", "findings": [], "reason": ""}),
     "post-transform check"),
    ("build-fails", "MANUAL_REVIEW", dict(build={"verdict": "FAIL", "output": "cannot find symbol Foo"}),
     "Build verification failed after 2 retries: cannot find symbol Foo"),
]


@pytest.mark.parametrize("status, kwargs, expected", [s[1:] for s in _STOPS], ids=[s[0] for s in _STOPS])
def test_every_manual_review_and_blocked_unit_carries_a_reason(tmp_path, java_file, status, kwargs, expected):
    kwargs = dict(kwargs)
    transforms = kwargs.pop("transforms", None) or [_files(java_file)] * 3
    transforms = [t if t is not _DIRTY else _files(java_file, _DIRTY) for t in transforms]
    result, _, _ = _run(tmp_path, java_file, transforms, **kwargs)
    fs = result["current_file"]
    assert fs["status"] == status
    assert str(fs.get("error") or "").strip(), f"{status} with no error: {fs}"
    assert expected in fs["error"]
    assert "pipeline bug" not in fs["error"]


def test_the_ams_case_a_perfect_score_stopped_by_the_output_guardrail(tmp_path, java_file):
    """AmsUserDetailsServiceImplTest (junit4-to-junit5): review 100, retries 0,
    no post-check verdict. The one path that fits is an ApplyGuardrail
    intervention on OUTPUT, which returned before step 2 and 3 with no error.
    The reason names the policy and never the bytes it matched."""
    assessment = {"sensitiveInformationPolicy": {"piiEntities": [
        {"type": "PASSWORD", "match": "hunter2", "action": "BLOCKED"}]}}
    gr = {"OUTPUT": {"action": "GUARDRAIL_INTERVENED", "assessments": [assessment]}}
    result, _, review = _run(tmp_path, java_file, [_files(java_file)], reviews=[_PERFECT], guardrail=gr,
                             phase="junit4-to-junit5")
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW" and fs["review_score"] == 100 and fs["retry_count"] == 0
    assert fs.get("post_check_verdict") is None
    assert fs["error"] == "Bedrock guardrail intervened on the transform output (sensitiveInformationPolicy)"
    assert "hunter2" not in fs["error"]


def test_the_output_guardrail_is_never_shown_the_file_path(tmp_path, java_file):
    """java8-to-java21 on AMS (2026-09-23): every changed file held at review 85-100 with
    CREDIT_DEBIT_CARD_NUMBER. The match was the Windows SID in the "// FILE:" header's absolute
    path, never the code. Scripted as the real policy behaved: it intervenes on the path."""
    seen = []

    def evaluate(self, text, source):
        seen.append((source, text))
        hit = java_file in text
        return {"action": "GUARDRAIL_INTERVENED" if hit else "NONE", "intervened": hit,
                "findings": ["sensitiveInformationPolicy: CREDIT_DEBIT_CARD_NUMBER BLOCKED"] if hit else []}

    result, _, _ = _run(tmp_path, java_file, [_files(java_file)], reviews=[_PERFECT],
                        patches=[("forge.guardrails.bedrock_guardrails.BedrockGuardrails.evaluate",
                                  {"autospec": True, "side_effect": evaluate})])
    fs = result["current_file"]
    assert fs["status"] == "DONE" and not fs.get("error")
    assert fs["guardrail_post_verdict"] == "NONE"
    outputs = [text for source, text in seen if source == "OUTPUT"]
    assert outputs == [CLEAN_JAVA]


def test_a_path_with_no_reason_is_still_given_one(tmp_path, java_file):
    """The backstop: a node that stops a unit and forgets to say why."""
    def forgetful(self, state):
        return {**state, "current_file": {**state["current_file"], "status": "MANUAL_REVIEW", "error": ""}}

    result, _, _ = _run(tmp_path, java_file, [_files(java_file)],
                        patches=[("forge.agents.guardrails_post.GuardrailsPostAgent.run",
                                  {"new": forgetful})])
    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert "no recorded reason" in fs["error"]


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
