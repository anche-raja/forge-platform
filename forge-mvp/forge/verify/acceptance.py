"""Run a pack's acceptance checks over the post-migration view of a project.

File scores gate files; acceptance gates the project. A run in which every
file scored 95 and ``routing_parity`` failed has lost endpoints, and this is
where that is said in those words rather than reported as success.

Every check is mechanical — no model is involved — and every outcome is one
of three things: passed, failed with the evidence, or **skipped with the
reason**. A check the runner cannot perform (its extractor is not built, the
decision it depends on is unset, the build tool is absent) is never counted
as a pass.
"""

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from forge.context.snapshot import SNAPSHOT_NAME
from forge.packs.spec import AcceptanceCheck, PackSpec
from forge.utils.telemetry import get_logger
from forge.verify.merged_tree import MergedTree

_log = get_logger(__name__)

ACCEPTANCE_NAME = "migration-acceptance.json"
_TEST_METHOD = re.compile(r"@(?:org\.junit(?:\.jupiter\.api)?\.)?(?:Test|ParameterizedTest|RepeatedTest)\b")
_DISABLED = re.compile(r"@(?:org\.junit\.jupiter\.api\.)?Disabled\b|@(?:org\.junit\.)?Ignore\b")


@dataclass
class CheckResult:
    pack: str
    kind: str
    value: object
    scope: str
    outcome: str            # "pass" | "fail" | "skip"
    detail: str
    evidence: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @property
    def failed(self) -> bool:
        return self.outcome == "fail"


@dataclass
class AcceptanceReport:
    source_dir: str
    output_dir: Optional[str]
    results: List[CheckResult]

    @property
    def failed(self) -> List[CheckResult]:
        return [r for r in self.results if r.failed]

    @property
    def skipped(self) -> List[CheckResult]:
        return [r for r in self.results if r.outcome == "skip"]

    @property
    def verdict(self) -> str:
        """``PASS`` only when every check ran and passed. Skips make it ``INCOMPLETE``."""
        if self.failed:
            return "FAIL"
        if self.skipped or not self.results:
            return "INCOMPLETE"
        return "PASS"

    def to_json(self) -> str:
        return json.dumps({
            "verdict": self.verdict,
            "source_dir": self.source_dir,
            "output_dir": self.output_dir,
            "results": [asdict(r) for r in self.results],
        }, indent=2, default=str)

    def to_markdown(self) -> str:
        lines = ["## Acceptance", "", f"**Verdict: {self.verdict}** — "
                 f"{sum(r.passed for r in self.results)} passed, {len(self.failed)} failed, "
                 f"{len(self.skipped)} skipped", ""]
        if self.verdict != "PASS":
            lines.append("A skipped check is not a pass. The project has migrated only when this reads PASS.")
            lines.append("")
        lines += ["| Pack | Check | Scope | Outcome | Detail |", "|---|---|---|---|---|"]
        for r in self.results:
            value = r.value if isinstance(r.value, str) else ""
            check = f"`{r.kind}`" + (f" `{value[:40]}{'…' if len(value) > 40 else ''}`" if value else "")
            lines.append(f"| {r.pack} | {check} | `{r.scope or '—'}` | **{r.outcome.upper()}** | {r.detail} |")
        for r in self.results:
            if r.evidence:
                lines += ["", f"<details><summary>{r.pack} · {r.kind} — evidence ({len(r.evidence)})</summary>", ""]
                lines += [f"- `{e}`" for e in r.evidence[:50]]
                if len(r.evidence) > 50:
                    lines.append(f"- … {len(r.evidence) - 50} more")
                lines += ["", "</details>"]
        return "\n".join(lines) + "\n"


# ─── runner ───────────────────────────────────────────────────────────────────

def run_acceptance(
    packs: Sequence[PackSpec],
    source_dir: str,
    output_dir: Optional[str],
    decisions: Mapping[str, str],
    *,
    deleted: Sequence[str] = (),
    run_build: bool = False,
    build_timeout: int = 900,
) -> AcceptanceReport:
    tree = MergedTree(source_dir, output_dir, deleted)
    snapshot = _load_snapshot(output_dir)
    results: List[CheckResult] = []
    materialized: Optional[Path] = None
    scratch: Optional[tempfile.TemporaryDirectory] = None

    def tree_on_disk() -> Path:
        nonlocal materialized, scratch
        if materialized is None:
            scratch = tempfile.TemporaryDirectory(prefix="forge-acceptance-")
            materialized = tree.materialize(scratch.name)
        return materialized

    try:
        for pack in packs:
            for check in pack.acceptance:
                results.append(_run_check(pack, check, tree, snapshot, decisions, tree_on_disk,
                                          run_build=run_build, build_timeout=build_timeout))
    finally:
        if scratch is not None:
            scratch.cleanup()
    return AcceptanceReport(source_dir=str(Path(source_dir).resolve()), output_dir=output_dir, results=results)


