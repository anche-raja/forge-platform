"""The project's own build, run over source + output before landing.

No toolchain needed: every subprocess is a fake. The one live proof is a
manual run against a real multi-reactor project (see the plan's verification);
these pin the decisions around it -- what to build, in which order, with which
JDK, what a failure reports, when a verdict is stale, and that the leader sees
the verdict but never the compiler output.
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import service
from forge.leader import cards
from forge.leader.convo import Conversation
from forge.leader.settings import LeaderSettings
from forge.leader.tools import ProjectContext, Toolbox
from forge.verify import project_build
from tests.conftest import write_config

NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def _pom(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<project {NS}><modelVersion>4.0.0</modelVersion>{body}</project>", encoding="utf-8")


@pytest.fixture
def reactors(tmp_path):
    """AMS's shape: a parent BOM that manages its own children, and two aggregators."""
    root = tmp_path / "app"
    _pom(root / "parent-bom/pom.xml",
         "<artifactId>parent-bom</artifactId><packaging>pom</packaging>"
         "<dependencyManagement><dependencies>"
         "<dependency><artifactId>shared-lib</artifactId></dependency>"
         "<dependency><artifactId>web</artifactId></dependency>"
         "</dependencies></dependencyManagement>")
    _pom(root / "common/pom.xml",
         "<parent><artifactId>parent-bom</artifactId><relativePath>../parent-bom/pom.xml</relativePath></parent>"
         "<artifactId>common</artifactId><modules><module>shared-lib</module></modules>")
    _pom(root / "common/shared-lib/pom.xml",
         "<parent><artifactId>common</artifactId></parent><artifactId>shared-lib</artifactId>")
    _pom(root / "internal/pom.xml",
         "<parent><artifactId>parent-bom</artifactId></parent>"
         "<artifactId>internal</artifactId><modules><module>web</module></modules>")
    _pom(root / "internal/web/pom.xml",
         "<parent><artifactId>internal</artifactId></parent><artifactId>web</artifactId>"
         "<dependencies><dependency><artifactId>shared-lib</artifactId></dependency></dependencies>")
    return root


def _cfg(tmp_path, **project_build_settings):
    return write_config(tmp_path, target_java_version="21", project_build=project_build_settings)


# ─── what to build, in which order ────────────────────────────────────────────

def test_reactors_build_parent_first_and_dependents_last(reactors):
    order = [str(p.relative_to(reactors)) for p in project_build.find_reactors(str(reactors))]
    # internal/web depends on common/shared-lib; everything inherits parent-bom.
    # The BOM managing its own children is not an edge, or it would sort last.
    assert order == ["parent-bom/pom.xml", "common/pom.xml", "internal/pom.xml"]


def test_a_module_is_never_built_as_its_own_reactor(reactors):
    assert all("shared-lib" not in str(p) and "web" not in str(p)
               for p in project_build.find_reactors(str(reactors)))


def test_a_configured_command_replaces_detection(reactors, tmp_path):
    steps = project_build.plan(str(reactors), _cfg(tmp_path, command="./build.sh package"))
    assert [s.argv for s in steps] == [["./build.sh", "package"]]


def test_maven_steps_install_into_an_isolated_repository(reactors, tmp_path):
    steps = project_build.plan(str(reactors), _cfg(tmp_path, maven_repo=str(tmp_path / "m2")))
    assert all(f"-Dmaven.repo.local={tmp_path / 'm2'}" in s.argv and "install" in s.argv for s in steps)


# ─── the project's own build script ───────────────────────────────────────────

def test_a_windows_command_keeps_its_backslashes_and_quoted_paths():
    # POSIX splitting reads a backslash as an escape: C:\tools\x.cmd -> C:toolsx.cmd.
    argv = project_build.split_command(r'"C:\Program Files\Git\bin\bash.exe" build.sh -Dx=C:\m2', posix=False)
    assert argv == [r"C:\Program Files\Git\bin\bash.exe", "build.sh", r"-Dx=C:\m2"]
    assert project_build.split_command("./build.sh 'a b'", posix=True) == ["./build.sh", "a b"]


