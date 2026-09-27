"""A pack tier may name its own reviewer, and a check with nothing to examine is worth full points.

On AMS the default reviewer (Nova Pro) scored an EAR pom 5 while asserting it
declared no dependencies and no plugins -- both false -- and a sound web pom 55
with no reason. Two things fix that: ``review_models.build`` names a model that
follows a rubric on XML, and a check the reviewer marks "n/a" is scored at its
maximum in code, with the total re-added from the rubric's own schema.
"""

from unittest.mock import patch

from forge.config import ForgeConfig
from forge.phases import get_phase
from forge.review.java_reviewer import JavaReviewer, apply_not_applicable, response_check_maxima
from tests.conftest import llm_reply, make_state

BUILD = "build-maven-modernize"
NOVA, HAIKU = "us.amazon.nova-pro-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _config(**extra):
    return ForgeConfig(data={"review_model": NOVA, "transform_model": "sonnet", "aws_region": "us-east-1",
                             "pass_threshold": 80, "retry_threshold": 50, "model_pricing": {}, **extra})


# ─── the config ───────────────────────────────────────────────────────────────

def test_the_one_reviewer_is_used_when_no_tier_names_another():
    cfg = _config()
    assert cfg.review_model_for("build") == NOVA
    assert cfg.review_model_for("") == NOVA


def test_a_tier_may_name_its_own_reviewer_and_the_others_keep_the_default():
    cfg = _config(review_models={"build": HAIKU})
    assert cfg.review_model_for("build") == HAIKU
    assert cfg.review_model_for("code") == NOVA
    assert cfg.review_model_for("") == NOVA


# ─── the reviewer ─────────────────────────────────────────────────────────────

def _review(tmp_path, cfg, reply: dict, phase=BUILD):
    pom = tmp_path / "pom.xml"
    pom.write_text("<project/>", encoding="utf-8")
    state = make_state(str(pom), tmp_path, phase=phase)
    state["current_file"]["transform_output"] = {"files": {str(pom): "<project><properties/></project>"}}
    with patch("forge.review.java_reviewer.ChatBedrockConverse") as converse:
        converse.return_value.invoke.return_value = llm_reply(reply)
        result = JavaReviewer(cfg).review(state)
    return result["current_file"], converse


def test_a_build_pack_is_reviewed_by_the_build_tier_model(tmp_path):
    fs, converse = _review(tmp_path, _config(review_models={"build": HAIKU}),
                           {"score": 90, "verdict": "PASS", "feedback": "", "checks": {}})
    assert fs["review_model"] == HAIKU and fs["review_verdict"] == "PASS"
    assert HAIKU in [c.kwargs.get("model") for c in converse.call_args_list]


def test_a_code_pack_keeps_the_default_reviewer(tmp_path):
    fs, converse = _review(tmp_path, _config(review_models={"build": HAIKU}),
                           {"score": 90, "verdict": "PASS", "feedback": "", "checks": {}}, phase="javax-to-jakarta")
    assert fs["review_model"] == NOVA
    assert HAIKU not in [c.kwargs.get("model") for c in converse.call_args_list]


# ─── "n/a" ────────────────────────────────────────────────────────────────────

RUBRIC = get_phase(BUILD).review_prompt


def test_the_rubrics_schema_names_every_check_with_its_maximum():
    assert response_check_maxima(RUBRIC) == (("java_and_bom", 25), ("jakarta_coords", 20), ("plugins_java21", 20),
                                             ("eliminations_exact", 20), ("nothing_else", 15))
    assert sum(cap for _, cap in response_check_maxima(RUBRIC)) == 100


def test_the_ams_ear_pom_verdict_is_recomputed_from_its_n_a_checks():
    reply = {"score": 5, "verdict": "MANUAL", "feedback": "Checks 1-4 do not apply.",
             "checks": {"java_and_bom": "n/a", "jakarta_coords": "n/a", "plugins_java21": "n/a",
                        "eliminations_exact": "n/a", "nothing_else": 15}}
    out = apply_not_applicable(reply, RUBRIC)
    assert out["score"] == 100
    assert out["checks"] == {"java_and_bom": 25, "jakarta_coords": 20, "plugins_java21": 20,
                             "eliminations_exact": 20, "nothing_else": 15}
    assert "n/a" in out["feedback"] and "java_and_bom" in out["feedback"]


def test_a_number_out_of_range_is_clamped_and_junk_is_zero():
    reply = {"score": 0, "checks": {"java_and_bom": "n/a", "jakarta_coords": 99, "plugins_java21": "ten",
                                    "eliminations_exact": -3, "nothing_else": None}}
    out = apply_not_applicable(reply, RUBRIC)
    assert out["checks"] == {"java_and_bom": 25, "jakarta_coords": 20, "plugins_java21": 0,
                             "eliminations_exact": 0, "nothing_else": 15}
    assert out["score"] == 60


def test_without_an_n_a_the_models_score_stands():
    reply = {"score": 55, "feedback": "x", "checks": {"java_and_bom": 10, "jakarta_coords": 20, "plugins_java21": 10,
                                                      "eliminations_exact": 10, "nothing_else": 5}}
    assert apply_not_applicable(reply, RUBRIC) is reply
    assert apply_not_applicable({"score": 42}, RUBRIC) == {"score": 42}


def test_the_reviewer_routes_on_the_recomputed_score(tmp_path):
    fs, _ = _review(tmp_path, _config(), {
        "score": 5, "verdict": "MANUAL", "feedback": "nothing here for checks 1-4",
        "checks": {"java_and_bom": "n/a", "jakarta_coords": "n/a", "plugins_java21": "n/a",
                   "eliminations_exact": "n/a", "nothing_else": 15}})
    assert fs["review_score"] == 100 and fs["review_verdict"] == "PASS"
