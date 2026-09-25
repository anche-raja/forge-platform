"""Build the migrated project, the way its own build would.

A compile is the one check that catches every file a pack missed and every
bad edit a reviewer let through, whichever pack caused it. The pack-level
``build`` acceptance check runs one command at the repository root, which
cannot build a project made of several Maven reactors built in order -- AMS
is three (a parent BOM, a shared library, the application) and has no root
pom at all. This module works out the project's own build instead:

- ``project_build.command`` in agents.yaml, when set, is run verbatim at the
  tree root. That covers Gradle, a ``build.sh``, anything custom -- including
  the project's own build script: a relative executable that exists in the
  tree is run from the tree, ``{maven_repo}`` in the command becomes the
  isolated repository below, and ``project_build.env`` adds variables the
  script reads.
- Otherwise every Maven reactor -- a ``pom.xml`` that is not a ``<module>`` of
  another pom -- is built with ``mvn install``, a reactor after any reactor
  whose artifacts it inherits from or depends on.

``install`` goes to an isolated local repository (``~/.forge/m2`` by
default), so the migrated snapshots never replace the original project's
artifacts in ``~/.m2``. The JDK is ``project_build.java_home``, else the one
``/usr/libexec/java_home`` reports for ``target_java_version``, else the
current ``JAVA_HOME``.

Nothing here decides anything: the result is pass, fail or skip, with the
failing step and the tail of its output, for a human to read.
"""

import hashlib
import os
import shlex
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from forge.utils.fs import prune_dirs

DEFAULT_TIMEOUT = 1200
DEFAULT_MAVEN_REPO = "~/.forge/m2"
TAIL_LINES = 40


@dataclass
class Step:
    label: str
    argv: List[str]
    cwd: str
    returncode: Optional[int] = None
    seconds: float = 0.0


@dataclass
class BuildResult:
    outcome: str                      # pass | fail | skip
    detail: str
    steps: List[Step] = field(default_factory=list)
    failed_step: Optional[str] = None
    tail: List[str] = field(default_factory=list)
    java_home: Optional[str] = None
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ─── settings ─────────────────────────────────────────────────────────────────

