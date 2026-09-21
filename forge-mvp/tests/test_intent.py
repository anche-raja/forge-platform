"""Intent: a sentence narrows the evidence-based pack set, and never extends it.

`reconcile` is pure, so the adversarial cases — a model that invents a pack,
returns both halves of an either/or, or answers outside the enum — are all
ordinary unit tests with no AWS anywhere.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.discover.emit import DEFAULT_DECISIONS, PLAN_JSON
from forge.intent.resolve import reconcile
from forge.intent.vocabulary import (
    DECISION_HELP,
    DECISION_OPTIONS,
    WEB_FRAMEWORK_ROUTES,
    render_vocabulary,
    route_governed_packs,
)
from forge.packs import load_packs
from tests.conftest import llm_reply, write_config
from tests.test_discover import repo  # noqa: F401 — a Struts 2 + Spring 5 reactor


@pytest.fixture
def registry():
    return load_packs()


def act(pack, *, evidence=("dependency x:y:1 (pom.xml)",), runnable=True, complete=True):
    return {"pack": pack, "complete": complete, "runnable": runnable, "evidence": list(evidence)}


def plan_for(proposal, activations, registry, **kw):
    return reconcile(proposal, activations, registry, defaults=DEFAULT_DECISIONS, **kw)


# ─── rule 1: evidence is authoritative ───────────────────────────────────────

def test_a_pack_the_prompt_asks_for_without_evidence_is_never_selected(registry):
    """The whole safety property: intent narrows, it cannot invent."""
    activations = [act("javax-to-jakarta")]
    plan = plan_for({"include": ["javax-to-jakarta", "hibernate-to-hibernate6"]}, activations, registry)

    assert plan.packs == ("javax-to-jakarta",)
    assert [u["asked"] for u in plan.unsupported] == ["hibernate-to-hibernate6"]
    assert "no detection evidence" in plan.unsupported[0]["reason"]


def test_an_empty_repository_yields_an_empty_plan_however_confident_the_prompt(registry):
    plan = plan_for({"include": ["spring-to-spring6"], "decisions": {"web_framework": "migrate-to-spring"}},
                    [], registry)
    assert plan.packs == ()
    assert plan.unsupported[0]["asked"] == "spring-to-spring6"


# ─── rule 2: nothing is silently dropped ─────────────────────────────────────

def test_every_excluded_candidate_keeps_its_reason_and_its_evidence(registry):
    activations = [act("javax-to-jakarta"), act("junit4-to-junit5", evidence=("dependency junit:junit:4.12 (pom.xml)",))]
    plan = plan_for({"include": ["javax-to-jakarta"],
                     "exclude": [{"pack": "junit4-to-junit5", "reason": "leave the tests alone"}]},
                    activations, registry)

    assert plan.packs == ("javax-to-jakarta",)
    assert len(plan.excluded) == 1
    assert plan.excluded[0]["pack"] == "junit4-to-junit5"
    assert plan.excluded[0]["reason"] == "leave the tests alone"
    assert plan.excluded[0]["evidence"] == ["dependency junit:junit:4.12 (pom.xml)"]


def test_a_candidate_dropped_without_a_stated_reason_still_says_so(registry):
    activations = [act("javax-to-jakarta"), act("junit4-to-junit5")]
    plan = plan_for({"include": ["javax-to-jakarta"]}, activations, registry)
    assert plan.excluded[0]["reason"] == "not selected by intent"


# ─── rule 3: closed vocabulary ───────────────────────────────────────────────

def test_an_unknown_decision_key_is_rejected_not_merged(registry):
    """The UI let an unknown key through into the config; this closes it."""
    plan = plan_for({"decisions": {"make_it_fast": "yes"}}, [act("javax-to-jakarta")], registry)

    assert "make_it_fast" not in plan.decisions
    assert plan.rejected == [{"key": "make_it_fast", "value": "yes", "reason": "unknown decision key"}]


def test_a_value_outside_the_enum_is_rejected_and_the_default_stands(registry):
    plan = plan_for({"decisions": {"risk_ceiling": "yolo"}}, [act("javax-to-jakarta")], registry)

    assert plan.decisions["risk_ceiling"] == DEFAULT_DECISIONS["risk_ceiling"]
    assert plan.provenance["risk_ceiling"] == "default"
    assert plan.rejected[0]["key"] == "risk_ceiling"
    assert "not one of" in plan.rejected[0]["reason"]


# ─── rule 4: mutually exclusive routes ───────────────────────────────────────

def test_the_two_struts_routes_are_never_both_selected(registry):
    """'They edit the same files toward different targets' — the requirements spec."""
    activations = [act("struts2-modernize"), act("struts2-to-springmvc6", runnable=False)]
    plan = plan_for({"include": ["struts2-modernize", "struts2-to-springmvc6"],
                     "decisions": {"web_framework": "modernize-in-place"}}, activations, registry)

    assert plan.packs == ("struts2-modernize",)
    assert plan.excluded[0]["pack"] == "struts2-to-springmvc6"
    assert "takes the other route" in plan.excluded[0]["reason"]


def test_the_other_route_wins_when_the_decision_says_so(registry):
    activations = [act("struts2-modernize"), act("struts2-to-springmvc6", runnable=False)]
    plan = plan_for({"decisions": {"web_framework": "migrate-to-spring"}}, activations, registry)

    assert plan.packs == ("struts2-to-springmvc6",)
    assert plan.excluded[0]["pack"] == "struts2-modernize"


def test_a_view_pack_that_reads_web_framework_is_not_arbitrated_by_it(registry):
    """`jsp-jstl-modernize` ends in '-modernize' and declares `web_framework`,
    but it runs on either route — a name-suffix rule would drop the JSPs."""
    activations = [act("struts2-to-springmvc6", runnable=False), act("jsp-jstl-modernize")]
    plan = plan_for({"decisions": {"web_framework": "migrate-to-spring"}}, activations, registry)
    assert "jsp-jstl-modernize" in plan.packs


# ─── rule 5: state labels survive selection ──────────────────────────────────

def test_a_detect_only_pack_is_labelled_never_promoted(registry):
    activations = [act("hibernate-to-hibernate6", runnable=False, complete=False)]
    plan = plan_for({"include": ["hibernate-to-hibernate6"]}, activations, registry)

    assert plan.packs == ("hibernate-to-hibernate6",)
    assert plan.states["hibernate-to-hibernate6"] == "detect-only"


def test_a_blocked_pack_is_labelled_blocked(registry):
    activations = [act("struts2-to-springmvc6", runnable=False, complete=True)]
    plan = plan_for({"decisions": {"web_framework": "migrate-to-spring"}}, activations, registry)
    assert plan.states["struts2-to-springmvc6"] == "blocked"


# ─── rule 6: coherence ───────────────────────────────────────────────────────

def test_dropping_a_dependency_that_was_available_is_reported(registry):
    """Both selected packs depend on the namespace pack; dropping it while
    keeping them is the hand-edited-profile mistake `missing_dependencies`
    exists to catch."""
    activations = [act("javax-to-jakarta"), act("struts2-modernize"), act("jsp-jstl-modernize")]
    plan = plan_for({"include": ["jsp-jstl-modernize", "struts2-modernize"]}, activations, registry)

    assert plan.gaps == {
        "jsp-jstl-modernize": ["javax-to-jakarta"],
        "struts2-modernize": ["javax-to-jakarta"],
    }


def test_the_losing_half_of_the_route_is_not_reported_as_a_gap(registry):
    """`jsp-jstl-modernize` names both Struts packs because it must follow
    whichever one runs. The other one's absence is the design, not a gap."""
    activations = [act("struts2-modernize"), act("struts2-to-springmvc6", runnable=False),
                   act("jsp-jstl-modernize"), act("javax-to-jakarta")]
    plan = plan_for({"decisions": {"web_framework": "modernize-in-place"}}, activations, registry)

    assert plan.gaps == {}


