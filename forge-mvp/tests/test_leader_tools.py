"""The four gates that make a model-driven conductor safe to point at a repo.

Every test here is one of the rules in the leader contract, and each rule is in
the contract because a critique found a way through it. The order below follows
R1–R6 and R9; the marker sweep in the middle is the one that matters most,
because the thing it guards — source text, compiler output and a matched secret
reaching a model — is invisible in a passing run and only shows up in someone
else's log.

No AWS anywhere: discovery is free by contract, and every paid service call is
patched.
"""

import json
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from forge import service
from forge.decisions import Outcome
from forge.leader import cards
from forge.leader.agent import state_block
from forge.leader.convo import Conversation
from forge.leader.settings import LeaderSettings
from forge.leader.tools import TOOL_DEFS, TOOL_NAMES, ProjectContext, Toolbox, to_json, validate_call
from forge.review_queue import QUEUE_NAME
from tests.conftest import write_config

LEGACY = ("package com.corp.user;\n"
          "import javax.persistence.Entity;\n"
          "public class UserAction {}\n")


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    (base / "user").mkdir()
    (base / "user/UserAction.java").write_text(LEGACY, encoding="utf-8")
    (base / "Other.java").write_text(
        "package com.corp;\nimport javax.persistence.Entity;\npublic class Other {}\n", encoding="utf-8")
    return tmp_path / "proj"


@pytest.fixture
def ctx(project, tmp_path):
    config = write_config(tmp_path)
    return ProjectContext(source_dir=str(project), output_dir=str(tmp_path / "out"),
                          config=config, base_config=config)


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    """``set_project`` with no ``output_dir`` profiles into the repository's own
    ``.migrated`` now, but a relative ``output_dir`` still resolves against the
    working directory — which under ``pytest`` is forge-mvp, where the owner's
    real runs land. Each test here starts in its own ``tmp_path`` instead."""
    monkeypatch.chdir(tmp_path)


def _box(ctx, convo=None, *, events=None, cancel=None, **settings):
    convo = convo if convo is not None else Conversation()
    resolved = LeaderSettings.from_config(ctx.config)
    if settings:
        resolved = replace(resolved, **settings)
    sink = events.append if events is not None else (lambda event: None)
    return Toolbox(ctx, convo, resolved, sink, cancel)


def _seed(convo, packs, *, decisions=None, plan=None):
    """Pretend discovery already ran and selected these packs."""
    convo.discovery = {
        "order": list(packs),
        "activations": [{"pack": p, "complete": True, "runnable": True, "evidence": []} for p in packs],
        "decisions": dict(decisions or {"risk_ceiling": "review-high"}),
        "profile": {"java_level": "8", "build_system": "maven", "counts": {}},
    }
    if plan is not None:
        convo.discovery["intent"] = plan
    convo.selected_packs = list(packs)
    return convo


def _run_result(ctx, *, pack="javax-to-jakarta", dry_run=False, cancelled=False, acceptance=None, **totals):
    base = {"total": 2, "passed": 2, "manual": 0, "blocked": 0, "held": 0,
            "bedrock_calls": 6, "cost_usd": 0.42}
    base.update(totals)
    return service.RunResult(phase=pack, source_dir=ctx.source_dir, output_dir=ctx.output_dir,
                             dry_run=dry_run, statuses=[], totals=base, skipped=[],
                             queue={"entries": []}, paths={}, acceptance=acceptance, cancelled=cancelled)


# ─── R1: the catalogue is closed ─────────────────────────────────────────────

def test_an_unknown_tool_name_is_an_observation_and_never_an_exception(ctx):
    box = _box(ctx)
    outcome = box.execute("rm_minus_rf", {"path": "/"}, tool_id="t1")
    assert outcome.ok is False
    assert "unknown tool 'rm_minus_rf'" in outcome.observation["error"]
    assert "profile_project" in outcome.observation["error"], "the error lists what may be called instead"
    assert outcome.cards == []
    assert validate_call("rm_minus_rf", {}) is not None


def test_the_catalogue_is_closed_and_every_entry_converts_for_bedrock(ctx):
    """A bare ``{}`` reaches Bedrock as an inputSchema with no type, and the
    Converse call is then rejected on every turn — every entry is bound to every
    call, so one malformed one breaks the whole feature, not one tool.

    Increment 2 added the three the wizard used to be: ``set_project`` (the
    leader asks for the folder), ``list_artifacts`` (step 9) and
    ``land_on_branch`` (the git branch the owner asked for)."""
    assert set(TOOL_NAMES) == {
        "set_project", "profile_project", "resolve_intent", "estimate_pack", "run_pack",
        "check_acceptance", "list_held_files", "apply_review_decisions", "generate_tests",
        "pack_feedback", "list_artifacts", "build_project", "land_on_branch", "open_pull_request"}
    for spec in TOOL_DEFS:
        name = spec["name"]
        assert spec.get("description"), f"{name} has no description"
        params = spec["parameters"]
        assert params.get("type") == "object", f"{name}: parameters need an explicit type"
        assert isinstance(params.get("properties"), dict), f"{name}: parameters need a properties dict"
        missing = set(params.get("required") or []) - set(params["properties"])
        assert not missing, f"{name} requires {missing}, which it does not declare"


# ─── R2: evidence, not a sentence, decides which packs exist ─────────────────

def test_a_pack_discovery_never_selected_is_refused_before_any_service_call(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    box = _box(ctx, convo)
    with patch("forge.service.run_migration") as run:
        outcome = box.execute("run_pack", {"pack": "spring-to-spring6"}, tool_id="t1")
        run.assert_not_called()
    assert outcome.ok is False
    assert "not one of the packs discovery selected" in outcome.observation["error"]
    assert "javax-to-jakarta" in outcome.observation["error"], "it names what may be run instead"


def test_a_pack_an_intent_plan_excluded_is_no_longer_selected(ctx):
    """``activations`` still lists a pack the plan deliberately dropped, which
    is why the bound is the plan's own pack list and not the evidence."""
    convo = Conversation()
    plan = {"packs": ["javax-to-jakarta"], "states": {"javax-to-jakarta": "runnable"},
            "excluded": [{"pack": "jsp-jstl-modernize", "reason": "the user said leave the JSPs alone"}]}
    result = {"order": ["javax-to-jakarta"], "decisions": {}, "intent": plan,
              "activations": [{"pack": "javax-to-jakarta"}, {"pack": "jsp-jstl-modernize"}]}
    _box(ctx, convo)._store_discovery(result)
    assert convo.selected_packs == ["javax-to-jakarta"]


def test_the_first_pack_question_profiles_the_project_itself_rather_than_refusing(ctx):
    """Profiling is free, so a leader that has not profiled yet is given the
    evidence instead of an error it can do nothing with."""
    convo = Conversation()
    outcome = _box(ctx, convo).execute("estimate_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is True, outcome.observation
    assert convo.selected_packs and "javax-to-jakarta" in convo.selected_packs
    assert outcome.observation["units"] == 2


def test_a_selected_pack_that_is_not_runnable_today_is_still_refused(ctx):
    # A detect-only pack: recognised by discovery, never runnable. Was
    # `struts1-to-springmvc6`, which has been removed — an id that no longer
    # exists would have exercised the unknown-pack path instead.
    convo = _seed(Conversation(), ["hibernate-to-hibernate6"])
    with patch("forge.service.run_migration") as run:
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "hibernate-to-hibernate6"}, tool_id="t1")
        run.assert_not_called()
    assert outcome.ok is False
    assert "not runnable today" in outcome.observation["error"]


