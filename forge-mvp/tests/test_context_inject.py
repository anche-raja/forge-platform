"""Context injection: the extracted descriptors reach the transform, review and
pre-flight prompts for packs that declare a context — and nothing changes for
packs that do not."""

from pathlib import Path
from unittest.mock import patch

import pytest

from forge.context import inject
from forge.context.inject import context_block_for
from forge.context.render import BEGIN, END
from forge.extract import clear_context_cache
from tests.conftest import llm_reply, make_state, write_config
from tests.test_extract_web_bootstrap import make_module
from tests.test_packs import _fm, write_pack

WEBAPP = "webapp-bootstrap-jakarta10"
LIBERTY = "liberty-server-config"
LEGACY_HEADER = "Transform this file:\nFile path: "


@pytest.fixture(autouse=True)
def _fresh():
    clear_context_cache()
    inject._warned.clear()
    yield
    clear_context_cache()
    inject._warned.clear()


@pytest.fixture
def module(tmp_path):
    # The existing Liberty config lives outside the maven config dir, as ops
    # often keeps it; the generate target at src/main/liberty/config/ stays absent.
    return make_module(tmp_path, java=("LoggingFilter.java", "StartupListener.java"),
                       extras=(("server.xml", "deploy/liberty/server.xml"),))


@pytest.fixture
def fresh_registry(monkeypatch):
    from forge import phases

    def use(directory):
        monkeypatch.setenv("FORGE_PACKS_DIR", str(directory))
        phases._packs.cache_clear()
        return phases

    yield use
    phases._packs.cache_clear()


def _transform(tmp_path, file_path, phase, **fs_overrides):
    """Run JavaUpgradeAgent once and return (human message, resulting file_status)."""
    from forge.agents.java_upgrade import JavaUpgradeAgent

    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"files": {file_path: "<web-app/>"}, "manual_flags": []})
        agent = JavaUpgradeAgent(write_config(tmp_path))
        state = make_state(file_path, tmp_path, phase=phase)
        state["current_file"].update(fs_overrides)
        result = agent.run(state)
        human = MockLLM.return_value.invoke.call_args[0][0][1].content
    return human, result["current_file"]


# ─── transform ────────────────────────────────────────────────────────────────

def test_context_block_is_appended_after_the_source_for_a_pack_with_context(tmp_path, module):
    web_xml = str(module / "src/main/webapp/WEB-INF/web.xml")
    human, fs = _transform(tmp_path, web_xml, WEBAPP)

    assert human.startswith(LEGACY_HEADER), "the CLI tests split on this header"
    assert BEGIN.format(name="web_bootstrap") in human and human.rstrip().endswith(END)
    assert human.index("```") < human.index(BEGIN.format(name="web_bootstrap")), "source first, context after"
    assert "## filter_chain" in human and "springSecurityFilterChain" in human
    assert "LoggingFilter" in human, "resolved servlet components are part of the context"
    assert fs["context_name"] == "web_bootstrap"
    assert len(fs["context_digest"]) == 64
    assert fs["status"] == "REVIEWING"


def test_no_context_for_a_pack_that_declares_none_and_the_message_is_unchanged(tmp_path, module):
    java = str(module / "src/main/java/com/acme/orders/web/LoggingFilter.java")
    human, fs = _transform(tmp_path, java, "javax-to-jakarta")
    source = Path(java).read_text(encoding="utf-8")
    assert human == f"{LEGACY_HEADER}{java}\n\n```\n{source}\n```"
    assert "CONTEXT" not in human
    assert fs["context_name"] is None and fs["context_digest"] is None


def test_no_context_for_the_builtin_phases(tmp_path, module):
    java = str(module / "src/main/java/com/acme/orders/web/LoggingFilter.java")
    human, _ = _transform(tmp_path, java, "java21")
    assert "CONTEXT" not in human


