"""``land_on_branch`` — the one thing in FORGE that writes to the user's repo.

Every other tool writes into ``output_dir``, a directory FORGE owns and may
rewrite on the next run. This one checks out a branch in the engineer's own
repository and commits into it, so these tests are the warranty rather than
coverage: what it refuses to touch, and what it never puts in a commit.

The artifact list is the sharp end. ``manual-review-queue.json`` embeds the
source of every held file verbatim (``review_queue.py`` stores ``original`` and
``transformed`` so the review page can diff them), and ``migration-review.html``
renders the same bytes. A landing that swept the output directory with ``git add
-A`` would commit the customer's source into the branch the engineer pushes
next, and nothing downstream would flag it — it is a file FORGE itself wrote.
That is why the exclusion list is a constant with a cross-check test, and not a
``.gitignore`` suggestion.

No AWS and no network anywhere: a throwaway repository under ``tmp_path`` is a
real git repository, and landing is plain ``subprocess`` over it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

from forge.leader.convo import Conversation
from forge.leader.landing import ARTIFACT_NAMES
from forge.leader.settings import LeaderSettings
from forge.leader.tools import ProjectContext, Toolbox
from forge.utils import run_manifest
from forge.utils.file_writer import STAGING_DIR
from tests.conftest import write_config

BRANCH = "forge/jakarta"
MIGRATED_REL = "src/main/java/com/corp/user/UserAction.java"
# Planted in the migrated file. It belongs in the commit and in the work tree,
# and in no observation: the leader never sees source text (R3), and a landing
# observation is a ToolMessage like any other.
SOURCE_MARKER = "S0URCE_MARKER_land_7f31"
MIGRATED = (
    "package com.corp.user;\n"
    "import jakarta.persistence.Entity;\n"
    f"// {SOURCE_MARKER}\n"
    "public class UserAction {}\n"
)
# Planted in every artifact FORGE writes. This one may reach neither the commit
# nor the work tree — finding it under the repository at all means an artifact
# was copied, whether or not git happened to stage it.
ARTIFACT_MARKER = "ARTIF4CT_MARKER_land_7f31"
HELD_MARKER = "HELD_MARKER_land_7f31"


# ─── a real, throwaway repository ────────────────────────────────────────────

def _git(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"the test's own `git {' '.join(args)}` failed: {done.stderr.strip()}"
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A one-commit repository, configured so a commit works anywhere.

    ``user.email``/``user.name`` are set locally because a CI container has
    neither and ``git commit`` then fails with "Please tell me who you are" —
    which would look exactly like landing refusing. ``commit.gpgsign`` and
    ``core.hooksPath`` are pinned for the same reason: an engineer's global
    config would otherwise decide whether this suite passes on their machine.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(root, "config", "core.hooksPath", str(hooks))
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "user.email", "tests@forge.invalid")
    _git(root, "config", "user.name", "FORGE tests")
    (root / "README.md").write_text("the repository as the engineer left it\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "before FORGE")
    return root


@pytest.fixture
def config(tmp_path):
    return write_config(tmp_path)


def _output(tmp_path, *, migrated: bool = True, at: Path = None) -> Path:
    """An output directory shaped like one a finished run leaves behind.

    One migrated file, one of every artifact, and a held unit under
    ``.forge-staging/`` — the three kinds of thing that share the directory, of
    which exactly one may be committed.
    """
    out = at if at is not None else tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    if migrated:
        target = out / MIGRATED_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(MIGRATED, encoding="utf-8")
        # What a run leaves behind: the file, and the manifest entry saying who wrote it.
        run_manifest.record(str(out), "javax-to-jakarta", [str(target)])
    for name in ARTIFACT_NAMES:
        (out / name).write_text(f"{ARTIFACT_MARKER} in {name}\n", encoding="utf-8")
    held = out / STAGING_DIR / "src/main/java/com/corp/Held.java"
    held.parent.mkdir(parents=True, exist_ok=True)
    held.write_text(f"package com.corp;\n// {HELD_MARKER}\npublic class Held {{}}\n", encoding="utf-8")
    return out


def _land(repo, out, config, *, branch=BRANCH, message=None,
          completed=("javax-to-jakarta",), confirmed=True):
    convo = Conversation()
    convo.completed = list(completed)
    ctx = ProjectContext(source_dir=str(repo), output_dir=str(out), config=config, base_config=config)
    box = Toolbox(ctx, convo, LeaderSettings.from_config(config), lambda event: None, None)
    args = {"branch": branch}
    if message is not None:
        args["message"] = message
    return box.execute("land_on_branch", args, tool_id="t1", confirmed=confirmed), convo


# ─── reading the repository back ─────────────────────────────────────────────

def _branches(repo):
    return sorted(_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def _head(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _commit_files(repo):
    return sorted(_git(repo, "show", "--name-only", "--pretty=format:", "HEAD").split())


def _work_tree(repo):
    """Every file under the repository, ``.git`` aside — copied or not."""
    return sorted(
        str(p.relative_to(repo)).replace("\\", "/")
        for p in Path(repo).rglob("*")
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    )


# ─── the preconditions: refuse, never repair ─────────────────────────────────

def test_landing_always_needs_a_click_however_small_the_change_is(repo, tmp_path, config):
    """Like applying a review decision, this is the human's signature.

    An estimate cannot stand in for it: the cost of a bad landing is not model
    spend, it is a commit in someone else's repository.
    """
    out = _output(tmp_path)
    outcome, convo = _land(repo, out, config, confirmed=False)

    assert outcome.ok is True and outcome.needs_confirmation is True
    assert outcome.pending_id and outcome.pending_id in convo.pending
    assert outcome.cards and outcome.cards[0]["kind"] == "confirm"
    assert _branches(repo) == ["main"], "nothing may be checked out before the click"
    assert _work_tree(repo) == ["README.md"], "and nothing may be copied either"


def test_a_directory_that_is_not_a_git_repository_is_refused_with_git_init_as_advice(tmp_path, config):
    """Running ``git init`` for the user would make FORGE the author of their
    repository's history. The message says what to run; the user runs it."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    out = _output(tmp_path)

    outcome, _ = _land(plain, out, config)

    assert outcome.ok is False
    error = outcome.observation["error"]
    assert "git init" in error, f"the refusal has to say what would fix it: {error}"
    assert not (plain / ".git").exists(), "suggesting `git init` is not running it"


