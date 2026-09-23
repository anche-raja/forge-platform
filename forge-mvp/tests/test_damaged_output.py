"""Damage an earlier run wrote is re-checked before a chained run reads it (#13).

On AMS a java8-to-java21 run left ``}ßßß`` in OrderType.java and a doubled
``}`` in two more files. Every chained pack after it read those files as its
input, the content filter passed over them (nothing left to modernise), and
the per-file syntax check only ever sees fresh model output -- so the damage
rode along to the project build, which failed on it.

A chained run with ``syntax_check`` on now parses what ``.forge-writes.json``
says FORGE wrote, moves any copy that no longer parses under
``.forge-staging/.damaged/`` (never deletes it), and names the file and the
pack that must run again in an event, the run's report and the plan summary.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge import service
from forge.config import ForgeConfig
from forge.utils import run_manifest
from forge.verify import syntax
from tests.conftest import llm_reply, mocked_aws, write_config

needs_javac = pytest.mark.skipif(not syntax.find_javac(ForgeConfig(data={})), reason="no javac on this machine")

REL = "src/main/java/com/corp/OrderType.java"
ORIGINAL = ("package com.corp;\nimport javax.servlet.Filter;\n"
            "public class OrderType { boolean f(Object r) { return r instanceof Filter; } }\n")
# The AMS damage, shape for shape: a stray tail after a closing brace.
DAMAGED = ORIGINAL.replace("javax.", "jakarta.").rstrip("\n") + "ßßß\n}\n"
XML_REL = "src/main/resources/beans.xml"
XML_ORIGINAL = "<beans><bean id='a'/></beans>\n"
XML_DAMAGED = "<beans><bean id='a'/></beans>\n</beans>\n"


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "app"
    (src / "src/main/java/com/corp").mkdir(parents=True)
    (src / "src/main/resources").mkdir(parents=True)
    (src / REL).write_text(ORIGINAL, encoding="utf-8")
    (src / XML_REL).write_text(XML_ORIGINAL, encoding="utf-8")
    (src / "pom.xml").write_text("<project><properties><maven.compiler.source>1.8</maven.compiler.source>"
                                 "</properties></project>", encoding="utf-8")
    return src, tmp_path / "migrated"


def _plant(out: Path, rel: str, text: str, pack: str) -> None:
    """What an earlier run left: the file, and the manifest saying which pack wrote it."""
    target = out / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    run_manifest.record(str(out), pack, [str(target)])


def _chained(tmp_path, src, out, phase="java8-to-java21", events=None, **kw):
    seen = []
    cfg = write_config(tmp_path, syntax_check=True)
    with mocked_aws(review_score=95) as mocks:
        def transform(messages):
            path = messages[1].content.split("File path: ", 1)[1].splitlines()[0]
            seen.append(messages[1].content)
            body = Path(path).read_text(encoding="utf-8")
            return llm_reply({"files": {path: body.replace("javax.", "jakarta.")}, "manual_flags": []})
        mocks["upgrade"].return_value.invoke.side_effect = transform
        result = service.run_migration(str(src), phase, str(out), cfg, no_metrics=True, chain=True,
                                       on_event=(events.append if events is not None else None), **kw)
    return result, seen


@needs_javac
def test_a_damaged_java_file_is_moved_aside_and_the_pack_reads_the_original(project, tmp_path):
    src, out = project
    _plant(out, REL, DAMAGED, "javax-to-jakarta")
    events = []

    result, seen = _chained(tmp_path, src, out, events=events)

    hits = [e for e in events if e["type"] == "damaged_output"]
    assert [(e["file"], e["pack"]) for e in hits] == [(REL, "javax-to-jakarta")]
    moved = out / ".forge-staging/.damaged" / REL
    assert moved.read_text(encoding="utf-8") == DAMAGED, "the damaged copy is kept, not deleted"
    assert seen and "ßßß" not in seen[0], "the chained pack was handed the damage again"
    assert "javax.servlet" in seen[0], "it reads the original source instead"
    assert "ßßß" not in (out / REL).read_text(encoding="utf-8")

    report = (out / "migration-report-java8-to-java21.md").read_text(encoding="utf-8")
    assert "## Damaged output from earlier runs" in report
    assert "re-run **javax-to-jakarta**" in report, "the pack whose work was lost is named"
    summary = (out / "migration-summary.md").read_text(encoding="utf-8")
    assert "## Damaged output reverted to the original" in summary and REL in summary
    assert result.totals["passed"] == 1


def test_a_damaged_file_this_pack_does_not_select_reverts_and_says_which_pack_to_rerun(project, tmp_path):
    """java8-to-java21 never selects an XML file: the report must still say so."""
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    events = []

    _chained(tmp_path, src, out, events=events)

    assert not (out / XML_REL).exists(), "the damaged copy is still where the merged view reads it"
    assert (out / ".forge-staging/.damaged" / XML_REL).is_file()
    assert XML_REL not in run_manifest.load(str(out)), "no pack is recorded as owning a file it no longer has"
    report = (out / "migration-report.md").read_text(encoding="utf-8")
    row = next(line for line in report.splitlines() if XML_REL in line and line.startswith("|"))
    assert "does not select it" in row and "re-run **spring-to-spring6**" in row, row
    record = json.loads((out / "migration-summary.json").read_text(encoding="utf-8"))
    assert record["reverted"][XML_REL]["pack"] == "spring-to-spring6"


def test_the_reverted_note_stays_until_the_owning_pack_runs_again(project, tmp_path):
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "java8-to-java21-not-this-one")
    _chained(tmp_path, src, out)
    summary = json.loads((out / "migration-summary.json").read_text(encoding="utf-8"))
    assert XML_REL in summary["reverted"]

    # Only the owner's own run clears it: pretend java8-to-java21 wrote it, and run that.
    record = json.loads((out / "migration-summary.json").read_text(encoding="utf-8"))
    record["reverted"][XML_REL]["pack"] = "java8-to-java21"
    (out / "migration-summary.json").write_text(json.dumps(record), encoding="utf-8")
    _chained(tmp_path, src, out)
    assert "## Damaged output reverted" not in (out / "migration-summary.md").read_text(encoding="utf-8")


def test_a_human_approved_file_is_moved_too_but_flagged_never_silently(project, tmp_path):
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    (out / "decisions-applied.jsonl").write_text(json.dumps(
        {"file": XML_REL, "pack": "spring-to-spring6", "decision": "approve", "applied": True}) + "\n",
        encoding="utf-8")
    events = []

    _chained(tmp_path, src, out, events=events)

    hit = next(e for e in events if e["type"] == "damaged_output")
    assert hit["approved"] is True
    assert (out / ".forge-staging/.damaged" / XML_REL).is_file()
    summary = (out / "migration-summary.md").read_text(encoding="utf-8")
    row = next(line for line in summary.splitlines() if XML_REL in line)
    assert "| yes |" in row, f"the summary must say a human had approved it: {row}"


def test_a_dry_run_reports_the_damage_and_moves_nothing(project, tmp_path):
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    events = []

    _chained(tmp_path, src, out, events=events, dry_run=True)

    hit = next(e for e in events if e["type"] == "damaged_output")
    assert hit["moved_to"] is None and hit["dry_run"] is True
    assert (out / XML_REL).read_text(encoding="utf-8") == XML_DAMAGED
    assert XML_REL in run_manifest.load(str(out))


def test_a_file_whose_original_is_broken_too_is_reported_and_left_in_place(project, tmp_path):
    """On AMS the source held the damage too: a half-finished landing had copied it back.

    Reverting to an original that does not parse would change nothing, so the
    file stays -- but it still fails the build, so it is still named.
    """
    src, out = project
    (Path(src) / XML_REL).write_text(XML_DAMAGED, encoding="utf-8")
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    events = []

    result = service.check_output(str(src), str(out), write_config(tmp_path), repair=True, on_event=events.append)

    assert [(d["file"], d["source_broken"], d["moved_to"]) for d in result["damaged"]] == [(XML_REL, True, None)]
    assert result["reverted"] == []
    assert (out / XML_REL).is_file(), "left where it was"
    assert XML_REL in run_manifest.load(str(out))
    assert events and events[0]["source_broken"] is True


def test_check_output_on_its_own_only_reports(project, tmp_path):
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    (out / "stray.xml").write_text("<not closed>", encoding="utf-8")   # no run wrote it: not checked

    result = service.check_output(str(src), str(out), write_config(tmp_path))

    assert [d["file"] for d in result["damaged"]] == [XML_REL]
    assert (out / XML_REL).is_file() and not (out / ".forge-staging").exists()


def test_without_syntax_check_a_chained_run_does_not_look(project, tmp_path):
    src, out = project
    _plant(out, XML_REL, XML_DAMAGED, "spring-to-spring6")
    cfg = write_config(tmp_path)          # syntax_check absent: off
    events = []
    with mocked_aws(review_score=95):
        service.run_migration(str(src), "java8-to-java21", str(out), cfg, no_metrics=True, chain=True,
                              on_event=events.append)
    assert not any(e["type"] == "damaged_output" for e in events)
    assert (out / XML_REL).is_file()


def test_the_cli_says_what_was_moved_and_which_pack_to_rerun(capsys):
    import migrate

    migrate._print_event({"type": "damaged_output", "file": REL, "pack": "java8-to-java21", "approved": True,
                          "moved_to": f".forge-staging/.damaged/{REL}", "source_broken": False})
    migrate._print_event({"type": "damaged_output", "file": XML_REL, "pack": "x", "approved": False,
                          "moved_to": None, "source_broken": True})
    out = capsys.readouterr().out
    assert f"Damaged output: {REL} (human-approved), written by java8-to-java21" in out
    assert "re-run java8-to-java21" in out
    assert "neither does the original" in out


@needs_javac
def test_the_real_ams_damage_is_found_in_one_javac_call(tmp_path):
    """The three files the AMS build failed on, as they were -- and nothing else."""
    root = tmp_path / "out"
    good = "package p;\npublic class Good { int x() { return 1; } }\n"
    files = {
        "p/OrderType.java": "package p;\npublic class OrderType {\n  void f() {\n    int a = 1;\n  }ßßß\n}\n",
        "p/OrderStatusType.java": "package p;\npublic class OrderStatusType {\n  void f() {}\n}\n}\n",
        "p/Good.java": good,
        "q/Good.java": good.replace("package p", "package q"),
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    calls = []

    def counting(*a, **kw):
        import subprocess
        calls.append(a[0])
        return subprocess.run(*a, **kw)

    verdict, found = syntax.check_tree(str(root), sorted(files), ForgeConfig(data={}), run=counting)
    assert verdict == syntax.FAIL
    assert sorted(found) == ["p/OrderStatusType.java", "p/OrderType.java"]
    assert len(calls) == 1, "one javac for the whole tree, not one per file"


def test_a_newer_language_feature_is_the_toolchain_not_damage(tmp_path):
    (tmp_path / "A.java").write_text("class A {}", encoding="utf-8")

    def fake(argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr=(
            "A.java:3: error: patterns in switch statements are a preview feature and are disabled by default.\n"
            "  (use --enable-preview to enable patterns in switch statements)\n1 error\n"))

    cfg = ForgeConfig(data={"project_build": {"java_home": str(tmp_path / "jdk")}})
    (tmp_path / "jdk/bin").mkdir(parents=True)
    (tmp_path / "jdk/bin/javac").write_text("", encoding="utf-8")
    verdict, found = syntax.check_tree(str(tmp_path), ["A.java"], cfg, run=fake)
    assert found == {} and verdict == syntax.PASS
