"""Where a generated test lands — decided here, not by the model.

``forge/utils/file_writer.py`` treats the model's path as a hint because a
migration must put a rewritten file back where it came from. Generation has no
such constraint: there is exactly one right destination for
``com.corp.UserServiceTest``, this pipeline knows it, and the model's key adds
nothing but a way to escape the output directory. So the key is ignored, the
package and type are read out of the content, and everything is written under
the ``src/test/java`` of the module that holds the class under test — which is
then guarded anyway.

The module matters. A reactor has one test tree per module, and a test written
to the repository root's ``src/test/java`` belongs to no module: Maven never
compiles it. On AMS every generated test landed there.

What is written is also recorded in the run manifest. Landing and the branch
sync copy only what a run recorded — an unrecorded file in the output directory
is treated as a stray, never committed — and generated tests were not recorded,
so 39 of them stayed behind while the sync reported nothing changed.
"""

from pathlib import Path
from typing import Dict, List, Mapping, Sequence

from forge.testgen.targets import TEST_ROOT, TestTarget, java_rel_path, read_surface, test_root_for
from forge.utils import run_manifest
from forge.utils.file_writer import staging_root
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# The manifest owner of a generated test. Not a pack id, so a later pack that
# selects the file is refused rather than allowed to overwrite it silently.
TESTGEN_PHASE = "generate-tests"


def test_root_of(target: TestTarget) -> str:
    """The test source root ``target``'s test belongs under: its own module's."""
    rel = (target.test_rel_path or "").replace("\\", "/")
    idx = rel.rfind(TEST_ROOT)
    if idx >= 0:
        return rel[: idx + len(TEST_ROOT)]
    return test_root_for(target.rel_path)


def destination_for(content: str, target: TestTarget) -> str:
    """The relative path a generated file belongs at, from its own declarations."""
    surface = read_surface(content)
    if surface is None or not surface.type_name:
        return target.test_rel_path
    package = surface.package or target.package
    return java_rel_path(package, surface.type_name, test_root_of(target))


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
    test_root = root / test_root_of(target)
    written: List[str] = []
    for rel, content in sorted(plan_writes(files, target).items()):
        dest = (root / rel).resolve()
        if not dest.is_relative_to(test_root):
            # Cannot happen from plan_writes; it is here because the day it can,
            # it must fail closed rather than write into src/main.
            _log.error("Refusing to write a generated test outside %s: %s", test_root, dest)
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


def record_written(output_dir: str, paths: Sequence[str]) -> None:
    """Note the generated tests in the run manifest, so landing and the branch sync copy them."""
    if paths:
        run_manifest.record(output_dir, TESTGEN_PHASE, list(paths))


def forget_written(output_dir: str, paths: Sequence[str]) -> None:
    """Drop tests that were written and then taken back out from the manifest."""
    rels = [run_manifest._rel(output_dir, p) for p in paths]
    if rels:
        run_manifest.forget(output_dir, rels)


def remove_written(paths: List[str], output_dir: str) -> None:
    """Take back tests that were written and then failed — inside output_dir only."""
    root = Path(output_dir).resolve()
    for p in paths:
        path = Path(p)
        if path.is_file() and path.resolve().is_relative_to(root):
            path.unlink()