def test_a_dirty_work_tree_is_refused_and_nothing_is_ever_stashed(repo, tmp_path, config):
    """A stash is a place uncommitted work goes to be forgotten, and ``-f`` on
    a checkout is a place it goes to be destroyed. The engineer's half-finished
    edit is theirs; landing waits."""
    (repo / "README.md").write_text("half an edit the engineer has not finished\n", encoding="utf-8")
    out = _output(tmp_path)

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert re.search(r"clean|uncommitted|dirty", outcome.observation["error"], re.I), \
        outcome.observation["error"]
    assert _git(repo, "stash", "list") == "", "the edit was stashed instead of being left alone"
    assert "half an edit" in (repo / "README.md").read_text(encoding="utf-8")
    assert _branches(repo) == ["main"] and _head(repo) == "main"


def test_a_branch_that_already_exists_is_refused_rather_than_committed_onto(repo, tmp_path, config):
    """``git checkout -b`` on an existing branch fails; ``git checkout`` would
    succeed and put the migration on top of whatever was already there."""
    _git(repo, "branch", BRANCH)
    before = _git(repo, "rev-parse", BRANCH)
    out = _output(tmp_path)

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert BRANCH in outcome.observation["error"]
    assert _git(repo, "rev-parse", BRANCH) == before, "the existing branch moved"
    assert _head(repo) == "main"


def test_a_branch_name_git_would_reject_never_reaches_a_checkout(repo, tmp_path, config):
    """``~`` is one of the characters ``git check-ref-format`` forbids. Checked
    up front, this is a sentence; left to ``checkout -b``, it is a subprocess
    error with the repository in an unknown state."""
    out = _output(tmp_path)

    outcome, _ = _land(repo, out, config, branch="jakarta~1")

    assert outcome.ok is False
    assert "jakarta~1" in outcome.observation["error"], "the refusal echoes the name it rejected"
    assert _branches(repo) == ["main"] and _head(repo) == "main"


