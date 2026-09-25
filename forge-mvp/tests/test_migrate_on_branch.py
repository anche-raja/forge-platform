"""``leader.migrate_on_branch`` — the branch first, one commit per pack, the PR at the end.

The owner asked for the migration to happen in the repository itself: a
``mig-<timestamp>`` branch before the first pack, every pack's files replacing
the originals in place and committed, the project built as it stands on that
branch, and the pull request opened at the end — as a draft when the build did
not pass. These tests drive the toolbox over a real throwaway repository with
the paid service patched out, and hold the rules ``land`` already holds: FORGE
refuses rather than tidies, never commits on a branch it did not make, never
commits its own artifacts or a held file, and never overwrites an uncommitted
edit of the user's.
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from forge import service
from forge.leader.convo import Conversation
from forge.leader.landing import SYNC_NAME, sync_to_branch
from forge.leader.settings import LeaderSettings
from forge.leader.tools import ProjectContext, Toolbox
from forge.utils import run_manifest
from forge.utils.file_writer import STAGING_DIR
from tests.test_landing import ARTIFACT_MARKER, HELD_MARKER, _branches, _git, _head, config, repo  # noqa: F401
from tests.test_leader_tools import _seed

FIRST = "src/main/java/com/corp/user/UserAction.java"
SECOND = "src/main/java/com/corp/Other.java"
PACKS = ["javax-to-jakarta", "java21"]


def _box(repo, config, convo, **settings):
    ctx = ProjectContext(source_dir=str(repo), output_dir=str(repo / ".migrated"),
                         config=config, base_config=config)
    resolved = replace(LeaderSettings.from_config(config), confirm_above_usd=0.0,
                       migrate_on_branch=True, branch_prefix="mig", **settings)
    return Toolbox(ctx, convo, resolved, lambda event: None, None), ctx


def _pack_writes(files):
    """A fake ``run_migration``: each pack writes its file, an artifact and a held unit."""
    calls = []

    def run(source_dir, pack, output_dir, config, **kw):
        calls.append({"pack": pack, **kw})
        out = Path(output_dir)
        target = out / files[pack][0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[pack][1], encoding="utf-8")
        run_manifest.record(str(out), pack, [str(target)])
        (out / "migration-report.md").write_text(f"{ARTIFACT_MARKER}\n", encoding="utf-8")
        held = out / STAGING_DIR / "src/main/java/com/corp/Held.java"
        held.parent.mkdir(parents=True, exist_ok=True)
        held.write_text(f"// {HELD_MARKER}\n", encoding="utf-8")
        totals = {"total": 1, "passed": 1, "manual": 0, "blocked": 0, "held": 0,
                  "bedrock_calls": 3, "cost_usd": 0.07}
        return service.RunResult(phase=pack, source_dir=source_dir, output_dir=output_dir, dry_run=False,
                                 statuses=[], totals=totals, skipped=[], queue={"entries": []}, paths={},
                                 acceptance=None, cancelled=False)

    return calls, run


def _build(outcome):
    return {"outcome": outcome, "detail": "", "failed_step": None, "tail": [], "java_home": None,
            "seconds": 1.0, "steps": [], "built_at": "2026-09-25T10:00:00+00:00"}


def _log(repo):
    return _git(repo, "log", "--format=%s", "main..HEAD").splitlines()


def _plan(repo, config, *, build="pass", files=None, **settings):
    files = files or {"javax-to-jakarta": (FIRST, "package com.corp.user;\n// jakarta\n"),
                      "java21": (SECOND, "package com.corp;\n// java 21\n")}
    convo = _seed(Conversation(), PACKS)
    box, ctx = _box(repo, config, convo, **settings)
    calls, run = _pack_writes(files)
    with patch("forge.service.run_migration", side_effect=run), \
         patch("forge.service.build_project", return_value=_build(build)) as built:
        first = box.execute("run_pack", {"pack": PACKS[0]}, tool_id="t1")
        last = box.execute("run_pack", {"pack": PACKS[1]}, tool_id="t2")
    return box, convo, calls, built, first, last


def test_the_branch_comes_first_and_every_pack_is_its_own_commit(repo, config):
    box, convo, calls, built, first, last = _plan(repo, config)

    assert first.ok and last.ok
    branch = convo.work_branch
    assert branch.startswith("mig-") and _head(repo) == branch
    assert _branches(repo) == sorted(["main", branch])
    assert _log(repo) == ["Migrate with FORGE pack java21", "Migrate with FORGE pack javax-to-jakarta"]
    # The files replaced the originals in place ...
    assert (repo / FIRST).read_text(encoding="utf-8").endswith("// jakarta\n")
    assert (repo / SECOND).read_text(encoding="utf-8").endswith("// java 21\n")
    # ... and nothing but them: no artifact, no held unit, not the output folder.
    committed = _git(repo, "diff", "--name-only", "main..HEAD").split()
    assert sorted(committed) == sorted([FIRST, SECOND])
    assert _git(repo, "status", "--porcelain") == "", "the output folder is excluded, not left dirty"
    # Each pack read the repository in place, and the build is of the repository.
    assert all(c["in_place"] is True and c["chain"] is False for c in calls)
    assert built.call_count == 1 and built.call_args.kwargs["overlay"] is False
    assert first.observation["commit"]["files_changed"] == 1
    assert convo.landings[branch]["base_branch"] == "main"


def test_a_dirty_work_tree_stops_the_first_pack_before_anything_is_spent(repo, config):
    (repo / "README.md").write_text("half an edit\n", encoding="utf-8")
    convo = _seed(Conversation(), PACKS)
    box, _ = _box(repo, config, convo)
    with patch("forge.service.run_migration") as run:
        outcome = box.execute("run_pack", {"pack": PACKS[0]}, tool_id="t1")
    run.assert_not_called()
    assert outcome.ok is False and "uncommitted" in outcome.observation["error"]
    assert _branches(repo) == ["main"] and convo.work_branch == ""


def test_an_edit_the_user_committed_on_the_branch_survives_the_next_pack(repo, config):
    convo = _seed(Conversation(), PACKS)
    box, ctx = _box(repo, config, convo)
    calls, run = _pack_writes({"javax-to-jakarta": (FIRST, "// forge\n"), "java21": (SECOND, "// forge 2\n")})
    with patch("forge.service.run_migration", side_effect=run), \
         patch("forge.service.build_project", return_value=_build("pass")):
        box.execute("run_pack", {"pack": PACKS[0]}, tool_id="t1")
        (repo / FIRST).write_text("// the user's fix\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "fix by hand")
        box.execute("run_pack", {"pack": PACKS[1]}, tool_id="t2")
    assert (repo / FIRST).read_text(encoding="utf-8") == "// the user's fix\n"
    assert _log(repo)[0] == "Migrate with FORGE pack java21"


def test_an_uncommitted_edit_to_a_file_forge_changes_is_refused_never_overwritten(repo, config):
    box, convo, *_ = _plan(repo, config)
    out = repo / ".migrated"
    (repo / FIRST).write_text("// the user's unfinished edit\n", encoding="utf-8")
    (out / FIRST).write_text("// forge rewrote it\n", encoding="utf-8")

    result = sync_to_branch(str(repo), str(out), convo.work_branch, "again")

    assert result["ok"] is False and FIRST in result["error"]
    assert (repo / FIRST).read_text(encoding="utf-8") == "// the user's unfinished edit\n"


def test_nothing_is_committed_on_a_branch_forge_did_not_make(repo, config):
    box, convo, *_ = _plan(repo, config)
    _git(repo, "switch", "-q", "main")
    (repo / ".migrated" / FIRST).write_text("// newer\n", encoding="utf-8")

    result = sync_to_branch(str(repo), str(repo / ".migrated"), convo.work_branch, "again")

    assert result["ok"] is False and "not on the migration branch" in result["error"]
    assert _git(repo, "log", "-1", "--format=%s", "main") == "before FORGE"


def test_a_sync_with_nothing_new_makes_no_commit(repo, config):
    box, convo, *_ = _plan(repo, config)
    before = _log(repo)
    result = sync_to_branch(str(repo), str(repo / ".migrated"), convo.work_branch, "again")
    assert result["ok"] is True and result["commit"] == "" and _log(repo) == before
    assert (repo / ".migrated" / SYNC_NAME).is_file() and not (repo / SYNC_NAME).exists()


def test_a_failed_build_still_opens_the_pull_request_as_a_draft(repo, config):
    opened = []

    def fake_open(source_dir, branch, base, **kw):
        opened.append({"branch": branch, "base": base, **kw})
        return {"ok": True, "url": "https://github.com/o/r/pull/9", "branch": branch, "base": base,
                "existing": False, "draft": kw.get("draft")}

    with patch("forge.leader.pull_request.open_pull_request", side_effect=fake_open), \
         patch("forge.service.build_status", return_value={"outcome": "fail", "stale": False}):
        box, convo, _, _, _, last = _plan(repo, config, build="fail", auto_publish=True)

    assert len(opened) == 1 and opened[0]["draft"] is True and opened[0]["branch"] == convo.work_branch
    assert opened[0]["base"] == "main"
    assert "Opened as a draft" in opened[0]["body"]
    assert last.observation["publish"]["status"] == "done"
    assert last.observation["publish"]["pull_request"]["draft"] is True


def test_a_passing_build_opens_a_ready_pull_request(repo, config):
    opened = []

    def fake_open(source_dir, branch, base, **kw):
        opened.append(kw)
        return {"ok": True, "url": "https://github.com/o/r/pull/9", "branch": branch, "base": base,
                "existing": False, "draft": kw.get("draft")}

    with patch("forge.leader.pull_request.open_pull_request", side_effect=fake_open), \
         patch("forge.service.build_status", return_value={"outcome": "pass", "stale": False}):
        _plan(repo, config, build="pass", auto_publish=True)
    assert len(opened) == 1 and opened[0]["draft"] is False
    assert "Opened as a draft" not in opened[0]["body"]


def test_migrate_on_branch_reads_only_a_real_yes():
    assert LeaderSettings.from_config({"leader": {"migrate_on_branch": True}}).migrate_on_branch is True
    assert LeaderSettings.from_config({"leader": {"migrate_on_branch": "false"}}).migrate_on_branch is False
    assert LeaderSettings.from_config({}).migrate_on_branch is False
