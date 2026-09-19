"""Executing a generated test — the second verification gate.

The compile gate proves the migrated code still builds. Running the generated
tests is the only gate that says anything about what it *does*, so a failure
here is the most valuable signal the pipeline produces — and the most dangerous
to act on blindly. Two rules follow from that:

* a test that fails is never left in the output tree. It is staged, reported
  with its output, and the tree keeps building;
* a missing toolchain is SKIPPED, never FAIL. A machine without Maven is an
  environment gap, not a bad test — the same rule as build verification.

The tests run against the *merged* tree (source with output overlaid),
materialised once per run into a temporary directory, because ``./migrated``
holds only the files the migration wrote and nothing compiles from that alone.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence

from forge.testgen.settings import TestGenSettings
from forge.testgen.targets import TestTarget
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"

_MAX_OUTPUT_CHARS = 6000


class TestRunner:
    """Runs one generated test class in a materialised copy of the merged tree."""

    def __init__(self, config, settings: Optional[TestGenSettings], source_dir: str, output_dir: str,
                 deleted: Sequence[str] = ()):
        self.settings = settings or TestGenSettings.from_config(config)
        self.run_settings = self.settings.run
        self.source_dir = source_dir
        self.output_dir = output_dir
        self.deleted = tuple(deleted)
        self._workspace: Optional[Path] = None
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._unavailable: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.run_settings.enabled)

    # ─── workspace ───────────────────────────────────────────────────────────

    def _executable(self) -> str:
        if self.run_settings.mode == "gradle":
            return "gradle"
        if self.run_settings.mode == "command":
            return (self.run_settings.command.split() or [""])[0]
        return "mvn"

    def _prepare(self) -> Optional[str]:
        """Materialise the merged tree once. Returns a reason when it cannot run."""
        if self._unavailable is not None:
            return self._unavailable
        if self._workspace is not None:
            return None

        executable = self._executable()
        if executable and shutil.which(executable) is None:
            self._unavailable = f"{executable} not found on PATH"
            _log.warning("Generated tests will not be run: %s", self._unavailable)
            return self._unavailable

        from forge.verify.merged_tree import MergedTree

        self._tmp = tempfile.TemporaryDirectory(prefix="forge-testrun-")
        workspace = MergedTree(self.source_dir, self.output_dir, self.deleted).materialize(self._tmp.name)
        if self.run_settings.mode == "maven" and not (workspace / "pom.xml").is_file():
            self._unavailable = "no pom.xml at the project root — set test_generation.run_tests.mode or .command"
            return self._unavailable
        self._workspace = workspace
        _log.info("Test workspace: %s", workspace)
        return None

    def _command(self, target: TestTarget, test_files: Sequence[str]) -> List[str]:
        workspace = str(self._workspace)
        if self.run_settings.mode == "gradle":
            return ["gradle", "-p", workspace, "test", "--tests", target.test_fqcn]
        if self.run_settings.mode == "command":
            rendered = self.run_settings.command.format(
                test_class=target.test_class, test_fqcn=target.test_fqcn,
                workspace=workspace, test_file=" ".join(test_files),
            )
            return rendered.split()
        return ["mvn", "-q", "-B", "-f", workspace, "test",
                f"-Dtest={target.test_class}", "-DfailIfNoTests=false", "-Dsurefire.failIfNoSpecifiedTests=false"]

    # ─── running ─────────────────────────────────────────────────────────────

    def run(self, target: TestTarget, written_paths: Sequence[str]) -> dict:
        if not self.enabled:
            return {"verdict": SKIPPED, "output": "test_generation.run_tests.enabled is false", "command": ""}
        if not written_paths:
            return {"verdict": SKIPPED, "output": "nothing was written to run", "command": ""}

        reason = self._prepare()
        if reason is not None:
            return {"verdict": SKIPPED, "output": reason, "command": ""}

        copied = self._copy_in(written_paths)
        cmd = self._command(target, copied)
        printable = " ".join(cmd)
        _log.info("Running generated test: %s", printable)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.run_settings.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            return {"verdict": FAIL, "command": printable,
                    "output": f"The generated test timed out after {self.run_settings.timeout_seconds}s — "
                              "a unit test that hangs is a failed test."}
        except OSError as e:
            return {"verdict": SKIPPED, "output": f"Could not run the test: {e}", "command": printable}
        finally:
            self._remove(copied)

        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        verdict = PASS if proc.returncode == 0 else FAIL
        if verdict == FAIL:
            _log.info("Generated test FAILED for %s (exit %s)", target.test_class, proc.returncode)
        return {"verdict": verdict, "output": combined[:_MAX_OUTPUT_CHARS], "command": printable}

    def _copy_in(self, written_paths: Sequence[str]) -> List[str]:
        """Copy this unit's test files into the workspace, at their own relative paths."""
        root = Path(self.output_dir).resolve()
        copied: List[str] = []
        for p in written_paths:
            src = Path(p)
            if not src.is_file():
                continue
            try:
                rel = src.resolve().relative_to(root)
            except ValueError:
                continue
            dest = self._workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(str(dest))
        return copied

    def _remove(self, copied: Sequence[str]) -> None:
        """Take the test back out again, so the next unit runs against a clean tree."""
        for p in copied:
            try:
                Path(p).unlink()
            except OSError:
                pass

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None
            self._workspace = None