def test_an_output_directory_with_nothing_to_land_is_refused_before_the_branch_exists(repo, tmp_path, config):
    """Artifacts and held units are not a migration. Landing them would produce
    a branch whose whole content is FORGE's own paperwork."""
    missing, _ = _land(repo, tmp_path / "never-ran", config)
    assert missing.ok is False
    assert _branches(repo) == ["main"]

    artifacts_only, _ = _land(repo, _output(tmp_path, migrated=False), config)
    assert artifacts_only.ok is False
    assert _branches(repo) == ["main"], "a branch was created for a commit that never came"
    assert _work_tree(repo) == ["README.md"]


# ─── the happy path: what lands, and what never does ─────────────────────────

@pytest.fixture
def landed(repo, tmp_path, config):
    out = _output(tmp_path)
    outcome, convo = _land(repo, out, config)
    assert outcome.ok is True, outcome.observation
    return outcome, repo, out


def test_the_commit_carries_the_migrated_file_and_not_one_forge_artifact(landed):
    """The test this module exists for.

    Every artifact is planted with the same marker, so the assertion is not "the
    eleven names we thought of are absent" but "nothing FORGE wrote about the
    migration is anywhere under the repository". ``.forge-staging/`` is held
    work: a human has not approved it, and a commit is approval.
    """
    outcome, repo, _ = landed

    assert _head(repo) == BRANCH
    assert _commit_files(repo) == [MIGRATED_REL]
    assert (repo / MIGRATED_REL).read_text(encoding="utf-8") == MIGRATED

    tree = _work_tree(repo)
    assert tree == sorted(["README.md", MIGRATED_REL]), \
        f"something other than the migration was copied into the repository: {tree}"
    for name in ARTIFACT_NAMES:
        assert name not in tree, f"{name} was copied into the user's repository"
    assert not any(part == STAGING_DIR for p in tree for part in p.split("/")), \
        "held units the human never approved were copied in"

    planted = "\n".join(Path(repo, p).read_text(encoding="utf-8", errors="replace") for p in tree)
    assert ARTIFACT_MARKER not in planted and HELD_MARKER not in planted
    assert SOURCE_MARKER in planted, "the one thing that should have landed did not"

    assert _git(repo, "status", "--porcelain") == "", \
        "landing left the work tree dirty — a file was copied and not committed"
    assert _git(repo, "rev-list", "--count", "main") == "1", "the branch the engineer was on moved"


def test_the_landing_observation_is_counts_and_a_command_never_file_text(landed):
    outcome, repo, _ = landed
    obs = outcome.observation

    assert obs["branch"] == BRANCH
    assert obs["files_changed"] == 1
    # A count of 0 and an empty list both say "nothing was removed"; the spec
    # does not pin which, and this test is about the migration, not the shape.
    assert not obs["deleted"]
    assert obs["packs"] == ["javax-to-jakarta"], "the packs come from what the conversation completed"
    assert _git(repo, "rev-parse", "HEAD").startswith(obs["commit"])
    assert len(str(obs["commit"])) >= 7

    text = json.dumps(obs)
    assert SOURCE_MARKER not in text, "R3: an observation is a ToolMessage, and the leader never sees source"
    assert ARTIFACT_MARKER not in text


def test_landing_never_pushes_and_the_push_command_is_on_the_card_only(landed):
    """Pushing is the one step that leaves the machine, so it is its own click
    (open_pull_request), never part of a landing.

    The repository has no remote at all, which is also the proof: a landing that
    tried to push would have failed loudly rather than returned ``ok``. The
    command is on the card for the human; the observation carries none, so the
    leader has nothing to hand out as "push it by hand".
    """
    outcome, repo, _ = landed

    assert outcome.observation["pushed"] is False
    assert "push_command" not in outcome.observation
    assert "git push" not in json.dumps(outcome.observation)
    assert _git(repo, "remote") == "", "a remote was configured behind the user's back"
    assert _git(repo, "for-each-ref", "refs/remotes") == "", "something was pushed"

    card = outcome.cards[0]
    assert card["kind"] == "land"
    assert card["branch"] == BRANCH
    assert f"git push -u origin {BRANCH}" in json.dumps(card), \
        "the card is where the user reads the command they still have to run"


