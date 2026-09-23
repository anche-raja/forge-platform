"""Putting a finished migration onto a branch of the user's own repository.

This is the only thing in FORGE that writes into the source tree. Every run,
every applied decision, every generated test goes to ``output_dir`` instead —
which is what makes a migration something you can throw away by deleting a
directory. Landing gives that up deliberately and once, at a point the user
clicks on, so the rule here is the inverse of the pipeline's:

**Every precondition refuses; none of them repairs.** No ``git stash``, no
``-f``, no ``--amend``, no ``git init``, no ``git add -A``, and never a push.
A dirty work tree is somebody's unsaved work and a tool that stashes it has
taken a decision the user did not make; an existing branch is a history a
``-f`` checkout would strand. Each refusal names the condition and what the
user would do about it, and stops.

**What lands is an allow-list, not a filter of the obvious.**
``manual-review-queue.json`` embeds every reviewed file's ORIGINAL text
verbatim (``review_queue.py:build_entry``), so committing it would copy the
user's source — including whatever the secret scan found in it — into a git
history they did not choose to put it in. That one file is the reason
:data:`ARTIFACT_NAMES` exists and is defined here rather than at the call site,
and a test cross-checks it against ``forge/ui/app.py``'s ``ARTIFACTS`` so a new
artifact cannot quietly start being committed.

**What lands is also only what FORGE wrote.** A file in ``output_dir`` that no
run recorded in ``.forge-writes.json`` -- and no reviewer approved in
``decisions-applied.jsonl`` -- is not part of the migration: a stray test
fixture, a file someone copied in by hand, a ``.DS_Store``. It is left behind
and named in the result, never committed (:func:`unrecorded_files`).

**A failed step reports the state; it does not roll back.** Half-undoing a
checkout is how a tool turns one problem into two. Every git call is
``subprocess.run`` with a list argv and ``check=False``, and a non-zero exit
comes back as a refusal carrying git's own stderr and a sentence saying which
branch the repository is now on.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from forge.utils import run_manifest
from forge.utils.file_writer import STAGING_DIR

# FORGE's own output, which is never project code. `manual-review-queue.json`
# is the dangerous one — it carries the source verbatim — but every name here
# would be noise in someone's repository.
#
# `test-generation-report.md` is in this list and NOT in the INCREMENT-2 spec's
# copy of it: the spec also requires this set to cover every name in
# `forge/ui/app.py`'s ARTIFACTS, and that tuple has it. The stricter reading
# wins, because the cost of a missing name is a committed artifact and the cost
# of an extra one is a report the user can still download from the Artifacts
# card.
ARTIFACT_NAMES = frozenset({
    "migration-report.md",
    "manual-review-queue.json",
    "migration-review.html",
    "migration-acceptance.json",
    "migration-context.json",
    "stack-profile.json",
    "forge-profile.yaml",
    "intent-plan.json",
    "generated-tests.json",
    "test-generation-report.md",
    "decisions-applied.jsonl",
    "pack-feedback.md",
    "project-build.json",
    "migration-summary.md",
    "migration-summary.json",
})

# The line the repository's own history carries on work Claude had a hand in.
# A landed migration was written by models, and a commit that hides that is a
# worse record than one that says so.
CO_AUTHOR_LINE = "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"

DEFAULT_BRANCH_HINT = "e.g. forge/jakarta-migration"
PUSH_COMMAND = "git push -u origin {branch}"
# Long enough for a pre-commit hook on a large repository, short enough that a
# hook waiting on a prompt fails the tool instead of hanging the turn.
GIT_TIMEOUT_SECONDS = 120
# argv has a length limit and a migration can touch thousands of files.
ADD_BATCH = 200
# The commit message may be model-authored. It reaches git through a list argv,
# so there is nothing to inject — but there is no reason for it to be a novel.
MESSAGE_CAP = 4000
STDERR_CAP = 200


class GitUnavailable(RuntimeError):
    """git is not installed, or did not answer in time."""


# ─── what lands ───────────────────────────────────────────────────────────────

def is_artifact(rel: str) -> bool:
    """True for FORGE's own output, which must never be committed.

    The name test is deliberately top-level only. FORGE writes its artifacts at
    the root of ``output_dir``, so a project file that happens to be called
    ``src/main/resources/migration-report.md`` is the user's and has to land.
    """
    rel = str(rel).replace("\\", "/").lstrip("/")
    if rel == STAGING_DIR or rel.startswith(STAGING_DIR + "/"):
        return True
    if "/" in rel:
        return False
    # `decisions.json` is what the static review page hands the user, and
    # `decisions-applied.jsonl` is the log of what was done with it. Neither is
    # project code. Same rule merged_tree.py already applies.
    if rel.startswith("decisions") and rel.endswith(".json"):
        return True
    # The run manifest: FORGE's bookkeeping, and it once landed as an added file.
    if rel == run_manifest.MANIFEST_NAME:
        return True
    # One report and one acceptance record per pack, and the plan summary.
    from forge.utils.report import is_report_artifact
    if is_report_artifact(rel):
        return True
    return rel in ARTIFACT_NAMES


# Written by the operating system, not by anyone; never landed and not worth
# naming in a result either.
_OS_NOISE = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# How many unrecorded paths a result names; the count is always exact.
SKIPPED_CAP = 50


def recorded_files(output_dir: str) -> set:
    """Relative paths FORGE put in ``output_dir``: what a run wrote, and what a human approved.

    ``.forge-writes.json`` is the run manifest. The applied-decisions log is
    read too because an approval promoted from staging was not recorded in the
    manifest before ``service.apply`` started doing so, and a file a human
    signed off on must not be dropped from a landing for a bookkeeping gap.
    """
    recorded = set(run_manifest.load(str(Path(output_dir).expanduser())))
    try:
        from forge.decisions import APPLIED_LOG
        text = (Path(output_dir).expanduser() / APPLIED_LOG).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for row in _json_objects(text):
        if row.get("decision") == "approve" and row.get("applied"):
            rel = str(row.get("file") or "").replace("\\", "/")
            if rel and not rel.startswith("/") and ".." not in rel.split("/"):
                recorded.add(rel)
    return recorded


def _json_objects(text: str):
    """Every JSON object in a JSONL log, even two run together on one line.

    The real AMS log starts with a test fixture glued to the first approval
    (no newline between them); reading it line by line would drop that approval.
    """
    decoder = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            nxt = text.find("\n", i)
            if nxt < 0:
                return
            i = nxt + 1
            continue
        if isinstance(obj, dict):
            yield obj


def _scan(output_dir: str):
    """``(landable, unrecorded)``: project files in ``output_dir``, split by provenance."""
    root = Path(output_dir).expanduser()
    if not output_dir or not root.is_dir():
        return [], []
    recorded = recorded_files(output_dir)
    landable: List[str] = []
    unrecorded: List[str] = []
    for dirpath, dirs, files in os.walk(root):
        # `.git` would only be here if the output directory were itself a
        # repository; copying it into another one is never right.
        dirs[:] = sorted(d for d in dirs if d not in (STAGING_DIR, ".git"))
        for name in sorted(files):
            rel = str((Path(dirpath) / name).relative_to(root)).replace("\\", "/")
            if is_artifact(rel) or name in _OS_NOISE:
                continue
            (landable if rel in recorded else unrecorded).append(rel)
    return sorted(landable), sorted(unrecorded)


def landable_files(output_dir: str) -> List[str]:
    """Every migrated file in ``output_dir`` that FORGE recorded, relative and sorted.

    Held units under ``.forge-staging/`` are excluded because they are exactly
    the files a human has not approved — landing them would be the hold gate
    with no gate. Files no run recorded are excluded too (:func:`unrecorded_files`).
    """
    return _scan(output_dir)[0]


def unrecorded_files(output_dir: str) -> List[str]:
    """Files in ``output_dir`` that no run wrote and no human approved.

    Landing leaves them behind and reports them. A pytest fixture once sat in
    the real output folder for two days, and a landing that trusted the
    directory would have committed ``com/corp/Other.java`` into the customer's
    repository.
    """
    return _scan(output_dir)[1]


# ─── git, always as a list argv ───────────────────────────────────────────────

def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """One git call. Never ``shell=True``, never ``check=True``.

    A branch name and a commit message come from a model; passed as argv there
    is nothing for them to escape into. ``check=False`` is the house rule for
    this module — a non-zero exit is a message for the user, not a traceback.
    """
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              check=False, timeout=GIT_TIMEOUT_SECONDS)
    except FileNotFoundError as e:
        raise GitUnavailable("git is not installed, or is not on this machine's PATH") from e
    except subprocess.TimeoutExpired as e:
        raise GitUnavailable(f"git {args[0] if args else ''} did not finish within "
                             f"{GIT_TIMEOUT_SECONDS}s") from e


def _stderr(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
    return text[:STDERR_CAP] or f"git exited {proc.returncode}"


def _current_branch(cwd: str) -> str:
    try:
        proc = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    except GitUnavailable:
        return "?"
    return (proc.stdout or "").strip() if proc.returncode == 0 else "?"


def _fail(error: str, state: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "error": error}
    if state:
        out["state"] = state
    return out


# ─── preconditions: four refusals, in order ───────────────────────────────────

def preconditions(source_dir: str, output_dir: str, branch: str) -> Optional[str]:
    """Why this must not land, or None. Each string ends with the user's move."""
    source = str(source_dir or "")
    if not source or not Path(source).is_dir():
        return f"no project set yet, or {source or '(none)'} is not a directory"
    if Path(source).resolve() == Path(output_dir or ".").expanduser().resolve():
        return ("the output directory and the repository are the same folder — there is nothing to "
                "copy, and FORGE will not commit a tree it cannot tell apart from its own output")

    branch = str(branch or "").strip()
    if not branch:
        return f"name the branch to land on ({DEFAULT_BRANCH_HINT})"
    if branch.startswith("-"):
        # git would read it as an option, and a "branch" called --force is not
        # a branch anyone meant to create.
        return f"'{branch}' is not a valid branch name: it must not start with '-'"

    try:
        # 1. a git work tree at all. The suggestion is a suggestion: FORGE does
        #    not create a repository for someone as a side effect of a migration.
        top = _git(source, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return ("there is no branch to land on: run `git init` in the project folder and commit "
                    "the code as it stands, then ask again. FORGE will not run it — the first "
                    f"commit decides what the migration is a diff against. Folder: {source}")
        toplevel = (top.stdout or "").strip() or source

        # 2. a clean work tree. Never stash: what is uncommitted is someone's
        #    unfinished work, and the migration would be indistinguishable from
        #    it in the commit.
        status = _git(source, "status", "--porcelain")
        if status.returncode != 0:
            return f"git could not read the work tree at {toplevel}: {_stderr(status)}"
        dirty = [line for line in (status.stdout or "").splitlines() if line.strip()]
        if dirty:
            shown = ", ".join(line[3:].strip() for line in dirty[:5])
            more = f" and {len(dirty) - 5} more" if len(dirty) > 5 else ""
            hint = ""
            if _inside(output_dir, toplevel):
                hint = (f" ({output_dir} is inside the repository — add it to .gitignore and "
                        "FORGE's own output stops counting as a change)")
            return (f"the work tree at {toplevel} has {len(dirty)} uncommitted change(s): "
                    f"{shown}{more}{hint}. Commit or stash them yourself first — FORGE will not "
                    "touch work it did not do.")

        # 3. a NEW branch with a legal name. An existing one would have to be
        #    checked out over, and that is a history nobody asked to move.
        legal = _git(source, "check-ref-format", "--branch", branch)
        if legal.returncode != 0:
            return f"'{branch}' is not a valid git branch name: {_stderr(legal)}"
        exists = _git(source, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if exists.returncode == 0:
            return (f"branch '{branch}' already exists in {toplevel}. Pick a name that does not — "
                    "FORGE only ever creates a new branch, so nothing you have can be overwritten.")
    except GitUnavailable as e:
        return str(e)

    # 4. something to land. A missing output directory and an output directory
    #    holding only artifacts are different mistakes, so they read differently.
    out = Path(output_dir or "").expanduser()
    if not output_dir or not out.is_dir():
        return f"no output directory at {output_dir or '(none)'} — run a pack first, there is nothing migrated to land"
    landable, unrecorded = _scan(output_dir)
    if not landable:
        extra = ""
        if unrecorded:
            shown = ", ".join(unrecorded[:5]) + (f" and {len(unrecorded) - 5} more" if len(unrecorded) > 5 else "")
            extra = (f" It does hold {len(unrecorded)} file(s) no FORGE run recorded ({shown}); "
                     "those are never landed.")
        return (f"{output_dir} holds no migrated files — only FORGE's own artifacts, and held "
                "units that are still waiting on a human. Run a pack, or settle the held files first."
                + extra)
    return None


def _inside(path: str, parent: str) -> bool:
    """Is ``path`` within ``parent``? Used only to make a refusal more helpful."""
    try:
        return Path(path or "").expanduser().resolve().is_relative_to(Path(parent).resolve())
    except (OSError, ValueError):
        return False


# ─── the commit message ───────────────────────────────────────────────────────

def commit_message(files: int, packs: Sequence[str], output_dir: str,
                   supplied: Optional[str] = None) -> str:
    """The message, with the co-author trailer whoever wrote the subject."""
    text = str(supplied or "").strip()
    if not text:
        through = ", ".join(str(p) for p in packs if p)
        subject = f"Migrate {files} file(s) through {through}" if through else f"Migrate {files} file(s) with FORGE"
        body = ["", "Packs, in the order they ran:"] if packs else []
        body += [f"- {p}" for p in packs if p]
        body += ["", f"Copied from {output_dir} by FORGE. Every held file was left behind; "
                     "nothing was pushed."]
        text = "\n".join([subject] + body)
    text = text[:MESSAGE_CAP].rstrip()
    if CO_AUTHOR_LINE not in text:
        text += "\n\n" + CO_AUTHOR_LINE
    return text


# ─── the land ─────────────────────────────────────────────────────────────────

def land(source_dir: str, output_dir: str, branch: str, *, message: Optional[str] = None,
         packs: Sequence[str] = (), deleted: Iterable[str] = ()) -> Dict[str, Any]:
    """Create ``branch``, copy the migration in, commit. Never push.

    Returns a JSON-safe dict: ``{"ok": False, "error", "state"?}`` or
    ``{"ok": True, "branch", "files_changed", "deleted", "commit", "packs",
    "push_command", "files", "skipped", "skipped_count"}``. ``skipped`` names files
    in ``output_dir`` that were left behind because no run recorded them. Nothing here raises for a git failure — the
    caller is a tool, and a tool failure is an observation (R6).
    """
    branch = str(branch or "").strip()
    refusal = preconditions(source_dir, output_dir, branch)
    if refusal:
        return _fail(refusal)

    source = str(Path(source_dir).expanduser().resolve())
    out_root = Path(output_dir).expanduser()
    files, unrecorded = _scan(output_dir)
    was_on = _current_branch(source)

    # Everything that can be known before the branch exists is checked here,
    # so a landing either happens whole or leaves the repository untouched.
    # `git add` refuses an ignored path outright, and on AMS that refusal
    # came after the checkout and the copy -- a half-landed branch.
    try:
        ignored = _ignored(source, files)
    except RuntimeError as e:   # GitUnavailable included
        return _fail(str(e), f"nothing was copied and no branch was created; the repository is still on {was_on}")
    if ignored:
        files = [f for f in files if f not in ignored]
        if not files:
            return _fail(f"every migrated file is ignored by the repository's .gitignore "
                         f"({', '.join(sorted(ignored)[:5])}{' …' if len(ignored) > 5 else ''})",
                         f"nothing was copied and no branch was created; the repository is still on {was_on}")

    try:
        made = _git(source, "checkout", "-b", branch)
        if made.returncode != 0:
            return _fail(f"could not create branch '{branch}': {_stderr(made)}",
                         f"nothing was copied and no branch was created; the repository is still on {was_on}")

        # From here on the repository is on the new branch. Every failure below
        # says so rather than trying to undo it: a half-reversed checkout is a
        # second problem on top of the first, and the user can always
        # `git switch -` themselves.
        on_branch = f"the repository is on the new branch '{branch}'"

        copied: List[str] = []
        rel = ""
        try:
            for rel in files:
                target = Path(source) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(out_root / rel, target)
                copied.append(rel)
        except OSError as e:
            return _fail(f"could not copy {rel}: {e}",
                         f"{on_branch} with {len(copied)} of {len(files)} file(s) copied into the "
                         "work tree and nothing staged or committed")

        removed = _remove_superseded(source, deleted)
        staged = copied + removed
        for start in range(0, len(staged), ADD_BATCH):
            chunk = staged[start:start + ADD_BATCH]
            added = _git(source, "add", "--", *chunk)
            if added.returncode != 0:
                return _fail(f"git add failed: {_stderr(added)}",
                             f"{on_branch} with the migrated files copied in and partially staged")

        staged = _staged_count(source)
        if staged == 0:
            return _fail("nothing to commit: every migrated file is already identical to what is "
                         "in the repository",
                         f"{on_branch} with no commit made; `git switch -` returns you to {was_on}")
        # A file the migration left byte-identical is not a change, so the count
        # comes from what git staged rather than from what was copied — and the
        # removals are reported separately, not folded in.
        changed = len(copied) if staged < 0 else max(staged - len(removed), 0)

        text = commit_message(changed, list(packs), str(out_root), message)
        committed = _git(source, "commit", "-m", text)
        if committed.returncode != 0:
            return _fail(f"git commit failed: {_stderr(committed)}",
                         f"{on_branch} with {len(staged)} path(s) staged and not committed")

        sha = _git(source, "rev-parse", "--short", "HEAD")
        return {
            "ok": True,
            "branch": branch,
            "files_changed": changed,
            "deleted": len(removed),
            "commit": (sha.stdout or "").strip() if sha.returncode == 0 else "",
            "packs": [str(p) for p in packs if p],
            # The user's to run, deliberately. A tool that pushes has published
            # someone's code to a place other people read.
            "push_command": PUSH_COMMAND.format(branch=branch),
            "files": copied,
            "deleted_files": removed,
            "source_dir": source,
            # Not FORGE's to commit, so not committed -- and said, so the user
            # can see what is sitting in the output directory.
            "skipped": unrecorded[:SKIPPED_CAP],
            "skipped_count": len(unrecorded),
            # Recorded, but the repository's .gitignore excludes them; git add
            # would refuse them, so they were never copied.
            "ignored": sorted(ignored)[:SKIPPED_CAP],
            "ignored_count": len(ignored),
        }
    except GitUnavailable as e:
        return _fail(str(e), f"the repository may be on branch '{branch}'; check with `git status`")


def _ignored(source: str, files: Sequence[str]) -> set:
    """The paths among ``files`` the repository's ignore rules exclude.

    Asked of git itself (``check-ignore``), so every .gitignore, the global
    excludes file and ``.git/info/exclude`` all count. A tracked file is never
    reported -- git adds those whatever the rules say. Raises RuntimeError
    when git cannot answer, which the caller turns into a refusal before the
    branch exists.
    """
    found: set = set()
    for start in range(0, len(files), ADD_BATCH):
        chunk = list(files[start:start + ADD_BATCH])
        proc = _git(source, "check-ignore", "--", *chunk)
        if proc.returncode == 0:
            found |= {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}
        elif proc.returncode != 1:     # 1 is "none of these is ignored"
            raise RuntimeError(f"git could not check the ignore rules: {_stderr(proc)}")
    return found


def _remove_superseded(source: str, deleted: Iterable[str]) -> List[str]:
    """Delete the paths the migration retired — only tracked ones, only inside.

    ``deleted_files`` is model-authored: it comes back in the transform
    response (``agents/java_upgrade.py:86``) as "the XML config this
    @Configuration class replaces". Deleting a path a model named inside
    somebody's repository needs two bounds, so this one takes both — the
    resolved path must be inside the work tree, and git must already track it.
    Anything else is skipped silently rather than refused: a stale entry for a
    file that is already gone is normal, not an error.
    """
    candidates: List[str] = []
    root = Path(source).resolve()
    for raw in deleted or ():
        rel = str(raw).replace("\\", "/").lstrip("/")
        if not rel or rel.startswith(".."):
            continue
        try:
            target = (root / rel).resolve()
        except OSError:
            continue
        if not target.is_relative_to(root) or not target.is_file():
            continue
        candidates.append(str(target.relative_to(root)).replace("\\", "/"))
    if not candidates:
        return []

    tracked: List[str] = []
    for start in range(0, len(candidates), ADD_BATCH):
        chunk = candidates[start:start + ADD_BATCH]
        listed = _git(source, "ls-files", "--", *chunk)
        if listed.returncode != 0:
            continue
        tracked += [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()]

    removed: List[str] = []
    for rel in sorted(set(tracked)):
        try:
            (root / rel).unlink()
        except OSError:
            continue
        removed.append(rel)
    return removed


def _staged_count(source: str) -> int:
    """How many paths are staged, or -1 when git will not say.

    An unborn HEAD (a repository with no commit yet) makes ``git diff --cached``
    refuse on some versions; that is not a reason to fail a land, so an
    unreadable answer means "let the commit decide".
    """
    try:
        proc = _git(source, "diff", "--cached", "--name-only")
    except GitUnavailable:
        return -1
    if proc.returncode != 0:
        return -1
    return len([line for line in (proc.stdout or "").splitlines() if line.strip()])
