"""Build verification: a file can score 95 and still not compile.

Where a real javac is on PATH these tests compile genuine Java rather than
mocking subprocess, so the command construction is exercised end to end.
"""

import contextlib
import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from forge.verify.build_verifier import BuildVerifier
from tests.conftest import llm_reply, make_state, write_config
from tests.test_phase0_closeout import _graph_mocks

HAS_JAVAC = shutil.which("javac") is not None
needs_javac = pytest.mark.skipif(not HAS_JAVAC, reason="javac not on PATH")

VALID_JAVA = (
    "package com.corp.demo;\n"
    "public class Widget {\n"
    "    public int size() { return 1; }\n"
    "}\n"
)
BROKEN_JAVA = (
    "package com.corp.demo;\n"
    "public class Widget {\n"
    "    public int size() { return \"not an int\" }\n"   # type error + missing semicolon
    "}\n"
)


# Graph-level fixtures: the public type must match the java_file fixture's name
# (javac requires it), and the graph writes to UserAction.java.
GRAPH_VALID = """\
package com.corp.user;
public class UserAction {
    public int size() { return 1; }
}
"""

GRAPH_BROKEN = """\
package com.corp.user;
public class UserAction {
    public int size() { return "not an int" }
}
"""


def _enabled(tmp_path, **over):
    settings = {"enabled": True, "mode": "javac", "command": "", "classpath": "", "timeout_seconds": 120}
    settings.update(over)
    return write_config(tmp_path, build_verification=settings)


def _write(tmp_path, source, name="Widget.java"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


# ─── verifier in isolation ───────────────────────────────────────────────────

def test_disabled_by_default(tmp_path):
    v = BuildVerifier(write_config(tmp_path))
    assert v.verify(make_state("x", tmp_path, dry_run=False))["verdict"] == "SKIPPED"


def test_dry_run_skips(tmp_path):
    v = BuildVerifier(_enabled(tmp_path))
    assert v.verify(make_state("x", tmp_path, dry_run=True))["verdict"] == "SKIPPED"


@needs_javac
def test_valid_java_compiles(tmp_path):
    src = _write(tmp_path, VALID_JAVA)
    state = make_state(src, tmp_path, dry_run=False)
    state["current_file"]["written_paths"] = [src]
    result = BuildVerifier(_enabled(tmp_path)).verify(state)
    assert result["verdict"] == "PASS", result["output"]


@needs_javac
def test_broken_java_fails_with_compiler_output(tmp_path):
    src = _write(tmp_path, BROKEN_JAVA)
    state = make_state(src, tmp_path, dry_run=False)
    state["current_file"]["written_paths"] = [src]
    result = BuildVerifier(_enabled(tmp_path)).verify(state)
    assert result["verdict"] == "FAIL"
    assert "Widget.java" in result["output"]      # actionable feedback for the retry


def test_missing_toolchain_skips_rather_than_fails(tmp_path):
    """A missing compiler is an environment problem — it must not condemn the
    migration to manual review."""
    src = _write(tmp_path, VALID_JAVA)
    state = make_state(src, tmp_path, dry_run=False)
    state["current_file"]["written_paths"] = [src]
    with patch("forge.verify.build_verifier.shutil.which", return_value=None):
        result = BuildVerifier(_enabled(tmp_path)).verify(state)
    assert result["verdict"] == "SKIPPED"
    assert "not found on PATH" in result["output"]


def test_timeout_is_a_failure(tmp_path):
    src = _write(tmp_path, VALID_JAVA)
    state = make_state(src, tmp_path, dry_run=False)
    state["current_file"]["written_paths"] = [src]
    with (
        patch("forge.verify.build_verifier.shutil.which", return_value="/usr/bin/javac"),
        patch("forge.verify.build_verifier.subprocess.run",
              side_effect=subprocess.TimeoutExpired(cmd="javac", timeout=1)),
    ):
        result = BuildVerifier(_enabled(tmp_path, timeout_seconds=1)).verify(state)
    assert result["verdict"] == "FAIL"
    assert "timed out" in result["output"]


def test_maven_mode_targets_the_output_project(tmp_path):
    v = BuildVerifier(_enabled(tmp_path, mode="maven"))
    cmd = v._build_command(["/a/A.java"], "/out", "/tmp/work")
    assert cmd == ["mvn", "-q", "-B", "compile", "-f", "/out"]


def test_command_mode_substitutes_placeholders(tmp_path):
    v = BuildVerifier(_enabled(tmp_path, mode="command", command="gradle build -p {output_dir}"))
    assert v._build_command(["/a/A.java"], "/out", "/w") == ["gradle", "build", "-p", "/out"]


# ─── verifier inside the graph ───────────────────────────────────────────────

@needs_javac
def test_build_failure_retries_with_compiler_errors(tmp_path, java_file):
    """A failed compile must consume a retry and inject the compiler output
    into the transform prompt — the same channel as a low review score."""
    config = _enabled(tmp_path)
    out_dir = tmp_path / "migrated"

    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=95)

        # First transform emits code that does not compile; every retry fixes it.
        # A callable rather than a list so the test does not depend on exactly
        # how many times LangGraph re-enters the node.
        calls = {"n": 0}

        def transform(_messages):
            calls["n"] += 1
            source = GRAPH_BROKEN if calls["n"] == 1 else GRAPH_VALID
            return llm_reply({"files": {java_file: source}, "manual_flags": []})

        up.return_value.invoke.side_effect = transform
        from forge.graph import build_graph

        app = build_graph(config)
        state = make_state(java_file, tmp_path, dry_run=False)
        state["output_dir"] = str(out_dir)
        result = app.invoke(state, config={"configurable": {"thread_id": java_file}})

    fs = result["current_file"]
    assert fs["status"] == "DONE"
    assert fs["retry_count"] == 1
    assert fs["build_verdict"] == "PASS"

    # The second transform call must have carried the compiler errors.
    retry_prompt = up.return_value.invoke.call_args_list[1][0][0][1].content
    assert "does not compile" in retry_prompt
    assert "UserAction.java" in retry_prompt