def test_a_generated_commit_message_names_the_packs_and_a_given_one_is_used_verbatim(repo, tmp_path, config):
    generated, _ = _land(repo, _output(tmp_path), config)
    assert generated.ok is True, generated.observation
    body = _git(repo, "log", "-1", "--pretty=%B")
    subject = body.splitlines()[0]
    assert "1 file" in subject and "javax-to-jakarta" in subject, subject
    assert "Co-Authored-By:" in body, "the repo's own convention for work a model did"

    _git(repo, "checkout", "-q", "main")
    mine, _ = _land(repo, _output(tmp_path), config, branch="forge/second",
                    message="Land the jakarta migration")
    assert mine.ok is True, mine.observation
    assert _git(repo, "log", "-1", "--pretty=%s") == "Land the jakarta migration"


# ─── only what a run recorded lands ──────────────────────────────────────────

STRAY_REL = "src/main/java/com/corp/Other.java"


def test_a_file_no_run_recorded_is_left_behind_and_named(repo, tmp_path, config):
    """Issue #25: a pytest fixture sat in the real output folder for two days.

    ``com/corp/Other.java`` was never written by a FORGE run, so it is not in
    ``.forge-writes.json`` and must not be committed into the user's repository
    -- and the user is told it is there, rather than it vanishing quietly.
    """
    out = _output(tmp_path)
    stray = out / STRAY_REL
    stray.write_text("package com.corp; class Other {}\n", encoding="utf-8")
    (out / ".DS_Store").write_bytes(b"\0")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert _commit_files(repo) == [MIGRATED_REL]
    assert STRAY_REL not in _work_tree(repo), "an unrecorded file was copied into the repository"
    assert outcome.observation["skipped_unrecorded"] == [STRAY_REL]
    assert outcome.observation["skipped_count"] == 1, ".DS_Store is noise, not a finding"
    assert stray.is_file(), "landing reports the stray file; deleting it is the user's call"


def test_an_approved_file_lands_even_when_the_manifest_never_heard_of_it(repo, tmp_path, config):
    """Approvals were not recorded in the manifest before this fix.

    The seven pom.xml files approved on AMS are in ``decisions-applied.jsonl``
    and not in ``.forge-writes.json``; a human signed them off, so they land.
    """
    out = _output(tmp_path)
    approved = "ams/pom.xml"
    (out / "ams").mkdir()
    (out / approved).write_text("<project/>\n", encoding="utf-8")
    # The real log begins with a fixture glued to the first approval, no newline between.
    (out / "decisions-applied.jsonl").write_text(
        json.dumps({"original": "class X {}"})
        + json.dumps({"run": "r", "file": approved, "pack": "build-maven-modernize", "decision": "approve",
                    "applied": True, "status_after": "DONE"}) + "\n"
        + json.dumps({"run": "r", "file": STRAY_REL, "pack": "x", "decision": "reject",
                      "applied": True, "status_after": "REJECTED"}) + "\n", encoding="utf-8")
    (out / STRAY_REL).write_text("class Other {}\n", encoding="utf-8")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert _commit_files(repo) == sorted([MIGRATED_REL, approved])
    assert outcome.observation["skipped_unrecorded"] == [STRAY_REL], "a rejection is not an approval"


def test_os_junk_and_forge_artifacts_never_land_even_when_the_manifest_names_them(repo, tmp_path, config):
    """The AMS landing staged ``.forge-writes.json`` and copied ``project-build.json``.

    Provenance is not enough on its own: a manifest that somehow listed an
    artifact, or a ``.DS_Store`` the Finder dropped into a migrated package,
    must still stay out of the user's repository.
    """
    out = _output(tmp_path)
    (out / "project-build.json").write_text(f'{{"outcome": "{ARTIFACT_MARKER}"}}', encoding="utf-8")
    (out / "src/main/java/com/corp/user/.DS_Store").write_bytes(b"\0\0")
    run_manifest.record(str(out), "javax-to-jakarta",
                        [str(out / "project-build.json"), str(out / "src/main/java/com/corp/user/.DS_Store")])

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert _commit_files(repo) == [MIGRATED_REL]
    tree = _work_tree(repo)
    assert not any(p.endswith(".DS_Store") for p in tree), tree
    assert run_manifest.MANIFEST_NAME not in tree and "project-build.json" not in tree, tree