# ─── rule 7: the order is never the model's ──────────────────────────────────

def test_order_comes_from_the_topological_sort_not_the_proposal(registry):
    activations = [act("javax-to-jakarta"), act("spring-to-spring6"), act("build-maven-modernize")]
    plan = plan_for({"include": ["spring-to-spring6", "javax-to-jakarta", "build-maven-modernize"]},
                    activations, registry)

    assert plan.packs == registry.resolve_order(
        ["spring-to-spring6", "javax-to-jakarta", "build-maven-modernize"])
    assert plan.packs.index("javax-to-jakarta") < plan.packs.index("spring-to-spring6")


# ─── rule 8: provenance ──────────────────────────────────────────────────────

def test_every_decision_says_where_it_came_from(registry):
    plan = plan_for({"decisions": {"risk_ceiling": "auto"}}, [act("java8-to-java21")], registry,
                    config_decisions={"container": "tomcat"})

    assert plan.decisions["risk_ceiling"] == "auto"
    assert plan.provenance["risk_ceiling"] == "prompt"
    assert plan.decisions["container"] == "tomcat"
    assert plan.provenance["container"] == "config"
    assert plan.provenance["views"] == "default"


def test_unstated_decisions_that_the_selected_packs_read_become_assumptions(registry):
    """Not every default matters — only the ones a selected pack will read."""
    plan = plan_for({}, [act("java8-to-java21")], registry)

    assert any("idiom_aggressiveness" in a for a in plan.assumptions)
    assert not any("persistence" in a for a in plan.assumptions)


