"""Where a generated test lands — decided here, not by the model.

``forge/utils/file_writer.py`` treats the model's path as a hint because a
migration must put a rewritten file back where it came from. Generation has no
such constraint: there is exactly one right destination for
``com.corp.UserServiceTest``, this pipeline knows it, and the model's key adds
nothing but a way to escape the output directory. So the key is ignored, the
package and type are read out of the content, and everything is written under
``src/test/java`` inside the destination root — which is then guarded anyway.
"""

from pathlib import Path
from typing import Dict, List, Mapping

from forge.testgen.targets import TestTarget, java_rel_path, read_surface
from forge.utils.file_writer import staging_root
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

TEST_ROOT = "src/test/java"


def destination_for(content: str, target: TestTarget) -> str:
    """The relative path a generated file belongs at, from its own declarations."""
    surface = read_surface(content)
    if surface is None or not surface.type_name:
        return target.test_rel_path
    package = surface.package or target.package
    return java_rel_path(package, surface.type_name, TEST_ROOT)


def plan_writes(files: Mapping[str, str], target: TestTarget) -> Dict[str, str]:
    """``relative destination -> content``, with the model's keys discarded."""
    planned: Dict[str, str] = {}
    for _key, content in sorted(files.items()):
        if not str(content).strip():
            continue
        planned[destination_for(str(content), target)] = str(content)
    return planned


def write_test_files(files: Mapping[str, str], target: TestTarget, dest_root: str) -> List[str]:
    """Write the generated files under ``dest_root``. Returns absolute paths written."""
    root = Path(dest_root).resolve()
    written: List[str] = []
    for rel, content in sorted(plan_writes(files, target).items()):
        dest = (root / rel).resolve()
        if not dest.is_relative_to(root / TEST_ROOT):
            # Cannot happen from plan_writes; it is here because the day it can,
            # it must fail closed rather than write into src/main.
            _log.error("Refusing to write a generated test outside %s: %s", root / TEST_ROOT, dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(str(dest))
        _log.info("Wrote %s", dest)
    return written


def stage_test_files(files: Mapping[str, str], target: TestTarget, output_dir: str) -> List[str]:
    """Write to the staging tree instead — a test a human has to look at first.

    The same ``.forge-staging/`` a held migration unit uses, so one directory
    holds everything waiting on a person and the merged tree skips all of it.
    """
    return write_test_files(files, target, str(staging_root(output_dir)))


def remove_written(paths: List[str], output_dir: str) -> None:
    """Take back tests that were written and then failed — inside output_dir only."""
    root = Path(output_dir).resolve()
    for p in paths:
        path = Path(p)
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
