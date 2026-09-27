"""A generated test that does not compile is held before it is written, synced or committed.

With per-class test execution off, nothing compiled a generated test before
it landed in src/test/java. On AMS two of 40 named members that did not
exist or were protected, and the plan's own build failed on them. One build
with tests compiled, after generation, and every test the compiler names is
held with the error instead.
"""

from pathlib import Path

from forge.config import ForgeConfig
from forge.service import COMPILE_GATE_ROUNDS, _hold_generated_tests_that_do_not_compile, _testgen_compile_gate_enabled
from forge.testgen.writer import record_written
from forge.utils import run_manifest
from forge.verify.project_build import BuildResult

MODULE = "ams-common/AssetManagementSharedServices"
GOOD = f"{MODULE}/src/test/java/org/example/am/shared/GoodTest.java"
BAD = f"{MODULE}/src/test/java/org/example/am/shared/dao/impl/ContactDAOImplTest.java"


def _project(tmp_path: Path):
    src, out = tmp_path / "proj", tmp_path / "proj" / ".migrated"
    (src / MODULE).mkdir(parents=True)
    (src / "pom.xml").write_text("<project/>", encoding="utf-8")
    (src / MODULE / "pom.xml").write_text("<project/>", encoding="utf-8")
    units = []
    for rel in (GOOD, BAD):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("class T {}", encoding="utf-8")
        units.append({"status": "GENERATED", "rel_path": rel, "written_paths": [str(p)], "held_paths": []})
    record_written(str(out), [str(out / GOOD), str(out / BAD)])
    return src, out, units


def _fake_build(*results):
    calls = []

    def build(root, config):
        calls.append(root)
        return results[min(len(calls), len(results)) - 1]
    build.calls = calls
    return build


def test_the_test_the_compiler_names_is_held_and_the_rest_stays(tmp_path):
    src, out, units = _project(tmp_path)
    fail = BuildResult("fail", "ams-common/pom.xml failed (exit 1)", failed_step="ams-common/pom.xml",
                       output="[INFO] Compiling\n[ERROR] COMPILATION ERROR :\n"
                              f"[ERROR] {BAD}:[51,59] getNamedParameterJdbcTemplate() has protected access in BaseDAO\n"
                              "[ERROR] -> [Help 1]\n")
    build = _fake_build(fail, BuildResult("pass", "3 step(s) built"))
    events = []
    held = _hold_generated_tests_that_do_not_compile(units, str(src), str(out), ForgeConfig(data={}),
                                                     on_event=events.append, build=build)
    assert held == [BAD] and len(build.calls) == 2, "held, then built again to confirm"
    bad, good = units[1], units[0]
    assert bad["status"] == "HELD" and bad["hold_reason"].startswith("does not compile: ") and "protected access" in bad["hold_reason"]
    assert not (out / BAD).exists() and (out / ".forge-staging" / BAD).is_file()
    assert bad["held_paths"] == [str(out / ".forge-staging" / BAD)] and bad["written_paths"] == []
    assert good["status"] == "GENERATED" and (out / GOOD).is_file()
    assert BAD not in run_manifest.load(str(out)) and GOOD in run_manifest.load(str(out))
    kinds = [e["type"] for e in events]
    assert "testgen_held" in kinds and events[-1] == {"type": "testgen_compile_gate", "outcome": "pass", "round": 2, "held": 1}


def test_a_failure_that_names_no_generated_test_is_left_to_the_build_verdict(tmp_path):
    src, out, units = _project(tmp_path)
    fail = BuildResult("fail", "ams-common/pom.xml failed (exit 1)",
                       output=f"[ERROR] {MODULE}/src/main/java/org/example/am/shared/Foo.java:[3,4] cannot find symbol\n")
    build = _fake_build(fail)
    events = []
    assert _hold_generated_tests_that_do_not_compile(units, str(src), str(out), ForgeConfig(data={}),
                                                     on_event=events.append, build=build) == []
    assert all(u["status"] == "GENERATED" for u in units) and len(build.calls) == 1
    assert events[-1]["outcome"] == "fail" and "no generated test" in events[-1]["reason"]


def test_no_maven_means_no_gate_and_a_pass_means_nothing_held(tmp_path):
    src, out, units = _project(tmp_path)
    assert _hold_generated_tests_that_do_not_compile(units, str(src), str(out), ForgeConfig(data={}),
                                                     build=_fake_build(BuildResult("skip", "'mvn' is not on PATH"))) == []
    assert _hold_generated_tests_that_do_not_compile(units, str(src), str(out), ForgeConfig(data={}),
                                                     build=_fake_build(BuildResult("pass", "ok"))) == []
    assert all(u["status"] == "GENERATED" for u in units)


def test_the_gate_stops_after_its_rounds(tmp_path):
    src, out, units = _project(tmp_path)
    always = BuildResult("fail", "x", output=f"[ERROR] {BAD}:[1,1] bad\n[ERROR] {GOOD}:[1,1] bad\n")
    build = _fake_build(always)
    held = _hold_generated_tests_that_do_not_compile(units, str(src), str(out), ForgeConfig(data={}), build=build)
    assert sorted(held) == sorted([GOOD, BAD]) and len(build.calls) == 1, "nothing left to hold ends the gate early"
    assert COMPILE_GATE_ROUNDS >= 2


def test_the_gate_is_on_by_default_and_can_be_turned_off():
    assert _testgen_compile_gate_enabled(ForgeConfig(data={}))
    assert _testgen_compile_gate_enabled(ForgeConfig(data={"test_generation": {"compile_gate": True}}))
    assert not _testgen_compile_gate_enabled(ForgeConfig(data={"test_generation": {"compile_gate": False}}))
