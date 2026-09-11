"""Unit tests for the deterministic checkers, cost model, writer and telemetry."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.utils.cost import accrue, estimate_cost, usage_from_response
from forge.utils.file_writer import write_output
from forge.utils.java_checks import find_unmigrated_javax_imports, is_jdk_javax
from forge.utils.telemetry import MetricsEmitter, PIPELINE_METRICS

from tests.conftest import write_config

PRICING = {"m": {"input_per_1k": 0.003, "output_per_1k": 0.015}}


# ─── java_checks ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("imp", [
    "javax.servlet.http.HttpServletRequest",
    "javax.persistence.Entity",
    "javax.validation.Valid",
    "javax.xml.bind.JAXBContext",   # Jakarta despite the javax.xml prefix
    "javax.annotation.PostConstruct",
])
def test_jakarta_imports_are_flagged(imp):
    assert find_unmigrated_javax_imports(f"import {imp};") == [imp]


@pytest.mark.parametrize("imp", [
    "javax.crypto.Cipher",
    "javax.sql.DataSource",
    "javax.net.ssl.SSLContext",
    "javax.naming.InitialContext",
    "javax.xml.parsers.DocumentBuilder",     # JDK, unlike javax.xml.bind
    "javax.annotation.processing.Processor",  # JDK, unlike javax.annotation
])
def test_jdk_imports_are_left_alone(imp):
    assert find_unmigrated_javax_imports(f"import {imp};") == []
    assert is_jdk_javax(imp)


def test_static_and_wildcard_imports():
    src = "import static javax.persistence.CascadeType.ALL;\nimport javax.servlet.*;"
    assert set(find_unmigrated_javax_imports(src)) == {
        "javax.persistence.CascadeType.ALL", "javax.servlet.*",
    }


def test_javax_in_a_comment_or_string_is_not_an_import():
    src = '// migrate javax.servlet later\nString s = "javax.persistence.Entity";'
    assert find_unmigrated_javax_imports(src) == []


# ─── cost ────────────────────────────────────────────────────────────────────

def test_estimate_cost_basic():
    assert estimate_cost("m", 1000, 1000, PRICING) == pytest.approx(0.018)


@pytest.mark.parametrize("model,pricing", [
    ("unknown", PRICING), ("m", {}), ("", PRICING), ("m", {"m": "not-a-dict"}),
])
def test_estimate_cost_degrades_to_zero(model, pricing):
    assert estimate_cost(model, 1000, 1000, pricing) == 0.0


def test_usage_from_response_is_mock_safe():
    """MagicMock attribute access returns a Mock, not an int — must not crash
    or invent a cost."""
    assert usage_from_response(MagicMock()) == (0, 0)
    assert usage_from_response(object()) == (0, 0)


def test_accrue_adds_to_running_total():
    r = MagicMock()
    r.usage_metadata = {"input_tokens": 1000, "output_tokens": 1000}
    assert accrue({"estimated_cost_usd": 1.0}, r, "m", PRICING) == pytest.approx(1.018)


# ─── file_writer ─────────────────────────────────────────────────────────────

SRC = "package com.corp.demo;\npublic class UserService {}\n"
EXPECTED = Path("src/main/java/com/corp/demo/UserService.java")


@pytest.mark.parametrize("key", [
    "UserService.java",                                  # bare name
    "src/main/java/com/corp/demo/UserService.java",      # relative with dirs
])
def test_writer_preserves_package_path(tmp_path, key):
    out, proj = tmp_path / "out", tmp_path / "proj"
    proj.mkdir()
    write_output({
        "dry_run": False, "output_dir": str(out), "source_dir": str(proj),
        "current_file": {"transform_output": {"files": {key: SRC}}},
    })
    assert (out / EXPECTED).read_text(encoding="utf-8") == SRC


def test_writer_refuses_path_traversal(tmp_path):
    out, proj = tmp_path / "out", tmp_path / "proj"
    proj.mkdir()
    write_output({
        "dry_run": False, "output_dir": str(out), "source_dir": str(proj),
        "current_file": {"transform_output": {"files": {"../../evil.java": SRC}}},
    })
    assert not (tmp_path / "evil.java").exists()
    assert not list(tmp_path.rglob("evil.java"))


def test_writer_is_noop_in_dry_run(tmp_path):
    out = tmp_path / "out"
    write_output({
        "dry_run": True, "output_dir": str(out), "source_dir": str(tmp_path),
        "current_file": {"transform_output": {"files": {"A.java": SRC}}},
    })
    assert not out.exists()


# ─── telemetry ───────────────────────────────────────────────────────────────

def test_metrics_disabled_by_config(tmp_path):
    cfg = write_config(tmp_path, emit_cloudwatch_metrics=False)
    assert MetricsEmitter(cfg).emit({"files_processed": 1}) is False


def test_metrics_emit_shape_matches_terraform(tmp_path, monkeypatch):
    """Alarms declare no dimensions, so metrics must be published without any."""
    cfg = write_config(tmp_path, emit_cloudwatch_metrics=True)
    emitter = MetricsEmitter(cfg, enabled=False)
    client = MagicMock()
    emitter._client, emitter.enabled = client, True

    assert emitter.emit({"files_processed": 1, "review_score": 80, "bogus_metric": 5}) is True
    kwargs = client.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "FORGE/Migration"
    names = {d["MetricName"] for d in kwargs["MetricData"]}
    assert names == {"files_processed", "review_score"}   # unknown metric dropped
    assert all("Dimensions" not in d for d in kwargs["MetricData"])
    assert names <= set(PIPELINE_METRICS)


def test_metrics_failure_is_not_fatal(tmp_path):
    """A telemetry outage must never abort a migration run."""
    cfg = write_config(tmp_path, emit_cloudwatch_metrics=True)
    emitter = MetricsEmitter(cfg, enabled=False)
    client = MagicMock()
    client.put_metric_data.side_effect = RuntimeError("no credentials")
    emitter._client, emitter.enabled = client, True

    assert emitter.emit({"files_processed": 1}) is False   # swallowed, not raised