def _load_snapshot(output_dir: Optional[str]) -> Optional[dict]:
    if not output_dir:
        return None
    path = Path(output_dir) / SNAPSHOT_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _log.warning("Could not read %s: %s", path, e)
        return None


def _run_check(pack: PackSpec, check: AcceptanceCheck, tree: MergedTree, snapshot: Optional[dict],
               decisions: Mapping[str, str], tree_on_disk, *, run_build: bool, build_timeout: int) -> CheckResult:
    base = dict(pack=pack.id, kind=check.kind, value=check.value, scope=check.scope)

    unset = [k for k, _ in check.when if k not in decisions]
    if unset:
        return CheckResult(**base, outcome="skip", detail=f"decision '{unset[0]}' is not set")
    if not check.applies(decisions):
        cond = ", ".join(f"{k}={v}" for k, v in check.when)
        return CheckResult(**base, outcome="skip", detail=f"not applicable: requires {cond}")

    if check.kind == "no_match":
        return _no_match(base, check, tree)
    if check.kind == "count_unchanged":
        return _count_unchanged(base, check, tree)
    if check.kind == "test_parity":
        return _test_parity(base, tree)
    if check.kind == "authz_parity":
        return _authz_parity(base, tree, snapshot, tree_on_disk)
    if check.kind == "routing_parity":
        return CheckResult(**base, outcome="skip",
                           detail="requires the struts_routing_table extractor, which is not built")
    if check.kind == "build":
        return _build(base, check, tree_on_disk, run_build=run_build, timeout=build_timeout)
    return CheckResult(**base, outcome="skip", detail=f"unknown check kind '{check.kind}'")


# ─── kinds ────────────────────────────────────────────────────────────────────

def _no_match(base: dict, check: AcceptanceCheck, tree: MergedTree) -> CheckResult:
    pattern = re.compile(str(check.value), re.MULTILINE)
    hits = []
    for rel, text in tree.iter_text(check.scope):
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            hits.append(f"{rel}:{line}: {m.group(0)[:80]}")
    if hits:
        return CheckResult(**base, outcome="fail", detail=f"{len(hits)} match(es) remain", evidence=hits)
    return CheckResult(**base, outcome="pass", detail="no matches in the post-migration tree")


def _count_unchanged(base: dict, check: AcceptanceCheck, tree: MergedTree) -> CheckResult:
    pattern = re.compile(str(check.value), re.MULTILINE)
    before = sum(len(pattern.findall(t)) for _, t in tree.iter_text(check.scope, side="source"))
    after_hits: Dict[str, int] = {}
    for rel, text in tree.iter_text(check.scope):
        n = len(pattern.findall(text))
        if n:
            after_hits[rel] = n
    after = sum(after_hits.values())
    if before == after:
        return CheckResult(**base, outcome="pass", detail=f"{before} before, {after} after")
    detail = f"{before} before, {after} after — " + (
        "a JDK package was probably rewritten" if after < before else "new matches appeared")
    return CheckResult(**base, outcome="fail", detail=detail,
                       evidence=[f"{rel}: {n}" for rel, n in sorted(after_hits.items())])


def _test_parity(base: dict, tree: MergedTree) -> CheckResult:
    """Same number of test methods, and any reduction is explained by @Disabled."""
    scope = "**/src/test/**/*.java"
    before = {rel: len(_TEST_METHOD.findall(t)) for rel, t in tree.iter_text(scope, side="source")}
    after = {rel: (len(_TEST_METHOD.findall(t)), len(_DISABLED.findall(t)))
             for rel, t in tree.iter_text(scope)}
    problems = []
    for rel, n_before in sorted(before.items()):
        n_after, disabled = after.get(rel, (0, 0))
        if n_after < n_before and disabled < (n_before - n_after):
            problems.append(f"{rel}: {n_before} tests before, {n_after} after, {disabled} @Disabled")
        elif rel not in after:
            problems.append(f"{rel}: test file missing after migration")
    total_before = sum(before.values())
    total_after = sum(n for n, _ in after.values())
    if problems:
        return CheckResult(**base, outcome="fail",
                           detail=f"{total_before} test methods before, {total_after} after; tests lost without @Disabled",
                           evidence=problems)
    if not before:
        return CheckResult(**base, outcome="skip", detail="no test sources found")
    return CheckResult(**base, outcome="pass", detail=f"{total_before} test methods before, {total_after} after")