def test_a_script_in_the_project_runs_from_the_project(reactors, tmp_path, monkeypatch):
    """Not from wherever FORGE was started -- and so it is found even off PATH."""
    (reactors / "build.cmd").write_text("@echo off\n", encoding="utf-8")
    steps = project_build.plan(str(reactors), _cfg(tmp_path, command="build.cmd install"))
    assert steps[0].argv == [str(reactors.resolve() / "build.cmd"), "install"]

    monkeypatch.setattr(project_build.shutil, "which", lambda tool: None)
    result = project_build.run(str(reactors), _cfg(tmp_path, command="build.cmd install", java_home="/jdk"),
                               runner=lambda argv, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert result.outcome == "pass"


def test_a_build_script_can_be_pointed_at_the_isolated_repository(reactors, tmp_path):
    """A script that installs into the everyday ~/.m2 would replace the original's snapshots."""
    steps = project_build.plan(str(reactors), _cfg(
        tmp_path, command="bash build-jdk21.sh install -Dmaven.repo.local={maven_repo}",
        maven_repo=str(tmp_path / "m2")))
    assert steps[0].argv[-1] == f"-Dmaven.repo.local={tmp_path / 'm2'}"


def test_configured_variables_reach_the_build_and_win(reactors, tmp_path, monkeypatch):
    monkeypatch.setattr(project_build.shutil, "which", lambda tool: "/usr/bin/" + tool)
    seen = {}

    def runner(argv, **kw):
        seen.update(kw["env"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    config = _cfg(tmp_path, command="bash build.sh", java_home="/jdk",
                  env={"AMS_BUILD_DRIVE": "Y:", "JAVA_HOME": "/script/jdk"})
    assert project_build.run(str(reactors), config, runner=runner).outcome == "pass"
    assert seen["AMS_BUILD_DRIVE"] == "Y:"
    assert seen["JAVA_HOME"] == "/script/jdk", "an explicit variable outranks the resolved JDK"


# ─── which JDK ────────────────────────────────────────────────────────────────

def test_java_home_prefers_config_then_the_target_version_then_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JAVA_HOME", "/env/jdk")
    assert project_build.resolve_java_home(_cfg(tmp_path, java_home="/pinned/jdk")) == "/pinned/jdk"

    asked = []

    def java_home(argv, **kw):
        asked.append(argv)
        return SimpleNamespace(returncode=0, stdout="/jdks/21\n")

    if Path("/usr/libexec/java_home").exists():
        assert project_build.resolve_java_home(_cfg(tmp_path), run=java_home) == "/jdks/21"
        assert asked[-1][-1] == "21"

    def none(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="")

    assert project_build.resolve_java_home(_cfg(tmp_path), run=none) == "/env/jdk"


# ─── running it ───────────────────────────────────────────────────────────────

def test_a_failing_step_stops_the_rest_and_reports_its_tail(reactors, tmp_path, monkeypatch):
    monkeypatch.setattr(project_build.shutil, "which", lambda tool: "/usr/bin/" + tool)
    calls = []
    root = str(reactors.resolve())

    def runner(argv, **kw):
        calls.append(argv[-1])
        if "common" in argv[-1]:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr=f"[ERROR] {root}/common/shared-lib/src/A.java:[3,1] ';' expected\n[ERROR]\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = project_build.run(str(reactors), _cfg(tmp_path, java_home="/jdk"), runner=runner)
    assert result.outcome == "fail" and result.failed_step == "common/pom.xml"
    assert len(calls) == 2, "internal must not run after common failed"
    assert result.tail == ["[ERROR] common/shared-lib/src/A.java:[3,1] ';' expected"], \
        "paths are project-relative and empty [ERROR] lines are dropped"
    assert project_build.summarize(result.steps) == ["parent-bom/pom.xml: ok", "common/pom.xml: exit 1",
                                                     "internal/pom.xml: not run"]


def test_nothing_to_build_is_a_skip_that_says_what_to_set(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = project_build.run(str(empty), _cfg(tmp_path))
    assert result.outcome == "skip" and "project_build.command" in result.detail


def test_a_missing_build_tool_is_a_skip_not_a_failure(reactors, tmp_path, monkeypatch):
    monkeypatch.setattr(project_build.shutil, "which", lambda tool: None)
    result = project_build.run(str(reactors), _cfg(tmp_path))
    assert result.outcome == "skip" and "mvn" in result.detail


def test_a_timeout_is_a_failure_naming_the_step(reactors, tmp_path, monkeypatch):
    monkeypatch.setattr(project_build.shutil, "which", lambda tool: "/usr/bin/" + tool)

    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    result = project_build.run(str(reactors), _cfg(tmp_path, java_home="/jdk"), runner=runner)
    assert result.outcome == "fail" and result.failed_step == "parent-bom/pom.xml"


# ─── the service: the record, the report, staleness ───────────────────────────

def _fake_run(outcome):
    def run(root, config):
        return project_build.BuildResult(outcome, f"{outcome} detail", failed_step=None,
                                         tail=["line"] if outcome == "fail" else [])
    return run


def test_build_project_records_the_verdict_and_notices_when_it_goes_stale(reactors, tmp_path, monkeypatch):
    out = tmp_path / "out"
    (out / "common/shared-lib").mkdir(parents=True)
    migrated = out / "common/shared-lib/A.java"
    migrated.write_text("class A {}", encoding="utf-8")
    (out / "migration-report.md").write_text("# FORGE Migration Report\n", encoding="utf-8")

    assert service.build_status(str(reactors), str(out))["outcome"] == "not_run"

    monkeypatch.setattr(project_build, "run", _fake_run("fail"))
    events = []
    record = service.build_project(str(reactors), str(out), _cfg(tmp_path), on_event=events.append)
    assert record["outcome"] == "fail"
    assert json.loads((out / service.PROJECT_BUILD_NAME).read_text())["outcome"] == "fail"
    assert [e["type"] for e in events] == ["build_start", "build"]

    status = service.build_status(str(reactors), str(out))
    assert status["outcome"] == "fail" and status["stale"] is False

    # A FORGE report changing is not the project changing...
    (out / "migration-report.md").write_text("# rewritten\n", encoding="utf-8")
    assert service.build_status(str(reactors), str(out))["stale"] is False
    # ...a migrated file changing is.
    migrated.write_text("class A { int x; }", encoding="utf-8")
    assert service.build_status(str(reactors), str(out))["stale"] is True


def test_the_report_keeps_one_build_section_however_often_it_builds(reactors, tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "migration-report.md").write_text("# FORGE Migration Report\n\n## Summary\nx\n", encoding="utf-8")
    monkeypatch.setattr(project_build, "run", _fake_run("fail"))
    service.build_project(str(reactors), str(out), _cfg(tmp_path))
    monkeypatch.setattr(project_build, "run", _fake_run("pass"))
    service.build_project(str(reactors), str(out), _cfg(tmp_path))

    report = (out / "migration-report.md").read_text(encoding="utf-8")
    assert report.count("## Project build") == 1 and "PASS" in report and "## Summary" in report


# ─── the chat ─────────────────────────────────────────────────────────────────

@pytest.fixture
def box(reactors, tmp_path):
    config = _cfg(tmp_path)
    ctx = ProjectContext(source_dir=str(reactors), output_dir=str(tmp_path / "out"),
                         config=config, base_config=config, bound=True)
    return Toolbox(ctx, Conversation(), LeaderSettings.from_config(config), lambda e: None, None)


def test_the_leader_sees_the_verdict_and_never_the_compiler_output(box, monkeypatch):
    monkeypatch.setattr(project_build, "run", _fake_run("fail"))
    outcome = box.execute("build_project", {}, tool_id="t1")
    assert outcome.ok
    assert outcome.observation["outcome"] == "fail"
    assert "tail" not in outcome.observation, "compiler output must not reach the model"
    assert [c["kind"] for c in outcome.cards] == ["build"] and outcome.cards[0]["tail"] == ["line"]


@pytest.mark.parametrize("state", ["not_run", "fail"])
def test_landing_shows_the_build_and_still_offers_the_click(box, monkeypatch, state):
    if state == "fail":
        monkeypatch.setattr(project_build, "run", _fake_run("fail"))
        box.execute("build_project", {}, tool_id="t1")
    outcome = box.execute("land_on_branch", {"branch": "forge/x"}, tool_id="t2")
    assert outcome.needs_confirmation, "a failed or missing build warns; it never blocks"
    assert outcome.cards[0]["build"]["outcome"] == state
    assert outcome.observation["build"]["outcome"] == state


def test_cards_build_status_obs_is_what_both_surfaces_share():
    obs = cards.build_status_obs({"outcome": "pass", "stale": True, "detail": "ok", "tail": ["x"]})
    assert obs["outcome"] == "pass" and obs["stale"] is True and "tail" not in obs
