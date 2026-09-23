"""Deterministic risk scoring: the same file always gets the same score and the
same reasons in the same order, so a held unit can say exactly why."""

from unittest.mock import patch

import pytest

from forge.risk import DEFAULT_THRESHOLDS, score_unit, thresholds_from, tier_for
from tests.conftest import llm_reply, make_state, write_config

JAVA_PLAIN = "package com.acme;\npublic class Util {\n  int x;\n}\n"


def _java(body: str, lines: int = 0) -> str:
    return "package com.acme;\n" + body + "\n" * lines


# ─── bands and markers ────────────────────────────────────────────────────────

def test_tiny_file_is_low_with_no_reasons():
    """The whole existing suite runs under the default review-high ceiling;
    its fixture files must not be held."""
    score, tier, reasons = score_unit("/p/Util.java", JAVA_PLAIN)
    assert (score, tier, reasons) == (0, "LOW", [])


@pytest.mark.parametrize("lines,points", [(50, 0), (100, 10), (300, 20), (600, 30), (1000, 40), (5000, 40)])
def test_loc_bands_are_monotonic(lines, points):
    score, _, reasons = score_unit("/p/A.java", _java("class A {}", lines))
    assert score == points
    assert (f"{lines + 1} lines" in reasons) == (points > 0)


def test_markers_add_reasons_in_a_fixed_order():
    src = _java("import org.springframework.transaction.annotation.Transactional;\n"
                "@Transactional public class OrderAction implements ModelDriven<Order> {\n"
                "  void stop() { Thread.stop(); sun.misc.Unsafe u; }\n}")
    score, tier, reasons = score_unit("/p/OrderAction.java", src)
    assert score == 25 + 20 + 30 + 20
    assert tier == "HIGH"
    assert [r.split(":")[0] for r in reasons] == [
        "Spring-proxied class", "ModelDriven", "sun.misc.Unsafe", "Thread.stop",
    ]


def test_security_configuration_is_high_by_rule_even_when_small():
    src = _java("public class WebSecurityConfig extends WebSecurityConfigurerAdapter {}")
    score, tier, reasons = score_unit("/p/WebSecurityConfig.java", src)
    assert tier == "HIGH" and score == DEFAULT_THRESHOLDS["high_at"]
    assert reasons == ["security configuration: HIGH by rule"]


def test_generated_unit_is_high_by_rule():
    score, tier, reasons = score_unit("/m/src/main/liberty/config/server.xml", "", generate=True)
    assert tier == "HIGH" and reasons[0].startswith("generated unit")


def test_web_xml_fan_out_and_authorization_rules():
    web = "<web-app>" + "<filter><filter-name>f</filter-name></filter>" * 4 + "<security-constraint/></web-app>"
    score, tier, reasons = score_unit("/m/WEB-INF/web.xml", web)
    assert score == 15 + 25 and tier == "MEDIUM"
    assert reasons == ["descriptor fan-out: 4 entries", "authorization rules in descriptor (authz_parity)"]


def test_extracted_filter_chain_sizes_fan_out_without_reparsing():
    ctx = {"filter_chain": [{}] * 12}
    score, _, reasons = score_unit("/m/WEB-INF/web.xml", "<web-app/>", ctx_summary=ctx)
    assert score == 30 and reasons == ["descriptor fan-out: 12 entries"]


def test_struts_xml_counts_actions():
    xml = "<struts>" + '<action name="a"/>' * 11 + "</struts>"
    score, _, reasons = score_unit("/m/struts-user.xml", xml)
    assert score == 30 and reasons == ["descriptor fan-out: 11 entries"]


@pytest.mark.parametrize("count,points", [(0, 0), (1, 5), (3, 15), (10, 30)])
def test_jsp_ognl_density(count, points):
    jsp = "<html>" + "<s:property value=\"%{x}\"/>" * count + "</html>"
    score, _, reasons = score_unit("/m/list.jsp", jsp)
    assert score == points
    assert (f"OGNL expressions: {count}" in reasons) == (count > 0)


def test_pack_content_matcher_hit_is_high_by_rule():
    class Spec:
        high_risk_matchers = (("**/*.java", r"@EnableGlobalMethodSecurity"),)

    src = _java("@EnableGlobalMethodSecurity public class GlobalSecurityConfig {}")
    score, tier, reasons = score_unit("/p/GlobalSecurityConfig.java", src, Spec())
    assert tier == "HIGH" and "matched the pack's content selector: HIGH by rule" in reasons
    # The same file under a pack with no matchers is just a small Java file.
    assert score_unit("/p/GlobalSecurityConfig.java", src)[1] == "LOW"