def test_a_gitignored_file_is_left_out_with_a_note_instead_of_failing_git_add(repo, tmp_path, config):
    """``git add`` refuses an ignored path, and on AMS it refused after the checkout.

    ``check-ignore`` runs before the branch exists; an ignored file is dropped
    and named, and everything else lands whole.
    """
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore logs")
    out = _output(tmp_path)
    log = out / "src/main/java/com/corp/user/debug.log"
    log.write_text("noise\n", encoding="utf-8")
    run_manifest.record(str(out), "javax-to-jakarta", [str(log)])

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert _commit_files(repo) == [MIGRATED_REL]
    assert outcome.observation["ignored"] == ["src/main/java/com/corp/user/debug.log"]
    assert _git(repo, "status", "--porcelain") == "", "a half-landing: something was copied and not committed"


def test_when_every_file_is_ignored_nothing_is_copied_and_no_branch_is_made(repo, tmp_path, config):
    (repo / ".gitignore").write_text("src/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore src")
    out = _output(tmp_path)

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert "ignored" in outcome.observation["error"]
    assert _branches(repo) == ["main"] and _head(repo) == "main"
    assert _work_tree(repo) == [".gitignore", "README.md"], "a refused landing copied files anyway"


def test_a_refused_landing_copies_nothing_at_all(repo, tmp_path, config):
    """A failed precondition leaves the repository exactly as it was: no branch, no file."""
    (repo / "README.md").write_text("an unfinished edit\n", encoding="utf-8")
    out = _output(tmp_path)
    (out / STRAY_REL).write_text("class Other {}\n", encoding="utf-8")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert _branches(repo) == ["main"]
    assert _work_tree(repo) == ["README.md"]
    assert _git(repo, "status", "--porcelain") == "M README.md"


def test_an_output_holding_only_unrecorded_files_is_refused_and_says_what_is_there(repo, tmp_path, config):
    out = _output(tmp_path, migrated=False)
    (out / "src/main/java/com/corp").mkdir(parents=True)
    (out / STRAY_REL).write_text("class Other {}\n", encoding="utf-8")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert STRAY_REL in outcome.observation["error"]
    assert _branches(repo) == ["main"]


# ─── the output folder inside the repository (<repo>/.migrated) ─────────────