@needs_javac
def test_build_failure_escalates_when_retries_exhausted(tmp_path, java_file):
    config = _enabled(tmp_path)
    config._cfg["max_retries"] = 1

    with contextlib.ExitStack() as stack:
        up = _graph_mocks(stack, java_file, review_score=95)
        up.return_value.invoke.return_value = llm_reply(
            {"files": {java_file: GRAPH_BROKEN}, "manual_flags": []}
        )
        from forge.graph import build_graph

        app = build_graph(config)
        state = make_state(java_file, tmp_path, dry_run=False)
        state["output_dir"] = str(tmp_path / "migrated")
        result = app.invoke(state, config={"configurable": {"thread_id": java_file}})

    fs = result["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert fs["build_verdict"] == "FAIL"
    assert result["files_manual"] == 1


def test_disabled_verification_leaves_file_done(tmp_path, java_file):
    """With verification off the graph must behave exactly as before."""
    from tests.conftest import CLEAN_JAVA

    with contextlib.ExitStack() as stack:
        _graph_mocks(stack, java_file, review_score=95)
        from forge.graph import build_graph

        app = build_graph(write_config(tmp_path))
        result = app.invoke(
            make_state(java_file, tmp_path),
            config={"configurable": {"thread_id": java_file}},
        )

    fs = result["current_file"]
    assert fs["status"] == "DONE"
    assert fs["build_verdict"] == "SKIPPED"


def test_javac_mode_ignores_non_java_written_files(tmp_path):
    """A migrated web.xml or a generated server.xml is not something to hand to
    javac; with nothing else written the gate is SKIPPED, not FAIL."""
    v = BuildVerifier(_enabled(tmp_path))
    state = make_state("x", tmp_path, dry_run=False)
    state["current_file"]["written_paths"] = [str(tmp_path / "out/WEB-INF/web.xml"),
                                              str(tmp_path / "out/src/main/liberty/config/server.xml")]
    result = v.verify(state)
    assert result["verdict"] == "SKIPPED"
    assert "no Java sources" in result["output"]