def test_a_scan_that_refuses_the_pack_becomes_the_message_not_a_traceback(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.utils.file_scanner.scan_java_files",
               side_effect=ValueError("Pack 'javax-to-jakarta' selects files by action_classes")):
        outcome = _box(ctx, convo).execute("estimate_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is False
    assert "selects files by action_classes" in outcome.observation["error"]


def test_a_missing_required_argument_is_answerable_rather_than_executed(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration") as run:
        outcome = _box(ctx, convo).execute("run_pack", {}, tool_id="t1")
        run.assert_not_called()
    assert outcome.ok is False and "missing 'pack'" in outcome.observation["error"]
    assert validate_call("apply_review_decisions",
                         {"run": "r1", "decisions": [{"file": "A.java", "pack": "p", "decision": "maybe"}]})


# ─── R3: the marker sweep ────────────────────────────────────────────────────

# Planted in the file bodies, the compiler output, the reviewer's feedback and
# the tail of a long `error`. It may reach a card — a diff is what a review card
# is for — and must reach no observation, no ToolMessage and not the state block.
SOURCE_MARKER = "S0URCE_MARKER_7f31"
# Planted inside a Bedrock sensitiveInformationPolicy finding, in the `match`
# member that holds the matched bytes. This one may reach NOTHING: a card is
# JSON in a transcript the server also serves back over HTTP, and the guardrail
# exists precisely to keep these bytes in one place.
SECRET_MARKER = "AKIAIOSFODNN7EXAMPLE7f31"

# `error` and `hold_reason` are capped rather than dropped, so the marker sits
# past the cap: that is the difference between a cap that works and a cap that
# only looks tidy.
_PADDING = "the transform agent returned a reason that runs on and on. " * 4


def _plant_queue(ctx, *, run="run-1", dry_run=False, status="MANUAL_REVIEW", extra=None):
    out = Path(ctx.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    entry = {
        "file_path": str(Path(ctx.source_dir) / "src/main/java/com/corp/Other.java"),
        "rel_path": "src/main/java/com/corp/Other.java",
        "pack": "javax-to-jakarta",
        "status": status,
        "generate": False,
        "risk_tier": "HIGH",
        "risk_score": 70,
        "risk_reasons": ["security configuration"],
        "review_score": 62,
        "review_verdict": "FAIL",
        "build_verdict": "FAIL",
        "original": f'class Other {{ String k = "{SOURCE_MARKER}"; }}\n',
        "transformed": {"src/main/java/com/corp/Other.java":
                        f'class Other {{ String k = "{SOURCE_MARKER}"; /* migrated */ }}\n'},
        "review_feedback": ("The transformed code does not compile. Fix these compiler errors:\n"
                            f'Other.java:1: error: cannot find symbol\n  String k = "{SOURCE_MARKER}";\n'),
        "build_output": f'Other.java:1: error: cannot find symbol\n  String k = "{SOURCE_MARKER}";\n',
        "guardrail_findings": [
            "sensitiveInformationPolicy: {'piiEntities': [{'match': '" + SECRET_MARKER +
            "', 'type': 'AWS_ACCESS_KEY', 'action': 'BLOCKED'}]}",
            "AWS access key id at line 1",
        ],
        "error": _PADDING + SOURCE_MARKER,
        "hold_reason": _PADDING + SOURCE_MARKER,
        "deleted_files": [],
    }
    entry.update(extra or {})
    queue = {"version": 2, "run": run, "phase": "javax-to-jakarta", "dry_run": dry_run,
             "source_dir": ctx.source_dir, "output_dir": ctx.output_dir, "entries": [entry]}
    (out / QUEUE_NAME).write_text(json.dumps(queue, indent=2), encoding="utf-8")
    return queue


class _Acceptance:
    """An AcceptanceOutcome stand-in whose evidence is matched source text."""

    def to_json(self):
        return {
            "verdict": "FAIL", "passed": 1, "failed": 1, "skipped": 0, "path": None,
            "results": [
                {"pack": "javax-to-jakarta", "kind": "no_match", "value": "javax\\.", "scope": "java",
                 "outcome": "fail", "detail": "2 hit(s)",
                 "evidence": [f"src/main/java/com/corp/Other.java:1: {SOURCE_MARKER}"]},
                {"pack": "javax-to-jakarta", "kind": "build", "value": "mvn -q", "scope": "module",
                 "outcome": "pass", "detail": "", "evidence": []},
            ],
        }


def _sweep(outcome, *, where):
    """Every route from a tool result to a model, checked in one place."""
    from langchain_core.messages import ToolMessage

    observation = to_json(outcome.observation)
    assert SOURCE_MARKER not in observation, f"{where}: source text reached the observation"
    assert SECRET_MARKER not in observation, f"{where}: a matched secret reached the observation"

    message = ToolMessage(content=to_json(outcome.observation), tool_call_id="t1",
                          status="success" if outcome.ok else "error")
    assert SOURCE_MARKER not in str(message.content), f"{where}: source text reached a ToolMessage"
    assert SECRET_MARKER not in str(message.content), f"{where}: a matched secret reached a ToolMessage"

    rendered = json.dumps(outcome.cards, default=str)
    assert SECRET_MARKER not in rendered, f"{where}: a matched secret reached a browser card"
    return observation, rendered


def test_no_source_text_and_no_matched_secret_ever_reaches_the_model(ctx):
    """R3, swept across every field that carries file bytes.

    ``original``/``transformed`` are the file; ``build_output`` is javac, which
    prints the offending source line under every error; ``review_feedback`` is
    overwritten with that same output on a build failure; acceptance
    ``evidence`` is 80 characters of matched source; a Bedrock
    ``sensitiveInformationPolicy`` finding embeds the credential it caught. Each
    one has its own reducer, and this is the test that notices when a tenth
    field arrives without one.
    """
    _plant_queue(ctx)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    box = _box(ctx, convo)

    held = box.execute("list_held_files", {}, tool_id="t1")
    assert held.ok is True
    observation, rendered = _sweep(held, where="list_held_files")

    entry = held.observation["entries"][0]
    assert "review_feedback" not in entry, "feedback is compiler output on a build failure"
    assert entry["has_feedback"] is True and entry["feedback_kind"] == "build"
    assert entry["guardrail_findings"] == {
        "count": 2, "kinds": ["sensitiveInformationPolicy", "AWS access key id at line 1"]}
    assert len(entry["error"]) == 200 and len(entry["hold_reason"]) == 200

    card = held.cards[0]
    assert SOURCE_MARKER in card["diff"], "the review card is the one place the diff belongs"
    assert SOURCE_MARKER in card["review_feedback"]
    assert card["guardrail_findings"] == ["sensitiveInformationPolicy", "AWS access key id at line 1"], (
        "a card is transcript JSON too — the matched bytes are reduced there as well")

    with patch("forge.service.acceptance", return_value=_Acceptance()):
        accepted = box.execute("check_acceptance", {"pack": "javax-to-jakarta"}, tool_id="t2")
    assert accepted.ok is True
    _sweep(accepted, where="check_acceptance")
    assert accepted.observation["results"][0]["evidence_count"] == 1
    assert "evidence" not in accepted.observation["results"][0]
    assert SOURCE_MARKER in json.dumps(accepted.cards), "the acceptance card keeps its evidence"

    with patch("forge.service.run_migration",
               return_value=_run_result(ctx, acceptance=_Acceptance(), manual=1)):
        ran = box.execute("run_pack", {"pack": "javax-to-jakarta", "acceptance": True}, tool_id="t3")
    assert ran.ok is True
    _sweep(ran, where="run_pack")
    assert ran.observation["acceptance"]["failed"] == 1, "run_pack reduces acceptance through the same reducer"

    block = state_block(convo, ctx)
    assert SOURCE_MARKER not in block and SECRET_MARKER not in block
    assert "MANUAL_REVIEW 1" in block, "the state block still says a file is waiting"


def test_a_blocked_entry_never_gets_a_diff_and_a_generated_one_never_crashes(ctx):
    """A BLOCKED unit's ``original`` is the file the secret gate refused, and a
    generated unit has no original at all — the formula that builds a diff has
    to survive both without discarding the run that produced them."""
    blocked_original = "BL0CKED_ORIGINAL_7f31"
    _plant_queue(ctx, status="BLOCKED", extra={
        "original": f'class Other {{ String pw = "{blocked_original}"; }}\n',
        "review_feedback": None, "build_output": None, "build_verdict": None,
        "error": "blocked by the local secret scan", "hold_reason": None})
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    blocked = _box(ctx, convo).execute("list_held_files", {}, tool_id="t1")
    assert blocked.cards[0]["diff"] is None, "a blocked original is not a diff, it is the file"
    assert blocked_original not in json.dumps(blocked.cards)
    assert blocked_original not in to_json(blocked.observation)

    _plant_queue(ctx, status="HELD", extra={"generate": True, "original": None,
                                            "transformed": {"server.xml": "<server/>"}})
    generated = _box(ctx, _seed(Conversation(), ["javax-to-jakarta"])).execute("list_held_files", {}, tool_id="t2")
    assert generated.ok is True and generated.cards[0]["diff"] is None

    _plant_queue(ctx, status="MANUAL_REVIEW", extra={"transformed": {}})
    empty = _box(ctx, _seed(Conversation(), ["javax-to-jakarta"])).execute("list_held_files", {}, tool_id="t3")
    assert empty.ok is True and empty.cards[0]["diff"] is None, "a transform that produced nothing has no diff"


def test_a_blocked_file_tells_the_leader_why_and_what_to_change_without_the_secret(ctx):
    """The leader told a user to "approve, reject or retry" a BLOCKED server.xml
    (#24). The observation now names the cause from the verdict guardrails_pre
    recorded, with a fixed sentence on what the user can change — and still
    never the matched bytes."""
    _plant_queue(ctx, status="BLOCKED", extra={
        "guardrail_pre_verdict": "SECRET_BLOCKED_LOCALLY", "error": "Local secret scan: AWS access key id at line 1",
        "review_score": None, "review_verdict": None, "build_verdict": None, "review_feedback": None,
        "build_output": None, "hold_reason": None, "transformed": {}})
    held = _box(ctx, _seed(Conversation(), ["javax-to-jakarta"])).execute("list_held_files", {}, tool_id="t1")
    _sweep(held, where="list_held_files (BLOCKED)")
    entry = held.observation["entries"][0]
    assert entry["blocked_by"] == "secret_scan" and "secret_scan.allow" in entry["unblock"]
    assert "approve" not in entry["unblock"]
    assert held.cards[0]["unblock"] == entry["unblock"], "the card says the same thing to the person"

    for verdict, error, cause in [("TOO_LARGE", "2400 lines exceeds complexity_block_threshold of 2000", "too_large"),
                                  ("GUARDRAIL_INTERVENED", None, "guardrail"),
                                  (None, "Cannot read file: [Errno 13] Permission denied", "unreadable"),
                                  ("NONE", "touches a vendor API the pack cannot migrate", "preflight_check"),
                                  (None, None, "unknown")]:
        obs = cards.entry_obs({"status": "BLOCKED", "guardrail_pre_verdict": verdict, "error": error})
        assert obs["blocked_by"] == cause and obs["unblock"], cause
    assert "blocked_by" not in cards.entry_obs({"status": "HELD", "guardrail_pre_verdict": "NONE"})
    assert cards.review_file_card({"status": "HELD"})["unblock"] is None


def test_the_leader_prompt_never_offers_to_approve_a_blocked_file():
    from forge.leader.agent import _SYSTEM

    rule = _SYSTEM[_SYSTEM.index("A BLOCKED file"):]
    assert "nothing to approve" in rule and "never offer" in rule
    assert "blocked_by" in rule and "unblock" in rule and "secret_scan.allow" in rule


def test_a_dry_run_preview_is_not_a_file_waiting_on_a_human(ctx):
    """A dry run queues every unit it would have transformed, DONE ones
    included. Turning those into review cards would offer an approve button
    that writes a file the user was only previewing."""
    _plant_queue(ctx, dry_run=True, status="DONE")
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    held = _box(ctx, convo).execute("list_held_files", {}, tool_id="t1")
    assert held.observation["count"] == 0 and held.cards == []


def test_an_unreadable_review_queue_is_a_state_not_a_failure(ctx):
    """A version-1 queue left in an output directory used to fail every turn,
    with no tool the leader could call to fix it."""
    out = Path(ctx.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / QUEUE_NAME).write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    outcome = _box(ctx, convo).execute("list_held_files", {}, tool_id="t1")
    assert outcome.ok is True and outcome.observation["count"] == 0
    assert "version-2" in outcome.observation["message"]
    assert "unknown" in state_block(convo, ctx), "the state block reports it rather than raising"


# ─── R4: money asks first ────────────────────────────────────────────────────

def test_a_run_over_the_ceiling_parks_a_confirmation_and_spends_nothing(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    box = _box(ctx, convo, confirm_above_usd=0.05)
    with patch("forge.service.run_migration") as run:
        outcome = box.execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        run.assert_not_called()
    assert outcome.ok is True and outcome.needs_confirmation is True
    assert outcome.observation["status"] == "needs_confirmation"
    assert outcome.pending_id and convo.pending[outcome.pending_id]["tool"] == "run_pack"
    card = outcome.cards[0]
    assert card["kind"] == "confirm" and card["est_usd"] == 0.14 and card["units"] == 2
    assert card["args"] == {"pack": "javax-to-jakarta"}
    assert "Confirm" in outcome.observation["note"]


def test_a_run_under_the_ceiling_just_runs(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)) as run:
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        assert run.call_count == 1
    assert outcome.ok is True and outcome.needs_confirmation is False
    assert outcome.observation["status"] == "done"
    assert convo.completed == ["javax-to-jakarta"] and convo.spend_usd == 0.42


def test_setting_confirm_above_usd_to_zero_is_the_owner_turning_the_gate_off(ctx):
    """Zero means 'never ask', which is a deliberate setting and not a missing
    one — so it must not be coerced back to the default."""
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    box = _box(ctx, convo, confirm_above_usd=0.0, unit_cost_usd=100.0)
    assert box.estimate("run_pack", {"pack": "javax-to-jakarta"}) == 200.0
    with patch("forge.service.run_migration", return_value=_run_result(ctx)) as run:
        outcome = box.execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        assert run.call_count == 1
    assert outcome.needs_confirmation is False and convo.pending == {}


def test_there_is_no_dry_run_in_the_chat(ctx):
    """The owner removed dry runs: the tool does not offer one, and a stale
    dry_run argument is ignored rather than obeyed."""
    run_pack = next(t for t in TOOL_DEFS if t["name"] == "run_pack")
    assert "dry_run" not in run_pack["parameters"]["properties"]
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)) as run:
        outcome = _box(ctx, convo, confirm_above_usd=0.0).execute(
            "run_pack", {"pack": "javax-to-jakarta", "dry_run": True}, tool_id="t1")
        assert run.call_args.kwargs["dry_run"] is False
    assert outcome.observation["dry_run"] is False and convo.completed == ["javax-to-jakarta"]


def test_a_run_says_which_model_did_the_transform(ctx):
    """A trial run must never be mistaken for a full one."""
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)):
        outcome = _box(ctx, convo, confirm_above_usd=0.0).execute(
            "run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    model = ctx.config.transform_model
    assert outcome.observation["transform_model"] == model
    assert model.split(".", 2)[-1] in outcome.summary


def test_spend_reaches_the_chat_per_unit_and_is_not_counted_twice(ctx):
    """The rail showed "pipeline $0.000" through a whole run, and kept it when a
    run died after paying for most of its units."""
    events = []
    convo = _seed(Conversation(), ["javax-to-jakarta"])

    def fake_run(*args, on_event=None, **kwargs):
        on_event({"type": "file", "index": 1, "total": 2, "cost_usd": 0.2})
        assert convo.spend_usd == 0.2, "spend lands while the run is still going"
        on_event({"type": "file", "index": 2, "total": 2, "cost_usd": 0.2})
        return _run_result(ctx)   # its total is 0.42: 0.02 the units did not report

    with patch("forge.service.run_migration", side_effect=fake_run):
        _box(ctx, convo, events=events, confirm_above_usd=0.0).execute(
            "run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert convo.spend_usd == 0.42
    usage = [e for e in events if e.get("type") == "usage"]
    assert [u["spend_usd"] for u in usage] == [0.2, 0.4] and all("via" not in u for u in usage)


def test_spend_already_incurred_survives_a_run_that_dies(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])

    def dies(*args, on_event=None, **kwargs):
        on_event({"type": "file", "index": 1, "total": 2, "cost_usd": 0.07})
        raise RuntimeError("ReadTimeoutError")

    with patch("forge.service.run_migration", side_effect=dies):
        outcome = _box(ctx, convo, confirm_above_usd=0.0).execute(
            "run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is False and convo.spend_usd == 0.07


# ─── R5: approval is the human's, at any price ───────────────────────────────

def test_applying_review_decisions_always_needs_a_click_even_when_it_is_free(ctx):
    _plant_queue(ctx)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    box = _box(ctx, convo, confirm_above_usd=0.0)
    args = {"run": "run-1", "decisions": [{"file": "src/main/java/com/corp/Other.java",
                                           "pack": "javax-to-jakarta", "decision": "approve"}]}
    with patch("forge.service.apply") as apply:
        outcome = box.execute("apply_review_decisions", args, tool_id="t1")
        apply.assert_not_called()
    assert outcome.needs_confirmation is True, "the gate is off and it still asks"
    assert box.estimate("apply_review_decisions", args) == 0.0
    card = outcome.cards[0]
    assert card["kind"] == "confirm" and card["decisions"] == args["decisions"], (
        "the click signs these decisions, so the card has to show them in full")


def test_a_confirmed_apply_reaches_the_service_and_reports_capped_detail(ctx):
    _plant_queue(ctx)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    result = service.ApplyResult(
        outcomes=[Outcome(file="src/main/java/com/corp/Other.java", decision="approve", applied=True,
                          status_after="DONE", detail=_PADDING + SOURCE_MARKER)],
        remaining=[], queue_after=None, log_path=None, all_applied=True)
    args = {"run": "run-1", "decisions": [{"file": "src/main/java/com/corp/Other.java",
                                           "pack": "javax-to-jakarta", "decision": "approve"}]}
    with patch("forge.service.apply", return_value=result) as apply:
        outcome = _box(ctx, convo).execute("apply_review_decisions", args, tool_id="t1", confirmed=True)
        assert apply.call_count == 1
    assert outcome.ok is True
    row = outcome.observation["outcomes"][0]
    assert row["applied"] is True and len(row["detail"]) == 200
    assert SOURCE_MARKER not in to_json(outcome.observation), "an apply detail quotes the file too"


def test_a_decision_from_an_older_queue_never_reaches_the_service(ctx):
    """The queue is one file per output directory, rewritten by every run, and
    cards live forever in the transcript. Approving one from run 1 against
    run 2's queue promotes a transform the human never saw."""
    _plant_queue(ctx, run="run-2")
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    args = {"run": "run-1", "decisions": [{"file": "src/main/java/com/corp/Other.java",
                                           "pack": "javax-to-jakarta", "decision": "approve"}]}
    with patch("forge.service.apply") as apply:
        outcome = _box(ctx, convo).execute("apply_review_decisions", args, tool_id="t1", confirmed=True)
        apply.assert_not_called()
    assert outcome.ok is False
    assert "the review queue changed" in outcome.observation["error"]


def test_a_decision_naming_only_the_basename_is_rejected_not_guessed(ctx):
    """``find_entry`` falls back to a unique basename match, which would approve
    a same-named file in another directory — a diff nobody looked at."""
    _plant_queue(ctx)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    args = {"run": "run-1", "decisions": [{"file": "Other.java", "pack": "javax-to-jakarta",
                                           "decision": "approve"}]}
    with patch("forge.service.apply") as apply:
        outcome = _box(ctx, convo).execute("apply_review_decisions", args, tool_id="t1", confirmed=True)
        apply.assert_not_called()
    assert outcome.ok is False
    assert outcome.observation["rejected"][0]["reason"].startswith("not in the current review queue")


def test_a_decision_naming_the_wrong_pack_is_rejected(ctx):
    _plant_queue(ctx)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    args = {"run": "run-1", "decisions": [{"file": "src/main/java/com/corp/Other.java",
                                           "pack": "spring-to-spring6", "decision": "approve"}]}
    with patch("forge.service.apply") as apply:
        outcome = _box(ctx, convo).execute("apply_review_decisions", args, tool_id="t1", confirmed=True)
        apply.assert_not_called()
    assert outcome.ok is False
    assert "held by pack 'javax-to-jakarta'" in outcome.observation["rejected"][0]["reason"]


# ─── R6: nothing here raises ─────────────────────────────────────────────────

def test_a_service_that_raises_becomes_an_observation(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", side_effect=RuntimeError("boom")):
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is False and outcome.observation["error"] == "RuntimeError: boom"
    assert convo.completed == [] and convo.spend_usd == 0.0


def test_nothing_to_do_is_a_result_rather_than_a_failure(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", side_effect=service.NoEligibleFiles("No eligible files")):
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is True and outcome.observation["status"] == "nothing"


def test_a_card_that_will_not_render_never_discards_a_paid_run(ctx):
    """The run is bought and the files are written. Reporting a rendering bug as
    a failed run is how the model is talked into running it a second time."""
    _plant_queue(ctx)
    # A pack still to run, so the plan stays open and no build card follows.
    convo = _seed(Conversation(), ["javax-to-jakarta", "java21"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch.object(cards, "review_file_card", side_effect=KeyError("transformed")):
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is True, "a paid run is never thrown away by a diff"
    assert outcome.cards == [] and "card_error" in outcome.summary
    assert outcome.observation["totals"]["passed"] == 2
    assert convo.completed == ["javax-to-jakarta"] and convo.spend_usd == 0.42


def test_a_tool_is_not_entered_once_stop_has_been_pressed(ctx):
    """``run_migration`` with cancel already set is not a no-op: it marks every
    file PENDING, breaks at unit one, and then overwrites the review queue with
    an empty one — the previous pack's held files stop being appliable."""
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    cancel = threading.Event()
    cancel.set()
    with patch("forge.service.run_migration") as run:
        outcome = _box(ctx, convo, cancel=cancel).execute("run_pack", {"pack": "javax-to-jakarta"},
                                                          tool_id="t1")
        run.assert_not_called()
    assert outcome.ok is False and outcome.observation["status"] == "cancelled"


def test_the_relay_stamps_service_events_and_never_forwards_a_terminal_one(ctx):
    """``done`` and ``error`` belong to the job registry. Relaying a tool's own
    would close the browser's EventSource halfway through the turn."""
    # A pack still to run, so no project build adds its own events.
    convo = _seed(Conversation(), ["javax-to-jakarta", "java21"])
    events = []

    def run(*args, **kwargs):
        on_event = kwargs["on_event"]
        on_event({"type": "start", "phase": "javax-to-jakarta", "files": 2})
        on_event({"type": "done", "state": "done"})
        on_event({"type": "error", "error": "nope"})
        on_event("not a dict at all")
        return _run_result(ctx)

    with patch("forge.service.run_migration", side_effect=run):
        _box(ctx, convo, events=events).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t9")
    assert [e["type"] for e in events] == ["start"]
    assert events[0]["via"] == "tool" and events[0]["tool_id"] == "t9"
    assert events[0]["phase"] == "javax-to-jakarta", "the event's own fields are untouched"


# ─── the end of a plan builds the project (#21) ──────────────────────────────

COMPILER_MARKER = "C0MPILER_LINE_7f31"


def _build_record(outcome="fail"):
    return {"outcome": outcome, "detail": "mvn install failed (exit 1)", "failed_step": "mvn install",
            "tail": [f"[ERROR] Other.java:[1,5] {COMPILER_MARKER}"], "java_home": None, "seconds": 3.0,
            "steps": ["mvn install"], "built_at": "2026-09-23T10:00:00+00:00"}


def test_the_last_pack_of_the_plan_builds_the_project_without_being_asked(ctx):
    """The first ten-pack run went from the last pack to the review queue and
    offered landing unbuilt, because only the prompt said to build."""
    convo = _seed(Conversation(), ["javax-to-jakarta", "java21"])
    box = _box(ctx, convo, confirm_above_usd=0.0)
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch("forge.service.build_project", return_value=_build_record()) as build:
        first = box.execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        build.assert_not_called()
        assert first.observation["plan_complete"] is False and "build" not in first.observation

        last = box.execute("run_pack", {"pack": "java21"}, tool_id="t2")
        assert build.call_count == 1

    args = build.call_args
    assert args.args[:2] == (ctx.source_dir, ctx.output_dir), "the same call build_project makes"
    assert last.ok is True and last.observation["plan_complete"] is True
    assert last.observation["build"] == {"outcome": "fail", "stale": False, "detail": "mvn install failed (exit 1)",
                                         "failed_step": "mvn install", "built_at": "2026-09-23T10:00:00+00:00"}
    assert COMPILER_MARKER not in to_json(last.observation), "the leader sees the verdict, never javac"
    assert last.cards[-1]["kind"] == "build" and COMPILER_MARKER in json.dumps(last.cards[-1])
    assert "build: fail" in last.summary
    assert convo.completed == ["javax-to-jakarta", "java21"]


def test_a_build_that_dies_never_discards_the_last_paid_run(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch("forge.service.build_project", side_effect=OSError("disk full")):
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert outcome.ok is True and outcome.observation["status"] == "done"
    assert outcome.observation["plan_complete"] is True
    assert outcome.observation["build_error"] == "OSError: disk full"
    assert convo.completed == ["javax-to-jakarta"] and convo.spend_usd == 0.42


def test_a_pack_with_nothing_to_do_or_that_cannot_run_never_holds_the_plan_open(ctx):
    """A plan that ends on an empty pack is still finished, and a selected pack
    that is not runnable today can never be run to finish it."""
    convo = _seed(Conversation(), ["javax-to-jakarta", "not-a-runnable-pack", "java21"])
    box = _box(ctx, convo)
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch("forge.service.build_project", return_value=_build_record("pass")) as build:
        box.execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        build.assert_not_called()
    with patch("forge.service.run_migration", side_effect=service.NoEligibleFiles("No eligible files")), \
         patch("forge.service.build_project", return_value=_build_record("pass")) as build:
        empty = box.execute("run_pack", {"pack": "java21"}, tool_id="t2")
        assert build.call_count == 1
    assert empty.observation["status"] == "nothing" and empty.observation["plan_complete"] is True
    assert empty.observation["build"]["outcome"] == "pass" and empty.cards[-1]["kind"] == "build"
    assert convo.completed == ["javax-to-jakarta"] and convo.nothing_to_do == ["java21"]


def test_a_plan_where_nothing_ran_or_a_stopped_run_builds_nothing(ctx):
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", side_effect=service.NoEligibleFiles("No eligible files")), \
         patch("forge.service.build_project") as build:
        outcome = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        build.assert_not_called()
    assert outcome.observation["plan_complete"] is True, "nothing was written, so there is nothing to build"

    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx, cancelled=True)), \
         patch("forge.service.build_project") as build:
        stopped = _box(ctx, convo).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t2")
        build.assert_not_called()
    assert stopped.observation["status"] == "cancelled" and "plan_complete" not in stopped.observation


# ─── R9: the leader cannot lower the hold gate ───────────────────────────────

def test_an_intent_plan_narrows_the_scope_a_run_uses_but_never_the_review_gate(ctx, tmp_path):
    """Scope from a plan is written to a file and read by nothing: without this
    merge, 'migrate to Tomcat, skip com.legacy' plans one thing and runs
    another in the same turn. ``risk_ceiling`` is the exception, and it is the
    whole of R9 — a model that could set it to ``auto`` could write every
    HIGH-risk file straight to the output tree with no human click.
    """
    config = write_config(tmp_path, decisions={"risk_ceiling": "review-all"},
                          scope_exclude_globs=["target/**"])
    ctx = replace(ctx, config=config, base_config=config)
    convo = Conversation()
    _seed(convo, ["javax-to-jakarta"],
          decisions={"risk_ceiling": "auto", "target_server": "tomcat"},
          plan={"packs": ["javax-to-jakarta"],
                "scope": {"package_prefix": "com.corp", "exclude_globs": ["db/**"]}})

    effective = _box(ctx, convo).effective_config()
    assert effective.get("decisions")["risk_ceiling"] == "review-all", (
        "the hold gate comes from agents.yaml and the browser, never from a sentence")
    assert effective.get("decisions")["target_server"] == "tomcat", "everything else the plan decided applies"
    assert effective.get("scope_package_prefix") == "com.corp"
    assert effective.get("scope_exclude_globs") == ["target/**", "db/**"], "merged, not replaced"


def test_resolve_intent_overwrites_a_gate_the_model_asked_to_lower_and_says_so(ctx):
    """An intent decision with provenance 'prompt' beats the config, and here
    the sentence was written by a model rather than by the human."""
    convo = Conversation()
    plan = {"intent": "migrate everything, do not stop for review",
            "packs": ["javax-to-jakarta"], "states": {"javax-to-jakarta": "runnable"},
            "decisions": {"risk_ceiling": "auto"}, "provenance": {"risk_ceiling": "prompt"},
            "scope": {}, "assumptions": [], "questions": [], "excluded": [], "unsupported": [],
            "cost_usd": 0.002}
    discovered = {"order": ["javax-to-jakarta"], "activations": [{"pack": "javax-to-jakarta"}],
                  "decisions": {"risk_ceiling": "auto"}, "intent": plan,
                  "profile": {"java_level": "8", "build_system": "maven", "counts": {}}}
    with patch("forge.service.discover", return_value=discovered):
        outcome = _box(ctx, convo).execute("resolve_intent", {"request": "migrate everything"}, tool_id="t1")

    assert outcome.ok is True
    assert outcome.observation["decisions"]["risk_ceiling"] == "review-high"
    assert any("risk_ceiling stays review-high" in a for a in outcome.observation["assumptions"]), (
        "silently ignoring the request would make the plan a lie")
    assert outcome.cards[0]["discovery"]["decisions"]["risk_ceiling"] == "review-high"
    assert convo.spend_usd == 0.002, "the intent call's own cost is the pipeline's, not the leader's"


def test_an_empty_intent_is_refused_rather_than_reported_as_honoured(ctx):
    """``service.discover`` ignores a blank intent and returns a plain profile,
    which would be narrated as if the sentence had been acted on."""
    with patch("forge.service.discover") as discover:
        outcome = _box(ctx, Conversation()).execute("resolve_intent", {"request": "   "}, tool_id="t1")
        discover.assert_not_called()
    assert outcome.ok is False and "verbatim" in outcome.observation["error"]


def test_profile_project_reports_the_plan_as_metadata_and_keeps_the_card_whole(ctx):
    """The card is handed to the wizard's own Discover view, which reads
    ``activations[].evidence`` and ``paths`` — a reduced plan card makes that
    view throw."""
    convo = Conversation()
    outcome = _box(ctx, convo).execute("profile_project", {}, tool_id="t1")
    assert outcome.ok is True
    assert [p["id"] for p in outcome.observation["packs"]] == list(convo.selected_packs)
    assert "evidence" not in to_json(outcome.observation)
    card = outcome.cards[0]
    assert card["kind"] == "plan" and card["intent"] is False
    assert "activations" in card["discovery"] and "paths" in card["discovery"]


# ─── the leader asks for the project ─────────────────────────────────────────

# Every tool bar `set_project` acts on a repository, so every one of them has to
# answer "which folder?" the same way. The args are the minimum each tool needs
# to get past schema validation, because validation runs before the handler and
# a missing argument would hide the check this test is about. A new tool has to
# be added here deliberately — that is the point of the assertion below.
_MINIMAL_ARGS = {
    "profile_project": {},
    "resolve_intent": {"request": "get me onto jakarta"},
    "estimate_pack": {"pack": "javax-to-jakarta"},
    "run_pack": {"pack": "javax-to-jakarta"},
    "check_acceptance": {"pack": "javax-to-jakarta"},
    "list_held_files": {},
    "apply_review_decisions": {"run": "run-1", "decisions": [
        {"file": "src/main/java/A.java", "pack": "javax-to-jakarta", "decision": "approve"}]},
    "generate_tests": {},
    "pack_feedback": {},
    "list_artifacts": {},
    "build_project": {},
    "land_on_branch": {"branch": "forge/jakarta"},
    "open_pull_request": {},
}


def _unbound(config):
    """A context for a chat that has not been told where the repository is.

    ``bound`` is derived from an empty ``source_dir`` today, and setting it here
    as well is deliberate: this helper says what the test means — a chat nobody
    has told where the repository is — rather than relying on a derivation that
    could reasonably change.
    """
    ctx = ProjectContext(source_dir="", output_dir="", config=config, base_config=config)
    ctx.bound = False
    return ctx


def test_set_project_on_a_folder_that_is_not_there_says_so_and_binds_nothing(ctx, tmp_path):
    """The user typed a path from memory. The refusal has to echo it back, or
    they cannot see the typo — this is the first thing the chat ever does."""
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)
    missing = str(tmp_path / "no-such-repository")

    outcome = box.execute("set_project", {"source_dir": missing}, tool_id="t1")

    assert outcome.ok is False
    assert missing in outcome.observation["error"]
    assert convo.source_dir == "", "a path that is not there must not bind the conversation"
    assert convo.discovery is None


def test_set_project_binds_the_conversation_and_profiles_it_in_the_same_breath(ctx, project):
    """Profiling is free, so making the user ask for it separately would be two
    turns and a model call to say nothing. The reply confirms what was found."""
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)

    # A path with a `.` segment in it: the conversation is bound to the resolved
    # form, or the same repository reached two ways looks like two projects.
    outcome = box.execute("set_project", {"source_dir": str(project / "src" / "..")}, tool_id="t1")

    assert outcome.ok is True, outcome.observation
    resolved = str(Path(str(project)).resolve())
    assert convo.source_dir == resolved
    assert outcome.observation["source_dir"] == resolved
    assert outcome.observation["output_dir"], "a project with no output directory can never run a pack"
    # Pinned to the profiler's own answer rather than to this fixture: the point
    # is that the profile reaches the reply, so the leader can say what it found
    # instead of asking the user to run a separate step.
    from forge.discover import build_profile

    profile = build_profile(resolved).to_json()
    assert outcome.observation["build_system"] == profile["build_system"]
    assert outcome.observation["java_level"] == profile["java_level"]
    assert "modules" in outcome.observation and "counts" in outcome.observation
    assert [p["id"] for p in outcome.observation["packs"]] == list(convo.selected_packs), (
        "set_project carries the profile_project observation, so the leader can propose work at once")
    assert convo.selected_packs, "a maven project with javax imports activates at least one pack"
    assert outcome.cards and outcome.cards[0]["kind"] == "plan"


def test_set_project_with_no_output_dir_writes_into_the_repositorys_own_migrated_folder(ctx, project):
    """The owner's call: the migration lives inside the repository it migrates,
    at ``<repo>/.migrated`` — and FORGE never reads that folder back as source."""
    from forge.leader.tools import DEFAULT_OUTPUT_DIR
    from forge.utils.fs import FORGE_OUTPUT_DIR_NAMES

    assert DEFAULT_OUTPUT_DIR in FORGE_OUTPUT_DIR_NAMES, "every source walk has to prune the default"
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)

    first = box.execute("set_project", {"source_dir": str(project)}, tool_id="t1")

    resolved = Path(str(project)).resolve()
    assert first.ok is True, first.observation
    assert first.observation["output_dir"] == str(resolved / ".migrated")
    assert convo.output_dir == str(resolved / ".migrated") == box.ctx.output_dir
    assert (resolved / ".migrated" / "forge-profile.yaml").is_file(), "discovery wrote into the repository's folder"

    # Profiling again reads the same repository: the folder it just wrote is not source.
    again = box.execute("profile_project", {}, tool_id="t2")
    assert again.observation["counts"] == first.observation["counts"]


def test_a_named_output_dir_still_wins_over_the_default(ctx, project, tmp_path):
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)
    outcome = box.execute("set_project", {"source_dir": str(project), "output_dir": str(tmp_path / "elsewhere")},
                          tool_id="t1")
    assert outcome.observation["output_dir"] == str(tmp_path / "elsewhere")


def test_a_second_different_project_in_the_same_chat_is_refused_and_the_first_one_stands(ctx, project, tmp_path):
    """R2 evidence, the completed packs and every parked estimate describe one
    repository. Re-binding would let discovery on A authorise a paid run on B."""
    other = tmp_path / "other-repo"
    (other / "src/main/java").mkdir(parents=True)
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)
    assert box.execute("set_project", {"source_dir": str(project)}, tool_id="t1").ok is True

    outcome = box.execute("set_project", {"source_dir": str(other)}, tool_id="t2")

    assert outcome.ok is False
    assert "new chat" in outcome.observation["error"], outcome.observation["error"]
    assert convo.source_dir == str(Path(str(project)).resolve()), "the first project still holds"


