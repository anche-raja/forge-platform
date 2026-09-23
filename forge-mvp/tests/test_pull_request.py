"""``open_pull_request`` — the only way FORGE pushes anything.

Landing commits onto a new local branch; this publishes that branch: ``git push
-u origin <branch>`` and ``gh pr create``. It is the one step that sends the
user's code somewhere other people read, so it is ``land_on_branch``'s rules one
step further out, and these tests are the warranty for them: always a click;
only the branch this chat landed, never forced; every precondition a refusal
with nothing pushed; a description built by code from FORGE's records that
never carries source, a diff or compiler output.

No network: ``origin`` is a bare repository under ``tmp_path`` (a real ``git
push``, to a directory), and ``gh`` is a fake behind the injectable runner.
"""

import json
import subprocess
from pathlib import Path

import pytest

from forge.leader import tools as tools_module
from forge.leader.agent import _SYSTEM, state_block
from forge.leader.convo import Conversation
from forge.leader.pull_request import (ToolMissing, build_line, default_title, github_repo, pack_rows,
                                       pr_body)
from forge.leader.settings import LeaderSettings
from forge.leader.tools import ProjectContext, Toolbox
from forge.review_queue import QUEUE_NAME
from forge.utils.report import SUMMARY_RECORD
from tests.conftest import write_config
from tests.test_landing import ARTIFACT_MARKER, SOURCE_MARKER, _git, _output

BRANCH = "forge/jakarta"
BASE = "h2-native"
PR_URL = "https://github.com/acme/ams/pull/7"
# Planted in the build record's compiler output. The PR body is published; the
# tail may reach the build card in the browser and nowhere else.
COMPILER_MARKER = "C0MPILER_TAIL_pr_51c9"


# ─── a repository with a landed branch and a bare origin ─────────────────────

@pytest.fixture
def config(tmp_path):
    return write_config(tmp_path)


@pytest.fixture
def repo(tmp_path):
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
    _git(root, "checkout", "-q", "-b", BASE)
    return root


@pytest.fixture
def origin(tmp_path, repo):
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", BASE)
    return bare


def _remote_branches(bare):
    return sorted(_git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split())


def _cp(argv, code=0, out="", err=""):
    return subprocess.CompletedProcess(argv, code, out, err)


