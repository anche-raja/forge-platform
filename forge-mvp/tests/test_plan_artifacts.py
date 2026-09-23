"""A plan's artifacts survive the plan (#23).

Each pack used to overwrite ``migration-report.md``, ``manual-review-queue.json``,
``migration-review.html`` and ``migration-acceptance.json``. After the first
ten-pack AMS run they described one pack with one file, the held files of the
other nine had dropped off the review page, and their chat cards had gone stale.

Now every pack keeps ``migration-report-<pack>.md`` (and an acceptance record
per pack), ``migration-summary.md`` has a row per pack, and the review queue
accumulates: entries from earlier packs stay until they are decided, keyed by
pack and path, and a newer run of the same pack replaces only its own.
"""

import json
from pathlib import Path

import pytest

from forge import service
from forge.review_queue import load_queue, merge_queue
from tests.conftest import llm_reply, mocked_aws, write_config

# Security configuration is HIGH risk by rule, so the default review-high ceiling holds it.
HELD_REL = "src/main/java/com/corp/WebSecurityConfig.java"
DONE_REL = "src/main/java/com/corp/Util.java"
HELD_SRC = ("package com.corp;\nimport javax.servlet.Filter;\n"
            "public class WebSecurityConfig extends WebSecurityConfigurerAdapter {\n"
            "  boolean f(Object r) { return r instanceof Filter; }\n}\n")
DONE_SRC = ("package com.corp;\nimport javax.persistence.Entity;\n"
            "public class Util { boolean f(Object r) { return r instanceof Entity; } }\n")


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "app"
    (src / "src/main/java/com/corp").mkdir(parents=True)
    (src / HELD_REL).write_text(HELD_SRC, encoding="utf-8")
    (src / DONE_REL).write_text(DONE_SRC, encoding="utf-8")
    (src / "pom.xml").write_text("<project><properties><maven.compiler.source>1.8</maven.compiler.source>"
                                 "</properties></project>", encoding="utf-8")
    return src, tmp_path / "migrated"


def _run(tmp_path, src, out, phase, marker, **kw):
    """One real run whose transform stamps ``marker`` into every file it writes."""
    cfg = write_config(tmp_path)
    with mocked_aws(review_score=95) as mocks:
        def transform(messages):
            path = messages[1].content.split("File path: ", 1)[1].splitlines()[0]
            body = Path(path).read_text(encoding="utf-8")
            return llm_reply({"files": {path: body.replace("javax.", "jakarta.") + f"// {marker}\n"},
                              "manual_flags": []})
        mocks["upgrade"].return_value.invoke.side_effect = transform
        return service.run_migration(str(src), phase, str(out), cfg, no_metrics=True, **kw)


def test_every_pack_keeps_its_report_and_the_summary_has_a_row_for_each(project, tmp_path):
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", "first")
    result = _run(tmp_path, src, out, "java8-to-java21", "second", chain=True)

    first = (out / "migration-report-javax-to-jakarta.md").read_text(encoding="utf-8")
    second = (out / "migration-report-java8-to-java21.md").read_text(encoding="utf-8")
    assert "**Phase:** javax-to-jakarta" in first and "**Phase:** java8-to-java21" in second
    latest = (out / "migration-report.md").read_text(encoding="utf-8")
    assert "**Phase:** java8-to-java21" in latest, "migration-report.md stays the latest run"

    summary = (out / "migration-summary.md").read_text(encoding="utf-8")
    rows = [line for line in summary.splitlines() if line.startswith("| ")]
    assert any(r.startswith("| javax-to-jakarta |") for r in rows), summary
    assert any(r.startswith("| java8-to-java21 |") for r in rows), summary
    assert "`migration-report-javax-to-jakarta.md`" in summary
    assert result.paths["summary"] == str(out / "migration-summary.md")
    assert result.paths["pack_report"] == str(out / "migration-report-java8-to-java21.md")