def test_generated_unit_gets_the_context_instead_of_a_source_file(tmp_path, module):
    target = str(module / "src/main/liberty/config/server.xml")
    assert not Path(target).exists()
    human, fs = _transform(tmp_path, target, LIBERTY, generate=True)

    assert human.startswith(LEGACY_HEADER)
    assert "No existing file — generate it." in human and f"Target path: {target}" in human
    assert "```" not in human.split(BEGIN.format(name="web_bootstrap"))[0], "no source fence for a generated unit"
    # For a server.xml target the resources lead, so pool sizes are never what gets cut.
    assert human.index("## datasources") < human.index("## web_xml")
    assert "maxPoolSize" in human and "jdbc/ordersDS" in human
    assert fs["status"] == "REVIEWING"


def test_retry_feedback_still_follows_the_context_block(tmp_path, module):
    web_xml = str(module / "src/main/webapp/WEB-INF/web.xml")
    human, _ = _transform(tmp_path, web_xml, WEBAPP, retry_count=1, review_feedback="filter order changed")
    assert human.index(END) < human.index("PREVIOUS REVIEW FEEDBACK")
    assert "filter order changed" in human


# ─── review ───────────────────────────────────────────────────────────────────

def test_reviewer_receives_the_same_context_block(tmp_path, module):
    from forge.review.java_reviewer import JavaReviewer

    web_xml = str(module / "src/main/webapp/WEB-INF/web.xml")
    with patch("forge.review.java_reviewer.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"score": 90, "verdict": "PASS", "feedback": "", "checks": {}})
        reviewer = JavaReviewer(write_config(tmp_path))
        state = make_state(web_xml, tmp_path, phase=WEBAPP)
        state["current_file"]["transform_output"] = {"files": {web_xml: "<web-app/>"}}
        reviewer.review(state)
        human = MockLLM.return_value.invoke.call_args[0][0][1].content

    assert human.startswith("Review this transformed code:")
    assert "The descriptors the transform was given (check nothing was dropped)" in human
    assert BEGIN.format(name="web_bootstrap") in human and "## filter_chain" in human
    assert human.index("```") < human.index(BEGIN.format(name="web_bootstrap"))


def test_reviewer_message_is_unchanged_without_context(tmp_path, module):
    from forge.review.java_reviewer import JavaReviewer

    java = str(module / "src/main/java/com/acme/orders/web/LoggingFilter.java")
    with patch("forge.review.java_reviewer.ChatBedrockConverse") as MockLLM:
        MockLLM.return_value.invoke.return_value = llm_reply({"score": 90, "verdict": "PASS", "feedback": "", "checks": {}})
        reviewer = JavaReviewer(write_config(tmp_path))
        state = make_state(java, tmp_path, phase="javax-to-jakarta")
        state["current_file"]["transform_output"] = {"files": {java: "x"}}
        reviewer.review(state)
        human = MockLLM.return_value.invoke.call_args[0][0][1].content
    assert human == "Review this transformed code:\n\n```\n// FILE: " + java + "\nx\n```"


# ─── pre-flight ───────────────────────────────────────────────────────────────

def test_pre_flight_screens_the_context_block_for_a_generated_unit(tmp_path, module):
    """Vendor descriptors are where credentials leak; for a unit with no source
    file the context block is the model's actual input, so that is what the
    guardrail must see."""
    from forge.agents.guardrails_pre import GuardrailsPreAgent

    target = str(module / "src/main/liberty/config/server.xml")
    with patch("forge.agents.guardrails_pre.BedrockGuardrails") as MockGR, \
         patch("forge.agents.guardrails_pre.ChatBedrockConverse") as MockLLM:
        MockGR.return_value.evaluate.return_value = {"action": "NONE", "findings": [], "intervened": False}
        MockLLM.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        agent = GuardrailsPreAgent(write_config(tmp_path))
        state = make_state(target, tmp_path, phase=LIBERTY)
        state["current_file"]["generate"] = True
        result = agent.run(state)
        screened = MockGR.return_value.evaluate.call_args[0][0]

    assert BEGIN.format(name="web_bootstrap") in screened
    assert "jdbc/ordersDS" in screened
    assert result["current_file"]["status"] == "TRANSFORMING", "a missing file is not a read failure for a generated unit"


def test_pre_flight_still_blocks_on_an_unreadable_source_file(tmp_path, module):
    from forge.agents.guardrails_pre import GuardrailsPreAgent

    with patch("forge.agents.guardrails_pre.BedrockGuardrails"), \
         patch("forge.agents.guardrails_pre.ChatBedrockConverse"):
        agent = GuardrailsPreAgent(write_config(tmp_path))
        result = agent.run(make_state(str(module / "missing.java"), tmp_path, phase=WEBAPP))
    assert result["current_file"]["status"] == "BLOCKED"


# ─── degradation ──────────────────────────────────────────────────────────────

def test_unregistered_context_runs_without_a_block_and_warns_once(tmp_path, module, fresh_registry, caplog):
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "routing-pack", frontmatter=_fm(
        "routing-pack", context="struts_routing_table",
        applies_to='\n  - file_glob: "**/*.java"\n  - selector: struts_actions'))
    fresh_registry(packs_dir)

    java = str(module / "src/main/java/com/acme/orders/web/LoggingFilter.java")
    state = make_state(java, tmp_path, phase="routing-pack")
    cfg = write_config(tmp_path)
    assert context_block_for(state, cfg) == (None, None)
    assert context_block_for(state, cfg) == (None, None)
    assert caplog.text.count("no extractor is registered") == 1


