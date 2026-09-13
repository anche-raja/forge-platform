"""--feedback-report: reviewers' notes grouped so corrections become pack edits."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import migrate
from forge.feedback_report import (
    FEEDBACK_NAME, collect_notes, extract_rule, group_notes, normalise_note, pack_edit_path,
    render_feedback_report, write_feedback_report,
)


def _decisions(out: Path, name: str, *decisions):
    (out / name).write_text(json.dumps({"run": "r", "decisions": list(decisions)}), encoding="utf-8")


def _applied(out: Path, *rows):
    with (out / "decisions-applied.jsonl").open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_rule_is_taken_from_the_explicit_field_then_the_note_text():
    assert extract_rule("anything", "Rule 4") == "Rule 4"
    assert extract_rule("see rule #2 — the permitted paths changed") == "Rule 2"
    assert extract_rule("RULE 7 again", "") == "Rule 7"
    assert extract_rule("no rule mentioned") is None


def test_notes_without_a_rule_group_by_their_normalised_first_line():
    assert normalise_note("  Keep the  realm!  \nsecond line") == "keep the realm"
    assert normalise_note("Keep the realm.") == normalise_note("keep   the realm")
    assert normalise_note("") == "(no note)"


def test_groups_by_pack_then_rule_with_counts_files_and_decisions(tmp_path):
    _decisions(tmp_path, "decisions.json",
               {"file": "a/A.java", "pack": "struts2-modernize", "decision": "retry", "note": "Rule 2: annotate the setter"},
               {"file": "a/B.java", "pack": "struts2-modernize", "decision": "retry", "note": "rule 2 again — setter missed"},
               {"file": "a/C.java", "pack": "struts2-modernize", "decision": "reject", "note": "keep the realm"},
               {"file": "w/web.xml", "pack": "webapp-bootstrap-jakarta10", "decision": "approve", "note": ""})
    groups = group_notes(collect_notes(str(tmp_path)))
    assert list(groups) == ["struts2-modernize", "webapp-bootstrap-jakarta10"]
    rows = groups["struts2-modernize"]
    assert list(rows) == ["Rule 2", "keep the realm"], "rule-keyed rows first, then by count"
    assert rows["Rule 2"]["count"] == 2 and rows["Rule 2"]["files"] == {"a/A.java", "a/B.java"}
    assert rows["Rule 2"]["decisions"] == {"retry": 2}
    assert groups["webapp-bootstrap-jakarta10"]["(no note)"]["decisions"] == {"approve": 1}


def test_dedupes_across_decisions_files_and_the_applied_log(tmp_path):
    d = {"file": "a/A.java", "pack": "p", "decision": "retry", "note": "same note"}
    _decisions(tmp_path, "decisions.json", d)
    _decisions(tmp_path, "decisions-2.json", d)
    _applied(tmp_path, {**d, "at": "t", "run": "r", "applied": True, "status_after": "DONE"},
             {"file": "a/Z.java", "pack": "p", "decision": "reject", "note": "only in the log"})
    notes = collect_notes(str(tmp_path))
    assert len(notes) == 2
    assert {n["file"] for n in notes} == {"a/A.java", "a/Z.java"}


def test_pack_edit_path_names_the_pack_file_or_the_builtin_module():
    assert pack_edit_path("struts2-modernize").endswith("prompts/packs/struts2-modernize.pack.md")
    assert pack_edit_path("java21") == "forge/phases.py"
    assert pack_edit_path("no-such-pack") == "(unknown pack)"


def test_report_names_the_file_to_edit_and_lists_notes_as_written(tmp_path):
    _decisions(tmp_path, "decisions.json",
               {"file": "a/A.java", "pack": "struts2-modernize", "decision": "retry", "note": "Rule 2: annotate the setter"})
    path = write_feedback_report(str(tmp_path))
    assert path == tmp_path / FEEDBACK_NAME
    md = path.read_text(encoding="utf-8")
    assert "## struts2-modernize — 1 note(s)" in md
    assert "Edit: `" in md and "struts2-modernize.pack.md" in md
    assert "| Rule 2 | 1 | retry ×1 | `a/A.java` |" in md
    assert "- Rule 2: annotate the setter" in md


def test_empty_output_dir_writes_a_one_line_report(tmp_path):
    md = render_feedback_report({}, str(tmp_path))
    assert "No decision notes found" in md and "--apply-decisions" in md
    assert write_feedback_report(str(tmp_path)).exists()


def test_cli_feedback_report_needs_no_source_dir_or_phase(tmp_path, capsys):
    _decisions(tmp_path, "decisions.json",
               {"file": "a/A.java", "pack": "javax-to-jakarta", "decision": "retry", "note": "leave javax.sql alone"})
    with patch.object(sys, "argv", ["migrate.py", "--feedback-report", "--output-dir", str(tmp_path)]):
        assert migrate.main() == 0
    out = capsys.readouterr().out
    assert "1 decision note(s) across 1 pack(s): javax-to-jakarta" in out
    assert (tmp_path / FEEDBACK_NAME).exists()