def settings(config) -> dict:
    raw = (config.get("project_build") if config is not None else None) or {}
    try:
        timeout = int(raw.get("timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    env = raw.get("env") or {}
    return {
        "command": str(raw.get("command") or "").strip(),
        "java_home": str(raw.get("java_home") or "").strip(),
        "maven_repo": str(raw.get("maven_repo") or DEFAULT_MAVEN_REPO).strip(),
        "timeout": max(60, timeout),
        "env": {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {},
    }


def split_command(command: str, *, posix: Optional[bool] = None) -> List[str]:
    """``project_build.command`` as argv.

    Shell quoting everywhere, but on Windows a backslash is a path separator,
    not an escape: POSIX splitting turns ``C:\\tools\\build.cmd`` into
    ``C:toolsbuild.cmd``. Quotes still group a path with spaces on both.
    """
    posix = os.name != "nt" if posix is None else posix
    argv = shlex.split(command, posix=posix)
    if not posix:
        argv = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a for a in argv]
    return argv


def resolve_java_home(config, *, run=subprocess.run) -> Optional[str]:
    """``project_build.java_home``, else the JDK for ``target_java_version``, else ``$JAVA_HOME``."""
    configured = settings(config)["java_home"]
    if configured:
        return str(Path(configured).expanduser())
    version = str((config.get("target_java_version") if config is not None else None) or "").strip()
    if version and Path("/usr/libexec/java_home").exists():
        try:
            proc = run(["/usr/libexec/java_home", "-v", version], capture_output=True, text=True, timeout=10)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return os.environ.get("JAVA_HOME") or None


# ─── Maven reactors ───────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name) -> str:
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None and c.text else ""


@dataclass
class _Pom:
    path: Path
    artifact: str
    modules: List[Path]
    uses: List[str]                    # artifactIds referenced as parent or dependency


def _read_pom(path: Path) -> Optional[_Pom]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    modules: List[Path] = []
    mods = _child(root, "modules")
    if mods is not None:
        for m in mods:
            if _local(m.tag) == "module" and m.text and m.text.strip():
                target = (path.parent / m.text.strip()).resolve()
                modules.append(target / "pom.xml" if target.is_dir() or not target.suffix else target)
    uses: List[str] = []
    parent = _child(root, "parent")
    if parent is not None and _text(parent, "artifactId"):
        uses.append(_text(parent, "artifactId"))
    # Real dependencies only. A BOM's <dependencyManagement> lists the very
    # modules that inherit from it -- AMS's parent BOM manages all of them --
    # so counting those would order the BOM after its own children. Only an
    # imported BOM (scope import) there is a build-order edge.
    deps = _child(root, "dependencies")
    for el in (deps if deps is not None else []):
        if _local(el.tag) == "dependency" and _text(el, "artifactId"):
            uses.append(_text(el, "artifactId"))
    managed = _child(root, "dependencyManagement")
    dm = _child(managed, "dependencies") if managed is not None else None
    for el in (dm if dm is not None else []):
        if _local(el.tag) == "dependency" and _text(el, "scope") == "import" and _text(el, "artifactId"):
            uses.append(_text(el, "artifactId"))
    return _Pom(path=path.resolve(), artifact=_text(root, "artifactId"), modules=modules, uses=uses)


def find_reactors(root: str) -> List[Path]:
    """Top-level poms under ``root``, in build order."""
    base = Path(root).resolve()
    poms: Dict[Path, _Pom] = {}
    for dirpath, dirs, files in os.walk(base):
        # FORGE's own output is never a reactor: a `.migrated` inside the
        # repository holds a copy of the very poms being built.
        prune_dirs(dirpath, dirs)
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if "pom.xml" in files:
            pom = _read_pom(Path(dirpath) / "pom.xml")
            if pom is not None:
                poms[pom.path] = pom
    children = {m for p in poms.values() for m in p.modules}
    tops = sorted(p for p in poms if p not in children)

    def descendants(path: Path, seen=None) -> List[_Pom]:
        seen = seen if seen is not None else set()
        if path in seen or path not in poms:
            return []
        seen.add(path)
        out = [poms[path]]
        for m in poms[path].modules:
            out += descendants(m, seen)
        return out

    produces = {t: {p.artifact for p in descendants(t)} for t in tops}
    needs = {t: {u for p in descendants(t) for u in p.uses} - produces[t] for t in tops}
    after = {t: {o for o in tops if o != t and needs[t] & produces[o]} for t in tops}

    ordered: List[Path] = []
    remaining = list(tops)
    while remaining:
        ready = [t for t in remaining if after[t] <= set(ordered)]
        if not ready:                  # a cycle: fall back to path order for the rest
            ready = remaining[:1]
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


# ─── running it ───────────────────────────────────────────────────────────────

def plan(root: str, config) -> List[Step]:
    """The steps that build the tree at ``root``; empty when nothing is buildable."""
    s = settings(config)
    base = Path(root).resolve()
    repo = str(Path(s["maven_repo"]).expanduser())
    if s["command"]:
        argv = [a.replace("{maven_repo}", repo) for a in split_command(s["command"])]
        # The project's own script (build.cmd, bin/build.sh) lives in the tree
        # being built, not wherever FORGE was started from.
        if argv and not Path(argv[0]).is_absolute() and (base / argv[0]).is_file():
            argv[0] = str(base / argv[0])
        return [Step(label=s["command"], argv=argv, cwd=str(base))]
    return [
        Step(label=pom.relative_to(base).as_posix(),
             argv=["mvn", "-q", "-B", "-DskipTests", f"-Dmaven.repo.local={repo}", "install", "-f", str(pom)],
             cwd=str(base))
        for pom in find_reactors(str(base))
    ]


def run(root: str, config, *, runner=subprocess.run) -> BuildResult:
    """Build the tree at ``root``. Stops at the first failing step."""
    s = settings(config)
    try:
        steps = plan(root, config)
    except ValueError as e:            # an unparseable project_build.command
        return BuildResult("fail", f"project_build.command cannot be parsed: {e}")
    if not steps:
        return BuildResult("skip", "nothing to build: no pom.xml found. Set project_build.command in agents.yaml")
    tool = steps[0].argv[0]
    if shutil.which(tool) is None and not Path(tool).exists():
        return BuildResult("skip", f"'{tool}' is not on PATH", steps=steps)

    java_home = resolve_java_home(config)
    env = dict(os.environ)
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = f"{java_home}/bin{os.pathsep}{env.get('PATH', '')}"
    env.update(s["env"])

    started = time.monotonic()
    for step in steps:
        left = s["timeout"] - (time.monotonic() - started)
        t0 = time.monotonic()
        try:
            proc = runner(step.argv, cwd=step.cwd, env=env, capture_output=True, text=True, timeout=max(1, left))
        except subprocess.TimeoutExpired:
            step.seconds = round(time.monotonic() - t0, 1)
            return BuildResult("fail", f"timed out after {s['timeout']}s building {step.label}", steps=steps,
                               failed_step=step.label, java_home=java_home,
                               seconds=round(time.monotonic() - started, 1))
        step.returncode = proc.returncode
        step.seconds = round(time.monotonic() - t0, 1)
        if proc.returncode != 0:
            # The build ran in a temporary copy; show paths relative to the project.
            prefix = str(Path(root).resolve()) + os.sep
            text = ((proc.stdout or "") + (proc.stderr or "")).replace(prefix, "")
            tail = [ln for ln in text.strip().splitlines() if ln.strip() not in ("[ERROR]", "")][-TAIL_LINES:]
            return BuildResult("fail", f"{step.label} failed (exit {proc.returncode})", steps=steps,
                               failed_step=step.label, tail=tail, java_home=java_home,
                               seconds=round(time.monotonic() - started, 1))
    return BuildResult("pass", f"{len(steps)} step(s) built", steps=steps, java_home=java_home,
                       seconds=round(time.monotonic() - started, 1))


# ─── staleness ────────────────────────────────────────────────────────────────

def output_fingerprint(source_dir: str, output_dir: str) -> str:
    """A digest of the migrated files in ``output_dir``, not FORGE's own reports.

    Project files sit in subdirectories, or at the root only when the source
    has the same file (a root pom.xml). FORGE's artifacts are root-level files
    the source does not have, and held units live under .forge-staging/.
    """
    out, src = Path(output_dir), Path(source_dir)
    h = hashlib.sha256()
    if not out.is_dir():
        return h.hexdigest()
    for dirpath, dirs, files in os.walk(out):
        dirs[:] = sorted(d for d in dirs if d != ".forge-staging")
        for f in sorted(files):
            p = Path(dirpath) / f
            rel = str(p.relative_to(out)).replace("\\", "/")
            if "/" not in rel and not (src / rel).is_file():
                continue
            st = p.stat()
            h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def summarize(steps: Sequence[Step]) -> List[str]:
    return [f"{st.label}: {'ok' if st.returncode == 0 else ('exit ' + str(st.returncode)) if st.returncode is not None else 'not run'}"
            for st in steps]