def test_a_missing_extractor_is_recorded_and_not_confused_with_wanting_no_context(
        tmp_path, module, fresh_registry):
    """The two reasons a context block is empty must stay tellable apart.

    Recording `context_name` only when a block arrived left both cases at
    `None`: a pack transforming blind because its extractor is unbuilt looked
    exactly like a pack that declared `context: none`. Four shipped packs are in
    the first group, so this is the field that says a run was lower confidence.
    """
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "blind-pack", frontmatter=_fm(
        "blind-pack", context="spring_bean_graph",
        applies_to='\n  - file_glob: "**/*.java"'))
    write_pack(packs_dir, "plain-pack", frontmatter=_fm(
        "plain-pack", context="none", applies_to='\n  - file_glob: "**/*.java"'))
    fresh_registry(packs_dir)

    java = str(module / "src/main/java/com/acme/orders/web/LoggingFilter.java")

    _, blind = _transform(tmp_path, java, "blind-pack")
    assert blind["context_name"] == "spring_bean_graph", "the declared context is named even when unmet"
    assert blind["context_missing"] is True
    assert blind["context_digest"] is None, "nothing was rendered, so there is nothing to digest"

    _, plain = _transform(tmp_path, java, "plain-pack")
    assert plain["context_name"] is None, "a pack wanting no context names none"
    assert not plain.get("context_missing"), "declaring `context: none` is not a missing context"


def test_a_context_that_is_present_still_records_its_name_and_digest(tmp_path, module):
    """The success path must keep working — this is the control for the test above."""
    target = str(module / "src/main/webapp/WEB-INF/web.xml")
    _, fs = _transform(tmp_path, target, WEBAPP)
    assert fs["context_name"] == "web_bootstrap"
    assert fs["context_digest"], "a rendered block is digested for the audit trail"
    assert not fs.get("context_missing")


def test_module_without_a_descriptor_degrades_to_no_context_with_a_warning(tmp_path, caplog):
    lib = tmp_path / "lib"
    src = lib / "src/main/java/com/acme/Util.java"
    src.parent.mkdir(parents=True)
    src.write_text("package com.acme;\npublic class Util {}\n", encoding="utf-8")
    (lib / "pom.xml").write_text("<project/>", encoding="utf-8")

    state = make_state(str(src), tmp_path, phase=WEBAPP)
    assert context_block_for(state, write_config(tmp_path)) == (None, None)
    assert "no WEB-INF/web.xml" in caplog.text


def test_max_chars_from_config_bounds_the_block(tmp_path, module):
    web_xml = str(module / "src/main/webapp/WEB-INF/web.xml")
    state = make_state(web_xml, tmp_path, phase=WEBAPP)
    block, digest = context_block_for(state, write_config(tmp_path, context={"max_chars": 1200}))
    assert block is not None and len(block) <= 1200, "max_chars is a guarantee, not a target"
    assert "OMITTED" in block and "## summary" in block and "migration-context.json" in block
    assert len(digest) == 64