def test_setting_the_project_is_free_and_never_waits_for_a_click(ctx, project):
    """Discovery makes no model call and no AWS call. A confirmation card here
    would put a button between the user and the first useful sentence."""
    convo = Conversation()
    box = Toolbox(_unbound(ctx.config), convo, LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)

    outcome = box.execute("set_project", {"source_dir": str(project)}, tool_id="t1")

    assert outcome.needs_confirmation is False and outcome.pending_id is None
    assert convo.pending == {}
    assert box.estimate("set_project", {"source_dir": str(project)}) == 0.0


def test_every_tool_that_needs_a_repository_asks_for_one_instead_of_guessing(ctx):
    """The string is the mechanism, not decoration.

    With no project bound, a tool that returned "not found" or an empty result
    would have the leader narrate an empty repository; the observation has to
    tell it what to do, which is ask the user which folder they mean. Every call
    here is `confirmed`, so the spend gate cannot answer first and hide a tool
    that never checks.
    """
    covered = set(_MINIMAL_ARGS) | {"set_project"}
    assert set(TOOL_NAMES) == covered, (
        f"the catalogue changed: {sorted(set(TOOL_NAMES) ^ covered)}. A new tool either needs a "
        "project — add it here — or is a second way to set one.")

    box = Toolbox(_unbound(ctx.config), Conversation(), LeaderSettings.from_config(ctx.config),
                  lambda event: None, None)
    for name, args in _MINIMAL_ARGS.items():
        with patch("forge.service.discover") as discover, patch("forge.service.run_migration") as run:
            outcome = box.execute(name, args, tool_id="t1", confirmed=True)
            discover.assert_not_called()
            run.assert_not_called()
        assert outcome.ok is False, f"{name} acted on a project nobody named: {outcome.observation}"
        assert "no project set yet" in outcome.observation["error"], \
            f"{name} said {outcome.observation['error']!r} — the leader cannot act on that"


