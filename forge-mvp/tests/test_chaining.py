"""A second pack builds on the first one's output instead of replacing it.

Packs read the original source, so running two over the same files loses the
first one's work — verified against a live run, and now refused outright
(`PackOverlap`). Refusing is right for the CLI, where the operator can choose a
different output directory. It is not enough for the chat surface: the user says
"migrate my app" and nothing else, and the leader has to sequence a ten-pack
plan without being told how. `chain=True` is how it does that.
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

import pytest

from forge import service
from forge.utils import run_manifest
from tests.conftest import llm_reply, mocked_aws, write_config

JAVAX = """package com.corp;
import javax.servlet.http.HttpServletRequest;
public class A { void f(HttpServletRequest r) {} }
"""
JAKARTA = JAVAX.replace("javax.servlet", "jakarta.servlet")
JAKARTA_21 = JAKARTA.replace("public class A", "public final class A")


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "app"
    (src / "src/main/java/com/corp").mkdir(parents=True)
    (src / "src/main/java/com/corp/A.java").write_text(JAVAX, encoding="utf-8")
    (src / "pom.xml").write_text(
        "<project><properties><maven.compiler.source>1.8</maven.compiler.source>"
        "</properties></project>", encoding="utf-8")
    return src, tmp_path / "migrated"


def _run(tmp_path, src, out, phase, produces, **kw):
    """One real run_migration with the model replaced by a fixed reply."""
    rel = "src/main/java/com/corp/A.java"
    with mocked_aws(write_config(tmp_path), review_score=95) as mocks:
        mocks["upgrade"].return_value.invoke.side_effect = (
            lambda messages: llm_reply({"files": {rel: produces}, "manual_flags": []}))
        return service.run_migration(str(src), phase, str(out),
                                     write_config(tmp_path), no_metrics=True, **kw)


def test_a_second_pack_reads_the_first_packs_output_not_the_original(project, tmp_path):
    """The whole point: the chained run must see `jakarta`, not `javax`."""
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", JAKARTA)
    assert "jakarta.servlet" in (out / "src/main/java/com/corp/A.java").read_text()

    seen = {}
    rel = "src/main/java/com/corp/A.java"
    with mocked_aws(write_config(tmp_path), review_score=95) as mocks:
        def capture(messages):
            seen["human"] = messages[1].content
            return llm_reply({"files": {rel: JAKARTA_21}, "manual_flags": []})
        mocks["upgrade"].return_value.invoke.side_effect = capture
        service.run_migration(str(src), "java8-to-java21", str(out),
                              write_config(tmp_path), no_metrics=True, chain=True)

    assert "jakarta.servlet" in seen["human"], "the chained pack was handed the migrated file"
    assert "javax.servlet" not in seen["human"], "and not the original"
    # And the second pack's own change landed on top of the first one's.
    final = (out / rel).read_text(encoding="utf-8")
    assert "jakarta.servlet" in final and "public final class A" in final


def test_without_chain_the_second_pack_is_refused(project, tmp_path):
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", JAKARTA)
    with pytest.raises(service.PackOverlap) as e:
        _run(tmp_path, src, out, "java8-to-java21", JAKARTA_21)
    assert "javax-to-jakarta" in str(e.value)


def test_chaining_is_exempt_from_the_overlap_guard(project, tmp_path):
    """Overwriting is the *point* when the input was that output."""
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", JAKARTA)
    result = _run(tmp_path, src, out, "java8-to-java21", JAKARTA_21, chain=True)
    assert result.totals["passed"] == 1


def test_a_chained_run_says_so(project, tmp_path):
    """A run that silently changed what it read would be impossible to debug."""
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", JAKARTA)
    events = []
    _run(tmp_path, src, out, "java8-to-java21", JAKARTA_21,
         chain=True, on_event=events.append)
    assert any(e["type"] == "chained" for e in events)


def test_the_manifest_records_what_a_pack_retired(project, tmp_path):
    """A descriptor an earlier pack replaced must not be handed to the next one.

    Without this the chained view still contains the retired file, and the next
    pack pays to migrate something on its way out of the project.
    """
    src, out = project
    rel = "src/main/java/com/corp/A.java"
    with mocked_aws(write_config(tmp_path), review_score=95) as mocks:
        mocks["upgrade"].return_value.invoke.side_effect = (
            lambda messages: llm_reply(
                {"files": {rel: JAKARTA}, "deleted_files": ["pom.xml"], "manual_flags": []}))
        service.run_migration(str(src), "javax-to-jakarta", str(out),
                              write_config(tmp_path), no_metrics=True)

    assert run_manifest.deleted_paths(str(out)) == ["pom.xml"]


def test_an_old_flat_manifest_is_still_readable(tmp_path):
    """The first manifest shape was a bare {path: phase} map of writes."""
    out = tmp_path / "migrated"
    out.mkdir()
    (out / run_manifest.MANIFEST_NAME).write_text('{"a/B.java": "javax-to-jakarta"}', encoding="utf-8")
    assert run_manifest.load(str(out)) == {"a/B.java": "javax-to-jakarta"}
    assert run_manifest.deleted_paths(str(out)) == []
