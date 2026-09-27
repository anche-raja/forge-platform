"""A build-tier pack transforms parents before children, one at a time, against the rewritten parent.

Eight poms at once handed every child its pre-migration parent as context, and
a sound child pom was held at 45 on AMS with every deduction reading "the
parent has not been migrated yet". The order is inheritance depth, not
directory depth: AMS's parent BOM lives in ``ams-parent-bom/`` beside the
modules that inherit from it.
"""

from pathlib import Path

from forge.config import ForgeConfig
from forge.extract import clear_context_cache, get_context
from forge.extract.reactor import build_order, inheritance_depth, run
from forge.service import _workers_for
from tests.test_extract_reactor import AGGREGATOR, LEAF, ROOT_POM


def _reactor(tmp_path: Path):
    # The root lives beside the modules, as on AMS, not above them.
    (tmp_path / "parent-bom").mkdir()
    (tmp_path / "parent-bom" / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    (tmp_path / "internal" / "services").mkdir(parents=True)
    (tmp_path / "internal" / "pom.xml").write_text(
        AGGREGATOR.replace("<artifactId>internal</artifactId>",
                           "<artifactId>internal</artifactId>").replace(
            "<parent><groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version></parent>",
            "<parent><groupId>com.corp</groupId><artifactId>parent</artifactId><version>1.0</version>"
            "<relativePath>../parent-bom/pom.xml</relativePath></parent>"), encoding="utf-8")
    (tmp_path / "internal" / "services" / "pom.xml").write_text(LEAF, encoding="utf-8")
    return [str(tmp_path / p) for p in ("internal/services/pom.xml", "internal/pom.xml", "parent-bom/pom.xml")]


def test_inheritance_depth_not_directory_depth_orders_the_poms(tmp_path):
    leaf, aggregator, root = _reactor(tmp_path)
    assert [inheritance_depth(p) for p in (root, aggregator, leaf)] == [0, 1, 2]
    assert build_order([leaf, aggregator, root]) == [root, aggregator, leaf]
    # Ties keep the scan order, so the run stays reproducible.
    other = str(tmp_path / "build.xml")
    assert build_order([leaf, other, root]) == [other, root, leaf]


def test_a_build_tier_pack_runs_one_unit_at_a_time_and_others_keep_their_parallelism():
    cfg = ForgeConfig(data={"max_parallel_files": 8})
    assert _workers_for(cfg, "build-maven-modernize") == 1
    assert _workers_for(cfg, "javax-to-jakarta") == 8
    assert _workers_for(cfg) == 8
    assert _workers_for(cfg, "no-such-pack") == 8


def test_a_child_sees_the_parent_this_run_has_rewritten_and_the_summary_says_so(tmp_path):
    clear_context_cache()
    leaf, _aggregator, root = _reactor(tmp_path)
    out = tmp_path / "out"
    before = get_context("reactor", str(tmp_path), str(Path(leaf).parent), output_dir=str(out)).data
    assert before["inherited_properties"]["maven.compiler.source"] == "1.8"
    assert [p["rewritten"] for p in before["parents"]] == [False, False]
    assert any("BEFORE this pack ran" in n for n in before["summary"]["notes"])

    # The run rewrites the root into the output directory.
    (out / "parent-bom").mkdir(parents=True)
    (out / "parent-bom" / "pom.xml").write_text(
        ROOT_POM.replace("<maven.compiler.source>1.8</maven.compiler.source>",
                         "<maven.compiler.release>21</maven.compiler.release>"), encoding="utf-8")
    after = get_context("reactor", str(tmp_path), str(Path(leaf).parent), output_dir=str(out)).data
    assert after["inherited_properties"]["maven.compiler.release"] == "21"
    assert "maven.compiler.source" not in after["inherited_properties"]
    assert [p["rewritten"] for p in after["parents"]] == [False, True]
    assert after["summary"]["rewritten_parents"] == ["parent-bom/pom.xml"]
    assert any("not yet rewritten (internal/pom.xml)" in n for n in after["summary"]["notes"])


def test_the_modules_own_old_values_are_not_restated(tmp_path):
    leaf, _aggregator, root = _reactor(tmp_path)
    ctx = run(str(tmp_path), str(Path(root).parent))
    assert set(ctx["module"]) == {"path", "coordinates", "packaging", "parent", "modules"}
    assert ctx["parents"] == [] and any("reactor root" in n for n in ctx["summary"]["notes"])
    assert any("inherits nothing" in n for n in ctx["summary"]["notes"])