def test_an_unbound_state_block_names_set_project_and_reads_no_queue(ctx, tmp_path, monkeypatch):
    """Two failures used to hide in the first turn of every chat.

    ``_queue_line`` with an empty ``output_dir`` calls ``load_queue("")``, which
    resolves to ``./manual-review-queue.json`` — whatever is in the server's
    working directory, which is some other project's run. And the PLAN section
    used to send an unbound leader to ``profile_project``, a tool that can only
    refuse until a folder is named. ``set_project`` profiles as it binds, so it
    is the only move there is.
    """
    stray = tmp_path / "cwd"
    stray.mkdir()
    (stray / QUEUE_NAME).write_text(
        json.dumps({"version": 2, "run": "someone-elses", "entries": []}), encoding="utf-8")
    monkeypatch.chdir(stray)

    unbound = ProjectContext(source_dir="", output_dir="", config=ctx.config,
                             base_config=ctx.base_config, bound=False)
    block = state_block(Conversation(), unbound)

    assert "set_project" in block and "no project set yet" in block
    assert "someone-elses" not in block, "a queue in the server's cwd is not this chat's"
    assert "profile_project" not in block, "profile_project can only refuse while unbound"


# ─── unit tests by default at the end of the plan (test_generation.after_plan) ─

class _TestGenResult:
    def __init__(self, generated=3):
        self.generated = generated

    def to_json(self):
        return {"totals": {"generated": self.generated, "held": 0, "cost_usd": 0.12}, "dependencies": [],
                "skipped": 0, "cancelled": False, "dry_run": False, "style": "junit5"}