def _authz_parity(base: dict, tree: MergedTree, snapshot: Optional[dict], tree_on_disk) -> CheckResult:
    """The authorization rules and the filter chain, before vs after, per module."""
    if not snapshot or snapshot.get("context") != "web_bootstrap":
        return CheckResult(**base, outcome="skip",
                           detail=f"no {SNAPSHOT_NAME} with a web_bootstrap context in the output directory")
    from forge.extract import clear_context_cache, get_context

    root = tree_on_disk()
    clear_context_cache()
    diffs = []
    for module_rel, before in snapshot.get("modules", {}).items():
        module_dir = root if module_rel in (".", "") else root / module_rel
        try:
            after = get_context("web_bootstrap", str(root), str(module_dir)).data
        except ValueError as e:
            diffs.append(f"{module_rel}: post-migration descriptors unreadable: {e}")
            continue
        for key, label in (("authz", "authorization rules"), ("filter_chain", "filter chain")):
            pre = _normalise(before.get(key))
            post = _normalise(after.get(key))
            if pre != post:
                diffs.append(f"{module_rel}: {label} changed")
                diffs.extend(_describe_diff(pre, post, label))
    clear_context_cache()
    if diffs:
        return CheckResult(**base, outcome="fail",
                           detail="authorization rules or filter chain differ from the pre-migration snapshot",
                           evidence=diffs)
    return CheckResult(**base, outcome="pass",
                       detail=f"authorization rules and filter chain identical across {len(snapshot.get('modules', {}))} module(s)")


def _normalise(value):
    """Drop declaration indexes: order is checked by sequence, not by absolute position."""
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items() if k != "order"}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _describe_diff(pre, post, label: str) -> List[str]:
    if isinstance(pre, list) and isinstance(post, list):
        pre_s = [json.dumps(x, sort_keys=True) for x in pre]
        post_s = [json.dumps(x, sort_keys=True) for x in post]
        # Wide enough that a security constraint's URL patterns survive: the
        # evidence has to say *what* was removed, not just that something was.
        out = [f"  - removed: {x[:400]}" for x in pre_s if x not in post_s]
        out += [f"  + added: {x[:400]}" for x in post_s if x not in pre_s]
        if not out and pre_s != post_s:
            out.append(f"  ~ {label}: same entries, different order")
        return out
    if isinstance(pre, dict) and isinstance(post, dict):
        out = []
        for k in sorted(set(pre) | set(post)):
            if pre.get(k) != post.get(k):
                out.extend(_describe_diff(pre.get(k), post.get(k), f"{label}.{k}") or [f"  ~ {label}.{k} changed"])
        return out
    return [f"  ~ {label}: {json.dumps(pre)[:80]} → {json.dumps(post)[:80]}"]


def _build(base: dict, check: AcceptanceCheck, tree_on_disk, *, run_build: bool, timeout: int) -> CheckResult:
    command = str(check.value)
    if not run_build:
        return CheckResult(**base, outcome="skip", detail="build checks run only with --acceptance-build")
    # shlex, not str.split: a build command with a quoted argument
    # (`mvn -Dx="a b"`) must reach the tool as one argument.
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return CheckResult(**base, outcome="fail", detail=f"unparseable build command: {e}")
    if not argv or shutil.which(argv[0]) is None:
        return CheckResult(**base, outcome="skip", detail=f"'{argv[0] if argv else command}' not found on PATH")
    root = tree_on_disk()
    try:
        proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(**base, outcome="fail", detail=f"timed out after {timeout}s")
    if proc.returncode == 0:
        return CheckResult(**base, outcome="pass", detail=f"`{command}` succeeded")
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-30:]
    return CheckResult(**base, outcome="fail", detail=f"`{command}` exited {proc.returncode}", evidence=tail)


# ─── persistence ──────────────────────────────────────────────────────────────

def write_acceptance(report: AcceptanceReport, output_dir: str) -> Path:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / ACCEPTANCE_NAME).write_text(report.to_json(), encoding="utf-8")
    md = out / "migration-report.md"
    if md.is_file():
        md.write_text(md.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + report.to_markdown(), encoding="utf-8")
    return out / ACCEPTANCE_NAME
