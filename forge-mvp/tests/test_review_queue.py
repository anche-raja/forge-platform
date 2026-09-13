"""The review queue and the static page a human decides from."""

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from forge.review_queue import (
    QUEUE_NAME, REVIEW_PAGE_NAME, build_queue, entry_to_file_status, load_queue, needs_review,
    render_review_page, write_queue, write_review_page,
)
from forge.state import make_file_status
from forge.verify.merged_tree import MergedTree

ORIGINAL = "package com.acme;\nimport javax.persistence.Entity;\npublic class A {}\n"
MIGRATED = "package com.acme;\nimport jakarta.persistence.Entity;\npublic class A {}\n"


def _status(path: Path, status="MANUAL_REVIEW", *, files=None, **fields):
    fs = make_file_status(str(path), "javax-to-jakarta")
    fs["status"] = status
    if files is not None:
        fs["transform_output"] = {"files": files, "deleted_files": [], "manual_flags": []}
    fs.update(fields)
    return fs


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "src/app/src/main/java/com/acme/A.java"
    p.parent.mkdir(parents=True)
    p.write_text(ORIGINAL, encoding="utf-8")
    return p


# ─── queue ────────────────────────────────────────────────────────────────────

def test_queue_entry_carries_original_transformed_risk_and_verdicts(tmp_path, src):
    fs = _status(src, files={str(src): MIGRATED}, risk_score=45, risk_tier="MEDIUM",
                 risk_reasons=["Spring-proxied class"], review_score=40, review_verdict="MANUAL",
                 review_feedback="routing lost", guardrail_findings=["PII in comment"], retry_count=2)
    queue = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"), phase="javax-to-jakarta")
    (e,) = queue["entries"]
    assert e["rel_path"] == "app/src/main/java/com/acme/A.java" and e["pack"] == "javax-to-jakarta"
    assert e["original"] == ORIGINAL and e["original_truncated"] is False
    assert e["transformed"] == {"app/src/main/java/com/acme/A.java": MIGRATED}
    assert (e["risk_score"], e["risk_tier"], e["risk_reasons"]) == (45, "MEDIUM", ["Spring-proxied class"])
    assert (e["review_score"], e["review_verdict"], e["review_feedback"]) == (40, "MANUAL", "routing lost")
    assert e["guardrail_findings"] == ["PII in comment"] and e["retry_count"] == 2
    assert queue["version"] == 2 and queue["dry_run"] is False


def test_only_review_statuses_are_queued_in_a_real_run(tmp_path, src):
    done = _status(src, "DONE", files={str(src): MIGRATED})
    held = _status(src, "HELD")
    blocked = _status(src, "BLOCKED")
    assert not needs_review(done, dry_run=False)
    assert needs_review(held, dry_run=False) and needs_review(blocked, dry_run=False)
    queue = build_queue([done, held, blocked], str(tmp_path / "src"), str(tmp_path / "out"))
    assert [e["status"] for e in queue["entries"]] == ["HELD", "BLOCKED"]


def test_dry_run_queues_every_unit_that_produced_a_transform(tmp_path, src):
    """A first trial exists to look at what the model would do."""
    done = _status(src, "DONE", files={str(src): MIGRATED})
    untouched = _status(src, "DONE")   # no transform_output at all
    queue = build_queue([done, untouched], str(tmp_path / "src"), str(tmp_path / "out"), dry_run=True)
    assert queue["dry_run"] is True
    assert len(queue["entries"]) == 1 and queue["entries"][0]["status"] == "DONE"


def test_original_is_capped_with_a_marker(tmp_path):
    big = tmp_path / "src/Big.java"
    big.parent.mkdir(parents=True)
    big.write_text("x" * 250_000, encoding="utf-8")
    queue = build_queue([_status(big)], str(tmp_path / "src"), str(tmp_path / "out"))
    e = queue["entries"][0]
    assert e["original_truncated"] is True and e["original"].endswith("[truncated: 250000 bytes total]")
    assert len(e["original"]) < 250_000


def test_generated_unit_has_no_original(tmp_path):
    target = tmp_path / "src/mod/src/main/liberty/config/server.xml"
    fs = _status(target, "HELD", files={str(target): "<server/>"}, generate=True)
    e = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"))["entries"][0]
    assert e["original"] is None and e["transformed"] == {"mod/src/main/liberty/config/server.xml": "<server/>"}


def test_transformed_is_read_back_from_staged_files_when_state_was_truncated(tmp_path, src):
    staged = tmp_path / "out/.forge-staging/app/src/main/java/com/acme/A.java"
    staged.parent.mkdir(parents=True)
    staged.write_text(MIGRATED, encoding="utf-8")
    fs = _status(src, "HELD", held_paths=[str(staged)])
    fs["transform_output"] = {"_truncated": True, "bytes": 999999}
    e = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"))["entries"][0]
    assert e["transformed"] == {"app/src/main/java/com/acme/A.java": MIGRATED}