def test_a_decision_with_no_value_anywhere_is_called_out(registry):
    """`liberty-server-config` reads liberty_edition and liberty_features, and
    DEFAULT_DECISIONS carries neither — the spec table lists both. A reader must
    not have to notice the absence for themselves."""
    plan = plan_for({}, [act("liberty-server-config")], registry)

    unset = [a for a in plan.assumptions if "no value set anywhere" in a]
    assert {"liberty_edition", "liberty_features"} == {a.split()[0] for a in unset}
    assert "open, websphere" in next(a for a in unset if a.startswith("liberty_edition"))


# ─── vagueness and malformed input ───────────────────────────────────────────

def test_a_vague_prompt_keeps_everything_and_defaults_everything(registry):
    activations = [act("javax-to-jakarta"), act("java8-to-java21"), act("junit4-to-junit5")]
    plan = plan_for({}, activations, registry)

    assert set(plan.packs) == {"javax-to-jakarta", "java8-to-java21", "junit4-to-junit5"}
    assert plan.decisions == dict(DEFAULT_DECISIONS)
    assert all(v == "default" for v in plan.provenance.values())


def test_no_proposal_at_all_gives_the_plan_discovery_would_have_produced(registry):
    """A response that would not parse must degrade to discovery, not to a guess."""
    activations = [act("javax-to-jakarta"), act("java8-to-java21")]
    assert plan_for(None, activations, registry).packs == plan_for({}, activations, registry).packs


@pytest.mark.parametrize("junk", [
    {"include": "not-a-list"}, {"decisions": ["not", "a", "dict"]},
    {"exclude": [{"no_pack_key": 1}]}, {"scope": "nope"}, {"questions": [None, 3]},
])
def test_malformed_proposal_fields_are_ignored_not_fatal(junk, registry):
    plan = plan_for(junk, [act("javax-to-jakarta")], registry)
    assert plan.packs == ("javax-to-jakarta",)


# ─── scope ───────────────────────────────────────────────────────────────────

def test_scope_globs_are_kept_and_bad_ones_rejected(registry):
    plan = plan_for({"scope": {"exclude_globs": ["db/**", "  "], "package_prefix": "org.example.am"}},
                    [act("javax-to-jakarta")], registry)

    assert plan.scope["exclude_globs"] == ["db/**"]
    assert plan.scope["package_prefix"] == "org.example.am"


# ─── the vocabulary itself ───────────────────────────────────────────────────

def test_every_route_pack_is_declared_in_the_routes_table(registry):
    """A new framework-tier pack reading `web_framework` must be added to
    WEB_FRAMEWORK_ROUTES, or rule 4 would quietly stop arbitrating it."""
    governed = route_governed_packs()
    for pack in registry.values():
        if pack.tier == "framework" and "web_framework" in pack.decisions:
            assert pack.id in governed, f"{pack.id} reads web_framework but is in no route"


def test_every_decision_option_has_help_text():
    assert set(DECISION_HELP) == set(DECISION_OPTIONS)


def test_the_vocabulary_offers_only_packs_the_evidence_activated():
    rendered = render_vocabulary([act("javax-to-jakarta")], dict(DEFAULT_DECISIONS))
    assert "javax-to-jakarta" in rendered
    assert "hibernate-to-hibernate6" not in rendered


def test_the_ui_and_the_intent_layer_share_one_decision_table():
    from forge.ui.app import DECISION_OPTIONS as ui_options
    assert ui_options is DECISION_OPTIONS


# ─── the agent ───────────────────────────────────────────────────────────────

SOURCE_MARKER = "this-is-a-file-body-and-must-never-be-sent"


def _profile_json():
    return {
        "build_system": "maven", "java_level": "8",
        "modules": [{"path": "orders", "packaging": "war"}],
        "dependencies": ["org.apache.struts:struts2-core:6.8.0"],
        "counts": {".java": 12}, "descriptors": ["orders/src/main/webapp/WEB-INF/web.xml"],
        "import_prefixes": {"javax.servlet.http": 4},
    }


def test_the_agent_is_never_given_source_code(tmp_path):
    """GUARDRAILS §7 one layer up: intent must not be the thing that leaks a file."""
    config = write_config(tmp_path)
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"include": ["javax-to-jakarta"]})
        from forge.intent.agent import IntentAgent

        agent = IntentAgent(config)
        profile = dict(_profile_json(), source_body=SOURCE_MARKER)
        agent.propose("modernize this", profile, [act("javax-to-jakarta")], dict(DEFAULT_DECISIONS))

        sent = "\n".join(m.content for m in MockLLM.return_value.invoke.call_args[0][0])
        assert SOURCE_MARKER not in sent
        assert "org.apache.struts:struts2-core:6.8.0" in sent   # coordinates are fine
        assert "web.xml" in sent                                # descriptor names are fine