def _tests_ctx(project, tmp_path, after_plan=True):
    config = write_config(tmp_path, test_generation={"enabled": True, "after_plan": after_plan})
    return ProjectContext(source_dir=str(project), output_dir=str(tmp_path / "out"),
                          config=config, base_config=config)


def test_under_the_spend_limit_the_plan_writes_tests_then_builds_once(project, tmp_path):
    ctx = _tests_ctx(project, tmp_path)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    order = []
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch("forge.service.generate_tests", side_effect=lambda *a, **k: order.append("tests") or _TestGenResult()), \
         patch("forge.service.build_project", side_effect=lambda *a, **k: order.append("build") or _build_record("pass")):
        last = _box(ctx, convo, confirm_above_usd=0.0).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    assert order == ["tests", "build"], "one build, and it compiles the new tests"
    assert last.needs_confirmation is False
    assert last.observation["tests"]["totals"]["generated"] == 3
    assert [c["kind"] for c in last.cards][-2:] == ["tests", "build"]


def test_over_the_spend_limit_the_plan_builds_and_parks_the_tests_for_a_click(project, tmp_path):
    ctx = _tests_ctx(project, tmp_path)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch.object(Toolbox, "_test_targets", return_value=100), \
         patch("forge.service.generate_tests", return_value=_TestGenResult()) as gen, \
         patch("forge.service.build_project", return_value=_build_record("pass")) as build:
        box = _box(ctx, convo, confirm_above_usd=1.0)
        last = box.execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
        gen.assert_not_called()
        assert build.call_count == 1
        assert last.needs_confirmation is True and last.pending_id
        assert last.observation["tests"]["status"] == "needs_confirmation"
        assert last.cards[-1]["kind"] == "confirm" and last.cards[-1]["tool"] == "generate_tests"

        confirmed = box.execute("generate_tests", {}, tool_id="t2", confirmed=True)
        assert gen.call_count == 1
        assert build.call_count == 2, "new tests make the last build stale, so it builds again"
        assert [c["kind"] for c in confirmed.cards] == ["tests", "build"]


def test_without_after_plan_the_plan_writes_no_tests(project, tmp_path):
    ctx = _tests_ctx(project, tmp_path, after_plan=False)
    convo = _seed(Conversation(), ["javax-to-jakarta"])
    with patch("forge.service.run_migration", return_value=_run_result(ctx)), \
         patch("forge.service.generate_tests") as gen, \
         patch("forge.service.build_project", return_value=_build_record("pass")):
        last = _box(ctx, convo, confirm_above_usd=0.0).execute("run_pack", {"pack": "javax-to-jakarta"}, tool_id="t1")
    gen.assert_not_called()
    assert "tests" not in last.observation