def test_write_and_load_queue_round_trip_and_reject_old_format(tmp_path, src):
    out = tmp_path / "out"
    queue = write_queue(str(out), [_status(src, files={str(src): MIGRATED})], str(tmp_path / "src"), phase="p")
    assert (out / QUEUE_NAME).exists()
    assert load_queue(str(out))["entries"][0]["rel_path"] == queue["entries"][0]["rel_path"]
    (out / QUEUE_NAME).write_text(json.dumps([{"file_path": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="version-2"):
        load_queue(str(out))
    with pytest.raises(FileNotFoundError, match="dry-run"):
        load_queue(str(tmp_path / "nowhere"))


def test_entry_to_file_status_round_trips_for_reruns(tmp_path, src):
    fs = _status(src, "HELD", files={str(src): MIGRATED}, risk_tier="HIGH", hold_reason="risk", human_note="fix X")
    e = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"))["entries"][0]
    back = entry_to_file_status(e)
    assert back["file_path"] == str(src) and back["status"] == "HELD" and back["risk_tier"] == "HIGH"
    assert back["human_note"] == "fix X"
    assert back["transform_output"]["files"] == {"app/src/main/java/com/acme/A.java": MIGRATED}


# ─── page ─────────────────────────────────────────────────────────────────────

class _Counter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fieldsets = 0
        self.external = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "fieldset" and "decision" in (a.get("class") or ""):
            self.fieldsets += 1
        if tag in ("script", "link", "img") and (a.get("src") or a.get("href")):
            self.external.append((tag, a.get("src") or a.get("href")))


def _page(tmp_path, src, **fields):
    fs = _status(src, files={str(src): MIGRATED}, review_feedback="the import was wrong", **fields)
    queue = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"), phase="javax-to-jakarta", run_id="run-1")
    return render_review_page(queue), queue


def test_page_shows_both_texts_a_diff_and_the_feedback(tmp_path, src):
    page, _ = _page(tmp_path, src, risk_tier="MEDIUM", risk_reasons=["Spring-proxied class"])
    assert "javax.persistence.Entity" in page and "jakarta.persistence.Entity" in page
    assert "<span class=\"del\">-import javax.persistence.Entity;</span>" in page
    assert "<span class=\"add\">+import jakarta.persistence.Entity;</span>" in page
    assert "the import was wrong" in page and "Spring-proxied class" in page
    assert "risk MEDIUM" in page and "data-run=\"run-1\"" in page


def test_page_has_one_widget_per_entry_and_no_external_assets(tmp_path, src):
    fs1 = _status(src, files={str(src): MIGRATED})
    fs2 = _status(src, "BLOCKED")
    queue = build_queue([fs1, fs2], str(tmp_path / "src"), str(tmp_path / "out"))
    page = render_review_page(queue)
    c = _Counter()
    c.feed(page)
    assert c.fieldsets == 2
    assert c.external == []
    assert "http://" not in page and "https://" not in page
    assert page.startswith("<!DOCTYPE html>")


def test_page_escapes_script_in_source_and_widget_produces_decisions_format(tmp_path):
    evil = tmp_path / "src/Evil.java"
    evil.parent.mkdir(parents=True)
    evil.write_text("class Evil { String s = \"</script><script>alert(1)</script>\"; }\n", encoding="utf-8")
    fs = _status(evil, files={str(evil): "class Evil {}"}, human_note="prior note", human_decision="retry")
    page = render_review_page(build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out")))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;/script&gt;" in page
    assert "data-file=\"Evil.java\"" in page and "(previously: retry)" in page and "prior note" in page
    assert "decisions.json" in page and "--apply-decisions" in page


def test_dry_run_page_says_so(tmp_path, src):
    fs = _status(src, "DONE", files={str(src): MIGRATED})
    queue = build_queue([fs], str(tmp_path / "src"), str(tmp_path / "out"), dry_run=True)
    page = render_review_page(queue)
    assert "dry run" in page and "nothing was written" in page


def test_write_review_page_lands_next_to_the_queue(tmp_path, src):
    out = tmp_path / "out"
    queue = write_queue(str(out), [_status(src, files={str(src): MIGRATED})], str(tmp_path / "src"))
    assert write_review_page(str(out), queue) == out / REVIEW_PAGE_NAME
    assert (out / REVIEW_PAGE_NAME).read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


# ─── merged view ──────────────────────────────────────────────────────────────

def test_merged_view_ignores_staging_and_review_artifacts(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    (src / "A.java").parent.mkdir(parents=True)
    (src / "A.java").write_text("a", encoding="utf-8")
    for rel in (".forge-staging/A.java", "migration-review.html", "decisions.json", "decisions-2.json",
                "decisions-applied.jsonl", "pack-feedback.md", "manual-review-queue.json"):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    (out / "B.java").write_text("b", encoding="utf-8")
    assert sorted(MergedTree(str(src), str(out)).rel_paths()) == ["A.java", "B.java"]
