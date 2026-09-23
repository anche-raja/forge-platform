"""`agents.yaml.example` must be a usable reference and an unusable config.

It is the file a new user copies, so two things have to hold: it documents every
key the generated config carries, and it refuses to run rather than failing
three model calls in with a Bedrock error about the wrong thing.
"""

from pathlib import Path

import pytest
import yaml

from forge.config import (DEFAULT_BEDROCK_READ_TIMEOUT, DEFAULT_MODEL_MAX_TOKENS, ConfigError,
                          ForgeConfig, bedrock_client_config, model_max_tokens)

EXAMPLE = Path(__file__).resolve().parents[1] / "agents.yaml.example"


def test_the_example_still_carries_the_placeholder_and_is_refused(tmp_path):
    """The placeholder is deliberate — it must fail loudly, not reach Bedrock.

    Left unguarded it produced `ValidationException: Guardrail was enabled but
    input is in incorrect format` under a botocore traceback, which names
    neither the real cause nor the fix.
    """
    with pytest.raises(ConfigError) as e:
        ForgeConfig(str(EXAMPLE))
    message = str(e.value)
    assert "guardrail_id" in message, "it names the key that is wrong"
    assert "generate-agents-yaml.sh" in message, "and the command that fixes it"


def test_a_generated_config_loads(tmp_path):
    """The guard must only reject the placeholder, not any real value."""
    good = dict(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    good["guardrail_id"] = "nbakjwjan4hf"
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(good), encoding="utf-8")
    assert ForgeConfig(str(path)).guardrail_id == "nbakjwjan4hf"


def test_the_example_documents_the_secret_gate():
    """`secret_scan` is a first-class control in GUARDRAILS.md §9.

    It was missing here, so the template a user copies configured no secret gate
    at all while the docs described tuning it.
    """
    cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert "secret_scan" in cfg
    assert cfg["secret_scan"]["action"] == "block", "the safe default, not warn"
    assert "entropy" in cfg["secret_scan"] and "allow" in cfg["secret_scan"]
    assert cfg.get("preflight_model_check") is False, "source must not reach a model pre-gate"


def test_the_example_covers_every_key_the_generator_emits():
    """A key only in the generated file is a key nobody can discover.

    Read from the generator script rather than a local `agents.yaml`, which is
    gitignored and absent in CI.
    """
    script = (Path(__file__).resolve().parents[2]
              / "forge-terraform/scripts/generate-agents-yaml.sh")
    if not script.is_file():
        pytest.skip("generator script not present in this checkout")

    # Only the heredoc is YAML; the rest is shell, whose `VAR="..."` lines and
    # `echo "==> ..."` would otherwise read as keys.
    lines = script.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("cat <<"))
    except StopIteration:
        pytest.skip("generator no longer emits a heredoc")
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() == "EOF":
            break
        body.append(ln)

    emitted = set()
    for line in body:
        stripped = line.strip()
        # Top-level keys are the unindented ones; `key:` with nothing after it
        # (a block header) counts too.
        if line[:1].isalpha() and ":" in stripped and not stripped.startswith("#"):
            emitted.add(stripped.split(":", 1)[0])
    assert emitted, "parsed no keys — the generator's shape changed, so this test is lying"

    documented = set(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    missing = {k for k in emitted if k not in documented}
    assert not missing, f"generated but undocumented in agents.yaml.example: {sorted(missing)}"


def test_every_model_call_sets_an_output_budget():
    """No ChatBedrockConverse may be built without max_tokens.

    Unset, langchain-aws omits maxTokens from the Converse request and Bedrock
    applies its own much smaller default. A transform that has to return a whole
    file inside a JSON envelope is then cut off mid-object -- and on a reasoning
    model that spends the budget before emitting any text, the content block
    comes back empty, reaching extract_json as "" and failing as "Expecting
    value: line 1 column 1 (char 0)". A live run held all 10 POMs of a Maven
    reactor that way, reported as a parse error that named nothing real.

    A source-level check rather than a mocked one: the bug is a missing
    argument, and only reading every construction site can prove none regressed.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "forge"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name != "ChatBedrockConverse":
                continue
            if not any(kw.arg == "max_tokens" for kw in node.keywords):
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")

    assert not offenders, "ChatBedrockConverse built with no max_tokens at: " + ", ".join(offenders)


@pytest.mark.parametrize("model,expected", [
    ("us.amazon.nova-pro-v1:0", 10000),              # Converse rejects anything above
    ("us.anthropic.claude-opus-4-8", DEFAULT_MODEL_MAX_TOKENS),
    (None, DEFAULT_MODEL_MAX_TOKENS),
])
def test_the_budget_never_exceeds_the_models_own_limit(model, expected):
    assert model_max_tokens(ForgeConfig(data={}), model) == expected


def test_a_smaller_shared_budget_still_wins_and_limits_can_be_overridden():
    assert model_max_tokens(ForgeConfig(data={"max_tokens": 4096}), "us.amazon.nova-pro-v1:0") == 4096
    cfg = ForgeConfig(data={"model_output_limits": {"claude-opus": 8192}})
    assert model_max_tokens(cfg, "us.anthropic.claude-opus-4-8") == 8192


def test_every_bedrock_client_sets_a_read_timeout():
    """No Bedrock client may fall back to botocore's 60s read timeout.

    Converse is not streamed, so a large pom.xml reply sends nothing until it is
    done, and a live run died with ReadTimeoutError at 60s. Same source-level
    check as the output budget: the bug is a missing argument.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "forge"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            bedrock_boto = (name == "client" and node.args and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == "bedrock-runtime")
            if name != "ChatBedrockConverse" and not bedrock_boto:
                continue
            if not any(kw.arg == "config" for kw in node.keywords):
                offenders.append(f"{path.relative_to(root.parent)}:{node.lineno}")

    assert not offenders, "Bedrock client built with no config (60s timeout) at: " + ", ".join(offenders)


@pytest.mark.parametrize("raw,expected", [
    (None, DEFAULT_BEDROCK_READ_TIMEOUT),
    ("soon", DEFAULT_BEDROCK_READ_TIMEOUT),
    (5, DEFAULT_BEDROCK_READ_TIMEOUT),      # below botocore's own default; a mistake
    (900, 900),
])
def test_the_read_timeout_falls_back_rather_than_crippling_a_run(raw, expected):
    cfg = ForgeConfig(data={} if raw is None else {"bedrock_read_timeout": raw})
    assert bedrock_client_config(cfg).read_timeout == expected


def test_the_trial_model_is_documented_and_priced():
    """Unpriced, a trial run's cost silently accrues as $0.00 -- the one number it is for."""
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    trial = example.get("trial_transform_model")
    assert trial, "trial_transform_model must be documented"
    assert trial in (example.get("model_pricing") or {}), f"{trial} is not in model_pricing"


def test_the_example_documents_the_output_budget():
    """The key has to be in the template, or a generated config silently omits it."""
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(example.get("max_tokens"), int), "max_tokens must be documented"
    assert example["max_tokens"] >= 8192, "too small to return a few-hundred-line file"


@pytest.mark.parametrize("raw,expected", [
    (None, DEFAULT_MODEL_MAX_TOKENS),      # key absent
    ("not a number", DEFAULT_MODEL_MAX_TOKENS),
    (10, DEFAULT_MODEL_MAX_TOKENS),        # too low to return anything; treated as a mistake
    (32000, 32000),                        # a deliberate raise is honoured
])
def test_the_budget_falls_back_rather_than_crippling_a_run(raw, expected):
    cfg = ForgeConfig(data={} if raw is None else {"max_tokens": raw})
    assert model_max_tokens(cfg) == expected
