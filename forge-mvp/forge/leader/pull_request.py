"""Publishing a landed branch: push it to ``origin`` and open a pull request.

This is the only thing in FORGE that sends anything off the machine that is not
a model call — the user's code, to a place other people read. So it is
``land_on_branch``'s rules one step further out:

**Always a click.** ``open_pull_request`` parks a confirmation card whatever it
costs ($0). The leader may offer it; only the button runs it.

**It publishes only what this chat landed.** The branch must be one
``land_on_branch`` created in this conversation, and it must exist locally.
Exactly that branch is pushed — ``git push -u origin <branch>``, never
``--force``, never a second ref — and the PR goes into the branch landing
started from (``base_branch``), unless the user names another.

**Every precondition refuses, before anything leaves the machine.** No branch,
no ``origin`` remote, no ``gh`` or an unauthenticated one, nothing on the branch
beyond its base: each is a sentence naming the fix, and nothing is pushed. A PR
that already exists for the branch is not an error — its URL comes back.

**The body is built by code, not a model**, from FORGE's own records: the packs
the chat ran, per-pack totals from ``migration-summary.json``, how many files
still wait on a human, and the project build verdict — said plainly when it
FAILED, was not run, or is stale. It never carries source code, a diff or
compiler output: it is published, and GUARDRAILS §7's boundary does not stop at
the edge of the machine.

Every call goes through an injectable ``runner(argv, cwd)`` so the tests drive
fake ``git``/``gh`` without a network.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# A push of a large migration over a slow link, or a gh call waiting on the API.
TIMEOUT_SECONDS = 180
STDERR_CAP = 300
TITLE_CAP = 200
# Pack rows in the body; a plan has about ten, so this is only a backstop.
PACK_ROW_CAP = 40

Runner = Callable[[List[str], str], subprocess.CompletedProcess]


class ToolMissing(RuntimeError):
    """git or gh is not installed, or did not answer in time."""


def default_runner(argv: List[str], cwd: str) -> subprocess.CompletedProcess:
    """One command, list argv, never a shell, never ``check=True``.

    Prompts are switched off: a credential prompt nobody can see would hang the
    turn instead of failing it with a sentence.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False,
                              timeout=TIMEOUT_SECONDS, env=env)
    except FileNotFoundError as e:
        raise ToolMissing(f"{argv[0]} is not installed, or is not on this machine's PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ToolMissing(f"`{' '.join(argv[:2])}` did not finish within {TIMEOUT_SECONDS}s") from e


def _err(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    return text[:STDERR_CAP] or f"exited {proc.returncode}"


def _fail(error: str, state: str = "nothing was pushed and no pull request was opened") -> Dict[str, Any]:
    return {"ok": False, "error": error, "state": state}


_URL_RE = re.compile(r"https://\S+/pull/\d+")


def github_repo(remote_url: str) -> str:
    """``[HOST/]OWNER/REPO`` for ``gh --repo`` from an ``origin`` URL, or ``""``.

    Passing it keeps gh from guessing between remotes (it refuses to guess when
    there are several and nothing is set as the default).
    """
    url = str(remote_url or "").strip()
    m = (re.match(r"^[\w.+-]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)/(.+?)(?:\.git)?/?$", url)
         or re.match(r"^(?:[^@/]+@)?([^/:]+):(.+?)/(.+?)(?:\.git)?/?$", url))
    if not m:
        return ""
    host, owner, repo = m.group(1), m.group(2), m.group(3)
    if "/" in owner or "/" in repo:
        return ""
    return f"{owner}/{repo}" if host == "github.com" else f"{host}/{owner}/{repo}"


# ─── the body ─────────────────────────────────────────────────────────────────

def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def pack_rows(output_dir: str) -> List[Dict[str, Any]]:
    """Per-pack totals from ``migration-summary.json``: counts only, never file text."""
    import json

    from forge.utils.report import SUMMARY_RECORD

    try:
        data = json.loads((Path(output_dir) / SUMMARY_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    packs = data.get("packs") if isinstance(data, dict) else None
    rows = []
    for pack, row in (packs.items() if isinstance(packs, dict) else []):
        t = row.get("totals") if isinstance(row, dict) and isinstance(row.get("totals"), dict) else {}
        rows.append({"pack": str(pack), **{k: _int(t.get(k)) for k in ("total", "passed", "manual", "blocked", "held")},
                     "acceptance": str((row or {}).get("acceptance") or "not run")})
    return rows[:PACK_ROW_CAP]


def awaiting_review(output_dir: str) -> int:
    """Files in the review queue still waiting on a human (held, manual review, blocked)."""
    from forge.review_queue import REVIEW_STATUSES, load_queue

    try:
        queue = load_queue(output_dir)
    except Exception:  # noqa: BLE001 — no queue, or an unreadable one: nothing counted
        return 0
    return sum(1 for e in (queue.get("entries") or [])
               if isinstance(e, dict) and e.get("status") in REVIEW_STATUSES)


def _cell(value: Any) -> str:
    """A table cell from FORGE's own data: no pipes, no line breaks."""
    return str(value).replace("|", "/").replace("\n", " ").strip()


def build_line(build: Optional[dict]) -> str:
    """The project build verdict, plainly. Never compiler output."""
    build = build if isinstance(build, dict) else {}
    outcome = str(build.get("outcome") or "not_run")
    detail = _cell(str(build.get("detail") or "")[:200])
    step = _cell(build.get("failed_step") or "")
    if outcome == "not_run":
        text = "**NOT RUN** — FORGE did not build this migration. It has not been compiled."
    elif outcome == "fail":
        text = "**FAILED**" + (f" at `{step}`" if step else "") + (f" — {detail}" if detail else "") + \
               ". The branch does not compile as it stands."
    elif outcome == "pass":
        text = "**PASSED**" + (f" — {detail}" if detail else "")
    elif outcome == "skip":
        text = "**SKIPPED**" + (f" — {detail}" if detail else "") + ". It has not been compiled."
    else:
        text = f"**{outcome.upper()}**" + (f" — {detail}" if detail else "")
    if build.get("stale") and outcome != "not_run":
        text += (" **STALE:** the migrated files changed after this build, so the verdict is about a "
                 "tree that no longer exists.")
    return text


def pr_body(*, branch: str, base: str, commit: str = "", packs: Sequence[str] = (),
            rows: Sequence[dict] = (), awaiting: int = 0, build: Optional[dict] = None) -> str:
    """The pull request description: deterministic markdown from FORGE's own records."""
    lines = ["## FORGE migration", "",
             f"This pull request brings `{_cell(branch)}` into `{_cell(base)}`"
             + (f" (commit `{_cell(commit)}`)" if commit else "") + ".", ""]
    landed = [str(p) for p in packs if p]
    if landed:
        lines += ["**Packs landed**, in the order they ran:", ""] + [f"1. `{_cell(p)}`" for p in landed] + [""]
    else:
        lines += ["**Packs landed:** none recorded in this chat.", ""]
    if rows:
        lines += ["### Per-pack results", "",
                  "| Pack | Files | Passed | Manual review | Blocked | Held | Acceptance |",
                  "|------|------:|-------:|--------------:|--------:|-----:|------------|"]
        for r in rows:
            lines.append(f"| `{_cell(r['pack'])}` | {r['total']} | {r['passed']} | {r['manual']} | {r['blocked']} "
                         f"| {r['held']} | {_cell(r['acceptance'])} |")
        lines.append("")
    lines += ["### Files awaiting review", "",
              (f"**{awaiting}** file(s) are still waiting on a human (held, manual review or blocked). "
               "They are not in this branch." if awaiting else "None — no file is waiting on a human."), ""]
    lines += ["### Project build", "", build_line(build), ""]
    lines += ["---", "",
              "Generated by FORGE. Every file in this branch was written by a FORGE migration run or "
              "approved by a reviewer; FORGE's own reports and review queue are not included."]
    return "\n".join(lines) + "\n"


def default_title(packs: Sequence[str]) -> str:
    landed = [str(p) for p in packs if p]
    if not landed:
        return "FORGE migration"
    if len(landed) <= 3:
        return f"FORGE migration: {', '.join(landed)}"
    return f"FORGE migration: {', '.join(landed[:3])} and {len(landed) - 3} more"


# ─── preconditions and the publish ────────────────────────────────────────────

def _ref_exists(run: Runner, cwd: str, ref: str) -> bool:
    return run(["git", "rev-parse", "--verify", "--quiet", ref], cwd).returncode == 0


def preconditions(source_dir: str, branch: str, base: str, *, landed: Sequence[str],
                  runner: Optional[Runner] = None) -> Optional[str]:
    """Why this must not publish, or None. Runs nothing that changes anything."""
    run = runner or default_runner
    source = str(source_dir or "")
    branch = str(branch or "").strip()
    base = str(base or "").strip()
    if not landed:
        return ("nothing has been landed in this chat — land the migration on a branch first "
                "(land_on_branch); FORGE only opens a pull request for a branch it made here")
    if not branch:
        return "name the branch to open a pull request for"
    if branch not in landed:
        return (f"'{branch}' is not a branch this chat landed ({', '.join(landed)}). FORGE only pushes "
                "a branch it created itself")
    if branch.startswith("-") or base.startswith("-"):
        return "a branch name must not start with '-'"
    if not source or not Path(source).is_dir():
        return f"no project set yet, or {source or '(none)'} is not a directory"
    try:
        legal = run(["git", "check-ref-format", "--branch", branch], source)
        if legal.returncode != 0:
            return f"'{branch}' is not a valid git branch name: {_err(legal)}"
        if not _ref_exists(run, source, f"refs/heads/{branch}"):
            return (f"branch '{branch}' does not exist in {source} any more — it was renamed or deleted "
                    "after landing; land again")
        origin = run(["git", "remote", "get-url", "origin"], source)
        if origin.returncode != 0 or not (origin.stdout or "").strip():
            return ("this repository has no `origin` remote, so there is nowhere to push. Add one "
                    "(`git remote add origin <url>`) and ask again — FORGE will not add it for you")
        try:
            version = run(["gh", "--version"], source)
        except ToolMissing:
            return ("the GitHub CLI (`gh`) is not installed, and FORGE opens pull requests with it. "
                    "Install it (https://cli.github.com) and run `gh auth login`, then ask again")
        if version.returncode != 0:
            return f"the GitHub CLI (`gh`) did not run: {_err(version)}"
        auth = run(["gh", "auth", "status"], source)
        if auth.returncode != 0:
            return ("the GitHub CLI is not signed in — run `gh auth login` yourself, then ask again. "
                    f"({_err(auth)})")
        if not base:
            return ("there is no base branch to open the pull request into: landing started from a "
                    "detached HEAD. Name the base branch")
        if base == branch:
            return f"the base branch and the branch are both '{branch}'"
        legal = run(["git", "check-ref-format", "--branch", base], source)
        if legal.returncode != 0:
            return f"'{base}' is not a valid git branch name: {_err(legal)}"
        base_ref = next((r for r in (f"refs/heads/{base}", f"refs/remotes/origin/{base}")
                         if _ref_exists(run, source, r)), None)
        if base_ref is None:
            return f"the base branch '{base}' exists neither locally nor on origin"
        ahead = run(["git", "rev-list", "--count", f"{base_ref}..refs/heads/{branch}"], source)
        if ahead.returncode != 0:
            return f"git could not compare '{branch}' with '{base}': {_err(ahead)}"
        if _int((ahead.stdout or "").strip()) <= 0:
            return (f"'{branch}' has no commits beyond '{base}' — there is nothing for a pull request "
                    "to propose")
    except ToolMissing as e:
        return str(e)
    return None


def _existing_pr(run: Runner, source: str, branch: str, repo_args: List[str]) -> str:
    """The URL of an open pull request whose head is ``branch``, or ``""``."""
    try:
        found = run(["gh", "pr", "list", *repo_args, "--head", branch, "--state", "open",
                     "--json", "url", "--jq", ".[0].url"], source)
    except ToolMissing:
        return ""
    url = (found.stdout or "").strip() if found.returncode == 0 else ""
    return url if _URL_RE.fullmatch(url) else ""


def open_pull_request(source_dir: str, branch: str, base: str, *, title: str, body: str,
                      landed: Sequence[str], runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Push ``branch`` to origin and open a PR into ``base``. Returns a JSON-safe dict.

    ``{"ok": False, "error", "state"}`` or ``{"ok": True, "url", "branch", "base",
    "existing"}``. Nothing here raises: the caller is a tool (R6).
    """
    run = runner or default_runner
    branch, base = str(branch or "").strip(), str(base or "").strip()
    refusal = preconditions(source_dir, branch, base, landed=landed, runner=run)
    if refusal:
        return _fail(refusal)
    source = str(Path(source_dir).expanduser().resolve())
    title = " ".join(str(title or "").split())[:TITLE_CAP] or "FORGE migration"

    try:
        origin = run(["git", "remote", "get-url", "origin"], source)
        slug = github_repo((origin.stdout or "").strip())
        repo_args = ["--repo", slug] if slug else []

        # Exactly one ref, never forced. `-u` records origin as its upstream.
        pushed = run(["git", "push", "-u", "origin", branch], source)
        if pushed.returncode != 0:
            return _fail(f"git push failed: {_err(pushed)}")

        with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="forge-pr-", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
            body_file = fh.name
        try:
            made = run(["gh", "pr", "create", *repo_args, "--base", base, "--head", branch,
                        "--title", title, "--body-file", body_file], source)
        finally:
            try:
                os.unlink(body_file)
            except OSError:
                pass
    except ToolMissing as e:
        return _fail(str(e), "the push may have happened; no pull request was opened")

    pushed_state = f"'{branch}' was pushed to origin; no pull request was opened"
    if made.returncode == 0:
        match = _URL_RE.search(made.stdout or "")
        if match:
            return {"ok": True, "url": match.group(0), "branch": branch, "base": base, "existing": False}
    existing = _existing_pr(run, source, branch, repo_args)
    if existing:
        return {"ok": True, "url": existing, "branch": branch, "base": base, "existing": True}
    if made.returncode == 0:
        return _fail("gh created something but printed no pull request URL", pushed_state)
    return _fail(f"gh pr create failed: {_err(made)}", pushed_state)