def _exclude_lines(repo):
    path = Path(repo) / ".git" / "info" / "exclude"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def test_an_output_folder_inside_the_repository_is_excluded_locally_and_the_landing_proceeds(repo, tmp_path,
                                                                                              config):
    """The chat's default output is ``<repo>/.migrated``: untracked, so every
    landing would refuse on a dirty tree made of FORGE's own folder. It goes
    into ``.git/info/exclude`` — local, never committed — and the project's
    ``.gitignore`` is never touched."""
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore logs")
    out = _output(tmp_path, at=repo / ".migrated")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert "/.migrated/" in _exclude_lines(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == "*.log\n", "the project's .gitignore is theirs"
    assert _commit_files(repo) == [MIGRATED_REL], "nothing under .migrated/ may be committed"
    assert _git(repo, "status", "--porcelain") == "", "the output folder still shows as a change"
    assert outcome.observation["excluded"]["pattern"] == "/.migrated/"
    assert outcome.observation["excluded"]["added"] is True
    card = outcome.cards[0]
    assert card["excluded"] == "/.migrated/" and card["exclude_added"] is True
    # The output folder is still there, untouched, and still FORGE's.
    assert (out / MIGRATED_REL).is_file() and (out / "manual-review-queue.json").is_file()


def test_the_exclude_is_idempotent_and_leaves_existing_lines_alone(repo):
    from forge.leader.landing import exclude_output

    info = repo / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("# the user's own\n*.swp", encoding="utf-8")    # no trailing newline

    assert exclude_output(str(repo), ".migrated") == ("/.migrated/", True)
    assert exclude_output(str(repo), ".migrated") == ("/.migrated/", False)

    lines = _exclude_lines(repo)
    assert lines[:2] == ["# the user's own", "*.swp"], lines
    assert lines.count("/.migrated/") == 1, lines


def test_an_exclude_pattern_matches_the_folder_literally():
    from forge.leader.landing import exclude_pattern

    assert exclude_pattern(".migrated") == "/.migrated/"
    assert exclude_pattern("tools/out[1]*") == "/tools/out\\[1]\\*/"


def test_a_refused_landing_writes_no_exclude(repo, tmp_path, config):
    """Every other refusal is ruled out before the exclude is written: an
    existing branch leaves .git/info/exclude as it was."""
    _git(repo, "branch", BRANCH)
    out = _output(tmp_path, at=repo / ".migrated")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False and BRANCH in outcome.observation["error"]
    assert "/.migrated/" not in _exclude_lines(repo)


def test_tracked_files_under_the_output_folder_still_count_as_uncommitted(repo, tmp_path, config):
    """An exclude hides untracked files only. A tracked, modified file under
    the output folder is somebody's change, and landing still refuses."""
    out = _output(tmp_path, at=repo / ".migrated")
    (out / "notes.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "-f", ".migrated/notes.txt")
    _git(repo, "commit", "-qm", "someone committed into the output folder")
    (out / "notes.txt").write_text("changed\n", encoding="utf-8")

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is False
    assert "uncommitted" in outcome.observation["error"]
    assert "tracks files under it" in outcome.observation["error"], outcome.observation["error"]
    assert _branches(repo) == ["main"]


def test_a_nested_forge_output_inside_the_output_is_never_landed(repo, tmp_path, config):
    """Another run's output inside this one — even recorded in the manifest —
    is not this migration."""
    out = _output(tmp_path, at=repo / ".migrated")
    nested = out / ".migrated" / MIGRATED_REL
    nested.parent.mkdir(parents=True)
    nested.write_text(MIGRATED, encoding="utf-8")
    run_manifest.record(str(out), "javax-to-jakarta", [str(nested)])

    outcome, _ = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert _commit_files(repo) == [MIGRATED_REL]


def test_the_branch_landing_started_from_is_recorded_for_the_pull_request(repo, tmp_path, config):
    """open_pull_request opens the PR into the branch landing started from
    (h2-native on AMS), so landing records it — on the result and the chat."""
    _git(repo, "checkout", "-q", "-b", "h2-native")
    out = _output(tmp_path, at=repo / ".migrated")

    outcome, convo = _land(repo, out, config)

    assert outcome.ok is True, outcome.observation
    assert outcome.observation["base_branch"] == "h2-native"
    assert outcome.cards[0]["base_branch"] == "h2-native"
    assert convo.last_landed == BRANCH
    assert convo.landings[BRANCH]["base_branch"] == "h2-native"
    assert convo.landings[BRANCH]["commit"] == outcome.observation["commit"]


def test_the_exclude_lands_in_the_common_git_dir_of_a_linked_worktree(repo, tmp_path, config):
    """In a linked worktree `.git` is a file; `git rev-parse --git-path` finds the exclude."""
    tree = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "h2-native", str(tree))
    out = _output(tmp_path, at=tree / ".migrated")

    outcome, _ = _land(tree, out, config)

    assert outcome.ok is True, outcome.observation
    assert outcome.observation["base_branch"] == "h2-native"
    assert "/.migrated/" in _exclude_lines(repo)
    assert _git(tree, "status", "--porcelain") == ""


# ─── the cross-check that keeps the list honest ──────────────────────────────

def test_every_artifact_the_ui_lists_is_one_landing_refuses_to_commit():
    """A new artifact must not be able to start being committed in silence.

    ``ui/app.py``'s ``ARTIFACTS`` is the list of files the UI offers for
    download — which is to say, the list of files FORGE writes into the output
    directory. Adding a row there is how a new artifact appears; this test is
    what makes that also a change to the landing exclusion list, rather than a
    file that quietly ends up in the engineer's next commit.
    """
    from forge.ui.app import ARTIFACTS

    listed = {name for name, _label in ARTIFACTS}
    missing = sorted(listed - set(ARTIFACT_NAMES))
    assert not missing, (
        f"{missing} is written into output_dir and offered for download, but landing.ARTIFACT_NAMES "
        "does not exclude it — it would be committed into the user's repository")
