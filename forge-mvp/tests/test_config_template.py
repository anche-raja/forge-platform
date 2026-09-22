"""`agents.yaml.example` must be a usable reference and an unusable config.

It is the file a new user copies, so two things have to hold: it documents every
key the generated config carries, and it refuses to run rather than failing
three model calls in with a Bedrock error about the wrong thing.
"""

from pathlib import Path

import pytest
import yaml

from forge.config import ConfigError, ForgeConfig

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