def test_a_malformed_response_becomes_no_proposal(tmp_path):
    config = write_config(tmp_path)
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        bad = MagicMock()
        bad.content = "I think you should upgrade Spring!"
        bad.usage_metadata = {"input_tokens": 10, "output_tokens": 5}
        MockLLM.return_value.invoke.return_value = bad
        from forge.intent.agent import IntentAgent

        agent = IntentAgent(config)
        assert agent.propose("x", _profile_json(), [], {}) is None
        assert agent.bedrock_calls == 1        # it counts the call, not the success


def test_the_agent_costs_what_the_pricing_table_says(tmp_path):
    config = write_config(tmp_path)
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"include": []})
        from forge.intent.agent import IntentAgent

        agent = IntentAgent(config)
        agent.propose("x", _profile_json(), [], {})
        assert agent.cost_usd > 0


# ─── service integration ─────────────────────────────────────────────────────

def test_discover_without_intent_makes_no_model_call(repo, tmp_path):  # noqa: F811
    """The contract the whole deterministic path rests on. If this ever fails,
    discovery has stopped being free."""
    from forge import service

    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        result = service.discover(str(repo), str(tmp_path / "out"))
        MockLLM.assert_not_called()
    assert "intent" not in result


def test_discover_with_intent_narrows_and_writes_the_plan(repo, tmp_path):  # noqa: F811
    from forge import service

    config = write_config(tmp_path)
    out = tmp_path / "out"
    proposal = {
        "include": ["javax-to-jakarta", "java8-to-java21"],
        "exclude": [{"pack": "junit4-to-junit5", "reason": "leave the tests alone"}],
        "scope": {"exclude_globs": ["db/**"]},
        "decisions": {"idiom_aggressiveness": "moderate"},
    }
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply(proposal)
        result = service.discover(str(repo), str(out), config, intent="just Java and the namespace")

    assert set(result["order"]) == {"javax-to-jakarta", "java8-to-java21"}
    assert result["intent"]["scope"]["exclude_globs"] == ["db/**"]
    assert result["intent"]["provenance"]["idiom_aggressiveness"] == "prompt"

    written = json.loads((out / PLAN_JSON).read_text())
    assert written["packs"] == list(result["order"])
    assert any(e["pack"] == "junit4-to-junit5" for e in written["excluded"])

    profile_yaml = (out / "forge-profile.yaml").read_text()
    assert "exclude_globs: ['db/**']" in profile_yaml
    assert "# Detected but NOT selected" in profile_yaml
    assert "idiom_aggressiveness: moderate   # from prompt" in profile_yaml


def test_a_decision_from_the_prompt_re_gates_the_activations(repo, tmp_path):  # noqa: F811
    """`liberty-server-config` is gated on `container: liberty`. Asking for
    Tomcat must stop it firing — which means re-resolving after the decision."""
    from forge import service

    config = write_config(tmp_path)
    with patch("forge.intent.agent.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"decisions": {"container": "tomcat"}})
        result = service.discover(str(repo), str(tmp_path / "out"), config, intent="deploy on tomcat")

    assert "liberty-server-config" not in result["order"]
    assert result["decisions"]["container"] == "tomcat"


def test_intent_without_a_config_is_refused_before_any_call(repo, tmp_path):  # noqa: F811
    from forge import service

    with pytest.raises(ValueError, match="agents.yaml"):
        service.discover(str(repo), str(tmp_path / "out"), None, intent="modernize")


# ─── scope globs reach the scanner ───────────────────────────────────────────

def test_scope_globs_exclude_files_from_the_scan(tmp_path):
    from forge.utils.file_scanner import scan_java_files

    src = tmp_path / "src/main/java/com/acme"
    src.mkdir(parents=True)
    (src / "Keep.java").write_text("package com.acme;\nclass Keep {}\n", encoding="utf-8")
    db = tmp_path / "db"
    db.mkdir()
    (db / "Drop.java").write_text("package com.acme;\nclass Drop {}\n", encoding="utf-8")

    everything = scan_java_files(str(tmp_path), "javax-to-jakarta")
    assert len(everything.files) == 2

    narrowed = scan_java_files(str(tmp_path), "javax-to-jakarta", "", ["db/**"])
    assert [Path(f).name for f in narrowed.files] == ["Keep.java"]
    assert any("excluded by scope glob 'db/**'" in s.reason for s in narrowed.skipped)