def test_held_files_from_an_earlier_pack_stay_on_the_review_page(project, tmp_path):
    """The first pack's held file must not drop off when the second pack runs."""
    src, out = project
    first = _run(tmp_path, src, out, "javax-to-jakarta", "first")
    first_run = first.queue["run"]
    assert [(e["pack"], e["rel_path"]) for e in first.queue["entries"]] == [("javax-to-jakarta", HELD_REL)]

    second = _run(tmp_path, src, out, "java8-to-java21", "second", chain=True)

    queue = load_queue(str(out))
    keys = [(e["pack"], e["rel_path"]) for e in queue["entries"]]
    assert ("javax-to-jakarta", HELD_REL) in keys, "the earlier pack's held file dropped off"
    assert ("java8-to-java21", HELD_REL) in keys, "the later pack holds the same file under its own key"
    earlier = next(e for e in queue["entries"] if e["pack"] == "javax-to-jakarta")
    assert earlier["run"] == first_run, "an untouched entry keeps the stamp its card was shown with"
    assert queue["run"] == second.queue["run"]
    page = (out / "migration-review.html").read_text(encoding="utf-8")
    assert "javax-to-jakarta" in page and HELD_REL in page

    summary = (out / "migration-summary.md").read_text(encoding="utf-8")
    row = next(line for line in summary.splitlines() if line.startswith("| javax-to-jakarta |"))
    assert "| 1 |" in row, f"the summary shows the earlier pack's file still awaiting review: {row}"


def test_a_rerun_of_the_same_pack_replaces_only_its_own_entries(project, tmp_path):
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", "first")
    _run(tmp_path, src, out, "java8-to-java21", "second", chain=True)
    before = {e["pack"]: e for e in load_queue(str(out))["entries"]}

    _run(tmp_path, src, out, "java8-to-java21", "second-again", chain=True)

    after = load_queue(str(out))["entries"]
    assert sorted(e["pack"] for e in after) == sorted(before), "one entry per (pack, file), never two"
    kept = next(e for e in after if e["pack"] == "javax-to-jakarta")
    assert kept == before["javax-to-jakarta"], "another pack's entry is carried verbatim"


def test_a_decision_for_an_earlier_packs_entry_still_applies(project, tmp_path):
    """And the queue after it keeps the later pack's entry exactly as it was."""
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", "first")
    _run(tmp_path, src, out, "java8-to-java21", "second", chain=True)
    later = next(e for e in load_queue(str(out))["entries"] if e["pack"] == "java8-to-java21")

    with mocked_aws():
        result = service.apply([{"file": HELD_REL, "pack": "javax-to-jakarta", "decision": "approve"}],
                               str(src), str(out), write_config(tmp_path))

    assert [o.applied for o in result.outcomes] == [True]
    text = (out / HELD_REL).read_text(encoding="utf-8")
    assert "// first" in text, "the approval shipped the earlier pack's own transform, not the later one's"
    remaining = load_queue(str(out))["entries"]
    assert remaining == [later], "the undecided entry was rebuilt instead of kept"
    applied = (out / "migration-report-javax-to-jakarta.md").read_text(encoding="utf-8")
    assert "## Applied decisions" in applied


def test_a_dry_run_never_pushes_a_real_held_file_off_the_page():
    real = {"version": 2, "run": "r1", "phase": "p", "dry_run": False,
            "entries": [{"pack": "p", "rel_path": "A.java", "status": "HELD", "held_paths": ["/s/A.java"]}]}
    dry = {"version": 2, "run": "r2", "phase": "p", "dry_run": True,
           "entries": [{"pack": "p", "rel_path": "A.java", "status": "DONE", "run": "r2", "dry_run": True},
                       {"pack": "p", "rel_path": "B.java", "status": "DONE", "run": "r2", "dry_run": True}]}
    merged = merge_queue(real, dry)
    assert [(e["rel_path"], e["run"], e["dry_run"]) for e in merged["entries"]] == [
        ("A.java", "r1", False), ("B.java", "r2", True)]

    # A real run of the pack replaces everything it had, dry or not.
    again = {"version": 2, "run": "r3", "phase": "p", "dry_run": False, "entries": []}
    assert merge_queue(merged, again)["entries"] == []