def test_plain_content_matcher_hit_adds_no_risk():
    """A matcher that only selects relevant files (javax.servlet users) is not a hazard."""
    class Spec:
        content_matchers = (("**/*.java", r"javax\.servlet"),)
        high_risk_matchers = ()

    src = _java("import javax.servlet.Filter; public class F {}")
    assert score_unit("/p/F.java", src, Spec())[1] == "LOW"


def test_packs_flag_only_security_config_as_high_risk():
    from forge.packs import load_packs

    registry = load_packs()
    flagged = {p for p in registry.order if registry[p].high_risk_matchers}
    assert flagged == {"springsec-to-springsec6"}


def test_score_is_capped_at_100():
    src = _java("@Transactional class A implements ModelDriven { sun.misc.Unsafe u; Thread.stop(); }", 1200)
    score, _, _ = score_unit("/p/A.java", src)
    assert score == 100


# ─── thresholds ───────────────────────────────────────────────────────────────

def test_thresholds_from_config_change_the_tier_not_the_score(tmp_path):
    src = _java("import x;\n@Transactional public class A {}")   # 25 points
    assert score_unit("/p/A.java", src)[1] == "LOW"
    cfg = write_config(tmp_path, risk={"high_at": 20, "medium_at": 10})
    score, tier, _ = score_unit("/p/A.java", src, thresholds=thresholds_from(cfg))
    assert score == 25 and tier == "HIGH"


def test_thresholds_from_config_tolerates_garbage(tmp_path):
    cfg = write_config(tmp_path, risk={"high_at": "lots"})
    assert thresholds_from(cfg) == dict(DEFAULT_THRESHOLDS)
    assert thresholds_from(None) == dict(DEFAULT_THRESHOLDS)


@pytest.mark.parametrize("score,tier", [(0, "LOW"), (29, "LOW"), (30, "MEDIUM"), (59, "MEDIUM"), (60, "HIGH"), (100, "HIGH")])
def test_tier_boundaries(score, tier):
    assert tier_for(score) == tier


def test_scoring_is_deterministic_across_calls():
    src = _java("@Secured class A implements ModelDriven {}", 350)
    assert score_unit("/p/A.java", src) == score_unit("/p/A.java", src)


# ─── assignment in the pre-flight node ────────────────────────────────────────

def _pre(tmp_path, file_path, *, gr_intervenes=False, phase="javax-to-jakarta", generate=False):
    from forge.agents.guardrails_pre import GuardrailsPreAgent

    with patch("forge.agents.guardrails_pre.BedrockGuardrails") as MockGR, \
         patch("forge.agents.guardrails_pre.ChatBedrockConverse") as MockLLM:
        MockGR.return_value.evaluate.return_value = {
            "action": "GUARDRAIL_INTERVENED" if gr_intervenes else "NONE", "findings": [], "intervened": gr_intervenes,
        }
        MockLLM.return_value.invoke.return_value = llm_reply({"verdict": "PASS", "findings": [], "reason": ""})
        state = make_state(file_path, tmp_path, phase=phase)
        state["current_file"]["generate"] = generate
        return GuardrailsPreAgent(write_config(tmp_path)).run(state)["current_file"]


def test_pre_flight_assigns_score_tier_and_reasons(tmp_path):
    src = tmp_path / "OrderAction.java"
    src.write_text(_java("@Transactional public class OrderAction implements ModelDriven<Order> {}"), encoding="utf-8")
    fs = _pre(tmp_path, str(src))
    assert fs["risk_score"] == 45 and fs["risk_tier"] == "MEDIUM"
    assert fs["risk_reasons"][0].startswith("Spring-proxied class")
    assert fs["status"] == "TRANSFORMING"


def test_a_blocked_file_is_still_scored(tmp_path):
    """The queue a human reads must show the risk of what the guardrail blocked."""
    src = tmp_path / "WebSecurityConfig.java"
    src.write_text(_java("public class WebSecurityConfig extends WebSecurityConfigurerAdapter {}"), encoding="utf-8")
    fs = _pre(tmp_path, str(src), gr_intervenes=True)
    assert fs["status"] == "BLOCKED"
    assert fs["risk_tier"] == "HIGH"
