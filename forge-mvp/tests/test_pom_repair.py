"""The one pom repair a rubric already prescribes is made in code, and an unrewritten parent shows no old values.

On AMS the transform produced a versionless ``<dependencyManagement>`` entry on
attempt after attempt, the parent BOM ended held, and every child was then
judged against the unrewritten parent -- whose pre-migration values the
reviewer cited in its deductions, whatever the note beside them said.
"""

import contextlib
from pathlib import Path
from unittest.mock import patch

from forge.extract import clear_context_cache, get_context
from forge.verify import syntax
from tests.conftest import make_state, write_config
from tests.test_extract_reactor import AGGREGATOR, LEAF, ROOT_POM
from tests.test_pom_properties import CONFIG, parent
from tests.test_syntax import _graph

VERSIONLESS = parent().replace("<version>5.10.5</version><scope>test</scope>", "<scope>test</scope>")


# ─── repair ───────────────────────────────────────────────────────────────────

def test_a_versionless_managed_entry_is_removed_and_named():
    fixed, repaired = syntax.repair_pom(VERSIONLESS)
    assert repaired == ["removed the versionless <dependencyManagement> entry org.junit.jupiter:junit-jupiter"]
    assert "junit-jupiter" not in fixed and "junit-bom" in fixed, "the BOM import stays"
    assert syntax.check_files({"pom.xml": fixed}, CONFIG, inherited_properties=set()) == (syntax.PASS, [])


def test_a_sound_pom_is_left_byte_identical():
    fixed, repaired = syntax.repair_pom(parent())
    assert (fixed, repaired) == (parent(), [])


def test_a_versionless_entry_with_exclusions_is_kept_for_the_model_to_fix():
    pom = VERSIONLESS.replace("<scope>test</scope>\n      </dependency>",
                              "<scope>test</scope><exclusions><exclusion><groupId>x</groupId>"
                              "<artifactId>y</artifactId></exclusion></exclusions>\n      </dependency>", 1)
    assert "<exclusions>" in pom
    fixed, repaired = syntax.repair_pom(pom)
    assert repaired == [] and fixed == pom


def test_a_versionless_dependency_outside_management_is_not_touched():
    pom = parent().replace("</project>", "<dependencies><dependency><groupId>a</groupId><artifactId>b</artifactId>"
                                         "</dependency></dependencies></project>")
    assert syntax.repair_pom(pom) == (pom, [])


def test_the_graph_repairs_the_pom_before_the_gate_and_records_it(tmp_path):
    from forge.graph import build_graph

    pom = tmp_path / "pom.xml"
    pom.write_text(parent(), encoding="utf-8")
    cfg = write_config(tmp_path, syntax_check=True)
    with contextlib.ExitStack() as stack:
        up, rev = _graph(stack, str(pom), [VERSIONLESS])
        result = build_graph(cfg).invoke(make_state(str(pom), tmp_path, phase="build-maven-modernize"),
                                         config={"configurable": {"thread_id": str(pom)}})
    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 0, "no retry was needed"
    assert fs["syntax_verdict"] == syntax.PASS
    assert any(f.startswith("repaired: removed the versionless") for f in fs["guardrail_findings"])
    written = fs["transform_output"]["files"][str(pom)]
    assert "junit-jupiter" not in written
    assert rev.return_value.invoke.call_count == 1
    reviewed = rev.return_value.invoke.call_args[0][0][1].content
    assert "junit-jupiter" not in reviewed, "the reviewer sees the repaired file"


# ─── an unrewritten parent shows names, not values ────────────────────────────

def _reactor(tmp_path: Path) -> Path:
    (tmp_path / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    (tmp_path / "internal" / "services").mkdir(parents=True)
    (tmp_path / "internal" / "pom.xml").write_text(AGGREGATOR, encoding="utf-8")
    (tmp_path / "internal" / "services" / "pom.xml").write_text(LEAF, encoding="utf-8")
    return tmp_path / "internal" / "services"


def test_an_unrewritten_parent_is_shown_by_name_only_during_a_run(tmp_path):
    clear_context_cache()
    leaf = _reactor(tmp_path)
    out = tmp_path / "out"
    ctx = get_context("reactor", str(tmp_path), str(leaf), output_dir=str(out)).data
    root = ctx["parents"][1]
    assert root["rewritten"] is False
    assert set(root["properties"]) == {"maven.compiler.source", "spring.version", "ams.version"}
    assert "1.8" not in " ".join(root["properties"].values())
    assert root["bom_imports"] == ["org.springframework:spring-framework-bom:(pre-migration)"]
    assert root["managed_versions"] == {"com.h2database:h2": "(pre-migration)"}
    assert ctx["inherited_properties"]["spring.version"] == "5.3.40", "the merged view still carries real names"

    (out / "pom.xml").parent.mkdir(parents=True, exist_ok=True)
    (out / "pom.xml").write_text(ROOT_POM.replace("1.8", "21"), encoding="utf-8")
    after = get_context("reactor", str(tmp_path), str(leaf), output_dir=str(out)).data
    assert after["parents"][1]["rewritten"] is True
    assert after["parents"][1]["properties"]["maven.compiler.source"] == "21", "a rewritten parent shows its values"


def test_outside_a_run_the_source_values_are_shown(tmp_path):
    leaf = _reactor(tmp_path)
    ctx = get_context("reactor", str(tmp_path), str(leaf)).data
    assert ctx["parents"][1]["properties"]["maven.compiler.source"] == "1.8"