def test_an_earlier_entry_loses_a_staged_path_a_later_pack_overwrote():
    """Staging mirrors the source layout; two packs holding one file share one staged path."""
    earlier = {"version": 2, "run": "r1", "phase": "a", "dry_run": False,
               "entries": [{"pack": "a", "rel_path": "A.java", "held_paths": ["/stage/A.java"],
                            "transformed": {"A.java": "a's version"}}]}
    later = {"version": 2, "run": "r2", "phase": "b", "dry_run": False,
             "entries": [{"pack": "b", "rel_path": "A.java", "held_paths": ["/stage/A.java"], "run": "r2"}]}
    merged = merge_queue(earlier, later)
    a = next(e for e in merged["entries"] if e["pack"] == "a")
    assert a["held_paths"] == [] and a["transformed"] == {"A.java": "a's version"}


def test_each_pack_keeps_its_acceptance_record(project, tmp_path):
    src, out = project
    _run(tmp_path, src, out, "javax-to-jakarta", "first", run_acceptance=True)
    _run(tmp_path, src, out, "java8-to-java21", "second", chain=True, run_acceptance=True)
    for pack in ("javax-to-jakarta", "java8-to-java21"):
        record = out / f"migration-acceptance-{pack}.json"
        if record.is_file():
            assert json.loads(record.read_text(encoding="utf-8"))
    assert (out / "migration-acceptance-javax-to-jakarta.json").is_file(), \
        "the first pack's acceptance record was overwritten by the second's"


def test_per_pack_reports_and_the_summary_never_land_or_enter_the_merged_view(tmp_path):
    from forge.leader.landing import is_artifact
    from forge.verify.merged_tree import MergedTree

    for name in ("migration-report-javax-to-jakarta.md", "migration-acceptance-java21.json",
                 "migration-summary.md", "migration-summary.json"):
        assert is_artifact(name), name
        assert MergedTree._is_artifact(name), name
    assert not is_artifact("docs/migration-report-notes.md"), "a project file deeper down is the user's"


def test_a_card_from_an_earlier_pack_is_still_current_after_the_next_pack_runs(project, tmp_path):
    """The chat side of the same fix: the stale-card check is per entry now."""
    from forge.leader.convo import Conversation
    from forge.leader.settings import LeaderSettings
    from forge.leader.tools import ProjectContext, Toolbox
    from unittest.mock import patch

    src, out = project
    first = _run(tmp_path, src, out, "javax-to-jakarta", "first")
    _run(tmp_path, src, out, "java8-to-java21", "second", chain=True)
    cfg = write_config(tmp_path)
    ctx = ProjectContext(source_dir=str(src), output_dir=str(out), config=cfg, base_config=cfg)
    convo = Conversation()
    convo.selected_packs = ["javax-to-jakarta", "java8-to-java21"]
    box = Toolbox(ctx, convo, LeaderSettings.from_config(cfg), lambda event: None, None)

    card_run = first.queue["run"]
    ok = {"run": card_run, "decisions": [{"file": HELD_REL, "pack": "javax-to-jakarta", "decision": "approve"}]}
    result = service.ApplyResult(outcomes=[], remaining=[], queue_after=None, log_path=None, all_applied=True)
    with patch("forge.service.apply", return_value=result) as apply:
        outcome = box.execute("apply_review_decisions", ok, tool_id="t1", confirmed=True)
    assert apply.call_count == 1, outcome.observation

    # The later pack's entry was produced by a different run: that card stamp does not cover it.
    stale = {"run": card_run, "decisions": [{"file": HELD_REL, "pack": "java8-to-java21", "decision": "approve"}]}
    with patch("forge.service.apply") as apply:
        outcome = box.execute("apply_review_decisions", stale, tool_id="t2", confirmed=True)
        apply.assert_not_called()
    assert "changed since" in outcome.observation["rejected"][0]["reason"]


def test_the_report_counts_unchanged_units(tmp_path):
    from forge.utils.report import generate_report

    statuses = [{"file_path": "A.java", "status": "DONE", "unchanged": True},
                {"file_path": "B.java", "status": "DONE"}]
    out = tmp_path / "r.md"
    generate_report(output_path=str(out), phase="p", source_dir=str(tmp_path), file_statuses=statuses,
                    bedrock_calls=1)
    assert "nothing written):** 1" in out.read_text(encoding="utf-8")


def test_the_leader_is_told_never_to_finish_a_failed_landing_by_hand():
    from forge.leader.agent import _SYSTEM
    assert "git add ." in _SYSTEM and "Never" in _SYSTEM