class FakeGh:
    """Real git, fake gh. Records every argv, and the body gh was handed."""

    def __init__(self, *, installed=True, authed=True, create=(0, PR_URL + "\n", ""), existing=""):
        self.installed, self.authed, self.create, self.existing = installed, authed, create, existing
        self.calls = []
        self.body = None

    def __call__(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        assert argv[0] == "gh", argv
        if not self.installed:
            raise ToolMissing("gh is not installed, or is not on this machine's PATH")
        sub = argv[1:]
        if sub == ["--version"]:
            return _cp(argv, 0, "gh version 2.60.0\n")
        if sub[:2] == ["auth", "status"]:
            return _cp(argv, 0, "Logged in to github.com") if self.authed else \
                _cp(argv, 1, "", "You are not logged into any GitHub hosts. Run gh auth login")
        if sub[:2] == ["pr", "create"]:
            self.body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
            return _cp(argv, *self.create)
        if sub[:2] == ["pr", "list"]:
            return _cp(argv, 0, self.existing + "\n" if self.existing else "")
        raise AssertionError(f"unexpected gh call: {argv}")

    def pushes(self):
        return [c for c in self.calls if c[:2] == ["git", "push"]]

    def gh(self, *prefix):
        return [c for c in self.calls if c[0] == "gh" and c[1:1 + len(prefix)] == list(prefix)]


@pytest.fixture
def fake(monkeypatch):
    runner = FakeGh()
    monkeypatch.setattr(tools_module, "PR_RUNNER", runner)
    return runner


def _box(repo, out, config, convo=None):
    convo = convo if convo is not None else Conversation()
    ctx = ProjectContext(source_dir=str(repo), output_dir=str(out), config=config, base_config=config)
    return Toolbox(ctx, convo, LeaderSettings.from_config(config), lambda event: None, None), convo


def _landed(repo, config, *, completed=("javax-to-jakarta", "java8-to-java21")):
    """Land for real, through the tool, from BASE with the output inside the repository."""
    out = _output(None, at=repo / ".migrated")
    convo = Conversation()
    convo.completed = list(completed)
    box, _ = _box(repo, out, config, convo)
    landed = box.execute("land_on_branch", {"branch": BRANCH}, tool_id="t1", confirmed=True)
    assert landed.ok is True, landed.observation
    return box, convo, out


# ─── the click ────────────────────────────────────────────────────────────────

def test_opening_a_pull_request_always_waits_for_a_click_and_shows_what_it_will_publish(
        repo, origin, config, fake):
    box, convo, _ = _landed(repo, config)

    outcome = box.execute("open_pull_request", {}, tool_id="t2")

    assert outcome.ok is True and outcome.needs_confirmation is True
    assert outcome.pending_id in convo.pending
    card = outcome.cards[0]
    assert card["kind"] == "confirm" and card["tool"] == "open_pull_request"
    assert card["est_usd"] == 0.0
    assert card["title"] == f"Push branch '{BRANCH}' to origin and open a PR into '{BASE}'"
    assert "Generated by FORGE" in card["preview"] and "NOT RUN" in card["preview"]
    assert card["build"]["outcome"] == "not_run"
    assert fake.pushes() == [] and fake.gh("pr") == [], "nothing may leave the machine before the click"
    assert _remote_branches(origin) == [BASE]


def test_a_confirmed_pull_request_pushes_exactly_the_landed_branch_and_returns_the_url(
        repo, origin, config, fake):
    box, convo, _ = _landed(repo, config)

    outcome = box.execute("open_pull_request", {}, tool_id="t2", confirmed=True)

    assert outcome.ok is True, outcome.observation
    assert outcome.observation == {"url": PR_URL, "branch": BRANCH, "base": BASE}, \
        "the observation is the url, the branch and the base — nothing else"
    assert fake.pushes() == [["git", "push", "-u", "origin", BRANCH]], "one ref, never forced"
    assert not any("--force" in c or "-f" in c or any(a.startswith("+") for a in c) for c in fake.calls)
    assert _remote_branches(origin) == sorted([BASE, BRANCH]), "only the landed branch was pushed"
    assert _git(origin, "rev-parse", BRANCH) == _git(repo, "rev-parse", BRANCH)

    (create,) = fake.gh("pr", "create")
    assert create[create.index("--base") + 1] == BASE
    assert create[create.index("--head") + 1] == BRANCH
    assert create[create.index("--title") + 1] == "FORGE migration: javax-to-jakarta, java8-to-java21"

    card = outcome.cards[0]
    assert card["kind"] == "pull_request" and card["url"] == PR_URL and card["existing"] is False
    assert convo.pull_requests == {BRANCH: PR_URL}
    assert f"pull request {PR_URL}" in state_block(convo, box.ctx)


def test_the_published_description_is_forges_and_never_carries_code_or_compiler_output(
        repo, origin, config, fake):
    box, convo, out = _landed(repo, config)
    (out / "project-build.json").write_text(json.dumps({
        "outcome": "fail", "detail": "ams-common failed (exit 1)", "failed_step": "ams-common",
        "tail": [f"[ERROR] {COMPILER_MARKER} cannot find symbol"], "fingerprint": "stale-on-purpose",
        "source_dir": str(repo)}), encoding="utf-8")
    (out / SUMMARY_RECORD).write_text(json.dumps({"version": 1, "reverted": {}, "packs": {
        "javax-to-jakarta": {"totals": {"total": 12, "passed": 10, "manual": 1, "blocked": 0, "held": 1},
                             "acceptance": "PASS"}}}), encoding="utf-8")
    (out / QUEUE_NAME).write_text(json.dumps({"version": 2, "run": "r1", "entries": [
        {"rel_path": "src/A.java", "pack": "javax-to-jakarta", "status": "HELD",
         "original": f"class A {{ /* {SOURCE_MARKER} */ }}", "transformed": {"src/A.java": SOURCE_MARKER}}]}),
        encoding="utf-8")

    outcome = box.execute("open_pull_request", {"title": "Jakarta for AMS"}, tool_id="t2", confirmed=True)

    assert outcome.ok is True, outcome.observation
    body = fake.body
    assert "`javax-to-jakarta`" in body and "`java8-to-java21`" in body
    assert "| `javax-to-jakarta` | 12 | 10 | 1 | 0 | 1 | PASS |" in body
    assert "**1** file(s) are still waiting on a human" in body
    assert "**FAILED** at `ams-common`" in body and "**STALE:**" in body
    assert "Generated by FORGE" in body
    for marker in (SOURCE_MARKER, ARTIFACT_MARKER, COMPILER_MARKER):
        assert marker not in body, f"{marker} reached a published pull request"
    assert "```" not in body and "@@" not in body, "no code block, no diff"
    assert SOURCE_MARKER not in json.dumps(outcome.observation)


def test_an_existing_pull_request_is_an_answer_not_a_failure(repo, origin, config, fake):
    box, convo, _ = _landed(repo, config)
    fake.create = (1, "", f'a pull request for branch "{BRANCH}" into branch "{BASE}" already exists:\n{PR_URL}')
    fake.existing = PR_URL

    outcome = box.execute("open_pull_request", {}, tool_id="t2", confirmed=True)

    assert outcome.ok is True, outcome.observation
    assert outcome.observation["url"] == PR_URL
    assert outcome.cards[0]["existing"] is True


def test_the_user_can_name_another_base(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    _git(repo, "push", "-q", "origin", "main")

    outcome = box.execute("open_pull_request", {"base": "main"}, tool_id="t2", confirmed=True)

    assert outcome.ok is True, outcome.observation
    (create,) = fake.gh("pr", "create")
    assert create[create.index("--base") + 1] == "main"


# ─── refusals: nothing leaves the machine ────────────────────────────────────

def _refused(outcome, fake, origin, *words):
    assert outcome.ok is False, outcome.observation
    error = outcome.observation["error"]
    for w in words:
        assert w in error, error
    assert fake.pushes() == [], "a refusal pushed"
    assert fake.gh("pr") == [], "a refusal reached gh pr"
    if origin is not None:
        assert BRANCH not in _remote_branches(origin)
    return error


def test_nothing_landed_in_this_chat_is_refused(repo, origin, config, fake):
    box, _ = _box(repo, repo / ".migrated", config)
    _refused(box.execute("open_pull_request", {}, tool_id="t1", confirmed=True), fake, origin, "land")


def test_a_branch_this_chat_did_not_land_is_never_pushed(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    _git(repo, "branch", "someone/else")
    outcome = box.execute("open_pull_request", {"branch": "someone/else"}, tool_id="t2", confirmed=True)
    _refused(outcome, fake, origin, "someone/else", "only pushes")
    assert "someone/else" not in _remote_branches(origin)


def test_a_landed_branch_that_is_gone_is_refused(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    _git(repo, "checkout", "-q", BASE)
    _git(repo, "branch", "-D", BRANCH)
    _refused(box.execute("open_pull_request", {}, tool_id="t2", confirmed=True), fake, origin, "does not exist")


def test_no_origin_remote_is_refused_and_none_is_added(repo, config, fake):
    box, _, _ = _landed(repo, config)
    _refused(box.execute("open_pull_request", {}, tool_id="t2", confirmed=True), fake, None, "origin")
    assert _git(repo, "remote") == ""


def test_no_github_cli_is_refused(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    fake.installed = False
    _refused(box.execute("open_pull_request", {}, tool_id="t2", confirmed=True), fake, origin, "gh", "install")


def test_an_unauthenticated_github_cli_is_refused(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    fake.authed = False
    _refused(box.execute("open_pull_request", {}, tool_id="t2", confirmed=True), fake, origin, "gh auth login")


def test_a_branch_with_nothing_beyond_its_base_is_refused(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    _git(repo, "checkout", "-q", BASE)
    _git(repo, "merge", "-q", "--ff-only", BRANCH)
    _refused(box.execute("open_pull_request", {}, tool_id="t2", confirmed=True), fake, origin, "no commits beyond")


def test_a_failed_push_opens_no_pull_request(repo, origin, config, fake):
    box, _, _ = _landed(repo, config)
    _git(repo, "remote", "set-url", "--push", "origin", str(Path(origin).parent / "missing.git"))
    outcome = box.execute("open_pull_request", {}, tool_id="t2", confirmed=True)
    assert outcome.ok is False and "push failed" in outcome.observation["error"]
    assert fake.gh("pr") == []


# ─── the pieces ───────────────────────────────────────────────────────────────

def test_the_repo_for_gh_comes_from_the_origin_url():
    assert github_repo("https://github.com/anche-raja/ams.git") == "anche-raja/ams"
    assert github_repo("git@github.com:anche-raja/ams.git") == "anche-raja/ams"
    assert github_repo("ssh://git@github.com/anche-raja/ams") == "anche-raja/ams"
    assert github_repo("https://ghe.corp.example/team/ams.git") == "ghe.corp.example/team/ams"
    assert github_repo("/tmp/origin.git") == ""


def test_the_build_verdict_is_said_plainly_whatever_it_is():
    assert "**NOT RUN**" in build_line(None)
    assert "**PASSED**" in build_line({"outcome": "pass", "detail": "3 step(s) built"})
    assert "**STALE:**" in build_line({"outcome": "pass", "stale": True})
    assert "**SKIPPED**" in build_line({"outcome": "skip", "detail": "'mvn' is not on PATH"})
    failed = build_line({"outcome": "fail", "failed_step": "ams-common", "tail": [COMPILER_MARKER]})
    assert "**FAILED** at `ams-common`" in failed and COMPILER_MARKER not in failed


def test_the_body_is_deterministic(tmp_path):
    kwargs = dict(branch=BRANCH, base=BASE, commit="a1b2c3d", packs=["javax-to-jakarta"],
                  rows=[{"pack": "javax-to-jakarta", "total": 3, "passed": 3, "manual": 0, "blocked": 0,
                         "held": 0, "acceptance": "PASS"}], awaiting=0, build={"outcome": "pass"})
    assert pr_body(**kwargs) == pr_body(**kwargs)
    assert pack_rows(str(tmp_path)) == [], "no summary record is no rows, not an error"
    assert default_title([]) == "FORGE migration"
    assert default_title(["a", "b", "c", "d"]) == "FORGE migration: a, b, c and 1 more"


def test_the_leader_is_told_to_offer_it_and_never_to_push_by_hand():
    assert "open_pull_request" in _SYSTEM
    assert "Never suggest pushing by hand" in _SYSTEM
    assert "always needs their click" in _SYSTEM
