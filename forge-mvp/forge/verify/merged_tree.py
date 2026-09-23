"""The post-migration view of a project: the source tree with the output overlaid.

``./migrated`` holds only the files the pipeline wrote. Every project-level
question — "does any file still import Struts?", "does the module compile?" —
is about the whole tree as it would be after the output is applied. This
module answers that without requiring the pipeline to mirror the source: a
virtual view for text checks, and a materialised copy for anything that needs
a real filesystem (a build, an extractor).
"""

import os
import shutil
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple

from forge.packs.glob import glob_match
from forge.utils.fs import EXCLUDED_DIRS

_MAX_TEXT_BYTES = 2 * 1024 * 1024


class MergedTree:
    def __init__(self, source_dir: str, output_dir: Optional[str], deleted: Sequence[str] = ()):
        self.source = Path(source_dir).resolve()
        self.output = Path(output_dir).resolve() if output_dir else None
        # Paths (relative, forward slashes) the migration retired, e.g. a
        # struts-config.xml replaced by Java config. Absent from the view.
        self.deleted = {d.replace("\\", "/").lstrip("/") for d in deleted}

    # ── enumeration ──────────────────────────────────────────────────────────

    def _walk(self, root: Path) -> Iterator[Path]:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
            for f in sorted(files):
                yield Path(dirpath) / f

    def rel_paths(self) -> Iterator[str]:
        """Every relative path in the merged view, source first then output-only additions."""
        seen = set()
        for p in self._walk(self.source):
            rel = str(p.relative_to(self.source)).replace("\\", "/")
            if rel in self.deleted:
                continue
            seen.add(rel)
            yield rel
        if self.output and self.output.is_dir():
            for p in self._walk(self.output):
                rel = str(p.relative_to(self.output)).replace("\\", "/")
                if rel in seen or rel in self.deleted or self._is_artifact(rel):
                    continue
                seen.add(rel)
                yield rel

    @staticmethod
    def _is_artifact(rel: str) -> bool:
        """The pipeline's own outputs live in output_dir too; they are not project files.

        Held units live under .forge-staging/ until a human approves them — an
        acceptance check must not see them as part of the migrated tree.
        """
        if rel.startswith(".forge-staging/"):
            return True
        if rel.startswith("decisions") and rel.endswith(".json"):
            return True
        from forge.utils.report import is_report_artifact

        # Per-pack reports and the plan summary (migration-report-<pack>.md, ...).
        if is_report_artifact(rel):
            return True
        return rel in ("migration-report.md", "migration-context.json", "migration-acceptance.json",
                       "manual-review-queue.json", "migration-review.html", "pack-feedback.md",
                       "decisions-applied.jsonl", "project-build.json", ".forge-writes.json")

    def resolve(self, rel: str) -> Optional[Path]:
        """The file backing ``rel`` in the merged view — output wins over source."""
        if rel in self.deleted:
            return None
        if self.output:
            cand = self.output / rel
            if cand.is_file():
                return cand
        cand = self.source / rel
        return cand if cand.is_file() else None

    def iter_text(self, scope: str, *, side: str = "merged") -> Iterator[Tuple[str, str]]:
        """``(rel_path, text)`` for files matching ``scope``.

        ``side`` is ``merged`` (post-migration view) or ``source`` (pre-migration).
        """
        for rel in self.rel_paths():
            if scope and not glob_match(scope, rel):
                continue
            if side == "source":
                path = self.source / rel
                if not path.is_file():
                    continue
            else:
                path = self.resolve(rel)
                if path is None:
                    continue
            try:
                if path.stat().st_size > _MAX_TEXT_BYTES:
                    continue
                yield rel, path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

    # ── materialisation ──────────────────────────────────────────────────────

    def materialize(self, dest: str) -> Path:
        """Copy the merged view to ``dest`` for a build or an extractor.

        Build directories and VCS state are not copied; a compile that needs
        them regenerates them.
        """
        root = Path(dest)
        root.mkdir(parents=True, exist_ok=True)
        for rel in self.rel_paths():
            src = self.resolve(rel)
            if src is None:
                continue
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        return root
