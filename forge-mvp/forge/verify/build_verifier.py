"""Compile-level verification of transformed output.

The reviewer scores migrated code on rules and readability, but nothing proved
it compiled — a file could score 95 and still not build. This closes that gap:
a failed compile is fed back to the transform agent as review feedback and
consumes a retry, exactly like a low review score.

Disabled by default. `javac` on a single file only succeeds when the project's
dependencies are on the classpath, so configure `build_verification.classpath`
(or use maven mode against a real pom.xml) before enabling it.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

from forge.config import ForgeConfig
from forge.state import ForgeState
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"

# Compiler output can be enormous; keep enough to act on without bloating the
# retry prompt or the DynamoDB record.
_MAX_OUTPUT_CHARS = 4000


class BuildVerifier:
    def __init__(self, config: ForgeConfig):
        settings = config.get("build_verification") or {}
        self.enabled = bool(settings.get("enabled", False))
        self.mode = (settings.get("mode") or "javac").lower()
        self.command = settings.get("command") or ""
        self.classpath = settings.get("classpath") or ""
        self.timeout = int(settings.get("timeout_seconds", 300))

    # ─── command construction ────────────────────────────────────────────────

    def _build_command(self, written: List[str], output_dir: str, workdir: str) -> List[str]:
        if self.mode == "maven":
            return ["mvn", "-q", "-B", "compile", "-f", output_dir]
        if self.mode == "command":
            rendered = self.command.format(file=" ".join(written), output_dir=output_dir)
            return rendered.split()
        # javac: compile the written files into a throwaway class output dir
        cmd = ["javac", "-proc:none", "-nowarn", "-d", workdir]
        if self.classpath:
            cmd += ["-cp", self.classpath]
        return cmd + written

    # ─── entry point ─────────────────────────────────────────────────────────

    def verify(self, state: ForgeState) -> dict:
        """Run the configured build. Returns {verdict, output, command}."""
        if not self.enabled:
            return {"verdict": SKIPPED, "output": "build_verification.enabled is false", "command": ""}
        if state.get("dry_run"):
            return {"verdict": SKIPPED, "output": "dry run — nothing written to compile", "command": ""}

        written = list(state["current_file"].get("written_paths") or [])
        if not written and self.mode != "maven":
            return {"verdict": SKIPPED, "output": "no files were written", "command": ""}

        executable = {"maven": "mvn", "command": (self.command.split() or [""])[0]}.get(self.mode, "javac")
        if executable and shutil.which(executable) is None:
            # A missing toolchain is an environment problem, not a bad migration.
            _log.warning("%s not found on PATH — skipping build verification", executable)
            return {"verdict": SKIPPED, "output": f"{executable} not found on PATH", "command": ""}

        with tempfile.TemporaryDirectory(prefix="forge-build-") as workdir:
            cmd = self._build_command(written, state["output_dir"], workdir)
            printable = " ".join(cmd)
            _log.info("Build verification: %s", printable)
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout, check=False,
                )
            except subprocess.TimeoutExpired:
                return {
                    "verdict": FAIL,
                    "output": f"Build timed out after {self.timeout}s",
                    "command": printable,
                }
            except OSError as e:
                _log.warning("Could not run build command: %s", e)
                return {"verdict": SKIPPED, "output": f"Could not run build: {e}", "command": printable}

        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        verdict = PASS if proc.returncode == 0 else FAIL
        if verdict == FAIL:
            _log.info("Build FAILED (exit %s)", proc.returncode)
        return {
            "verdict": verdict,
            "output": combined[:_MAX_OUTPUT_CHARS],
            "command": printable,
        }
