"""A generated test lands in the module that holds the class under test, and is recorded.

A reactor has one ``src/test/java`` per module. Rebuilding the destination from
package and type alone put all 39 AMS tests at the repository root, where Maven
never compiles them; and because nothing recorded those writes, the branch sync
treated them as strays and reported nothing changed.
"""

from forge.leader import landing
from forge.testgen import targets, writer
from forge.testgen.targets import scan_test_targets
from forge.utils import run_manifest
from tests.test_testgen import SERVICE_JAVA, generated_test

MODULE = "ams-common/AssetManagementSharedServices"
SOURCE = f"{MODULE}/src/main/java/com/corp/user/UserService.java"
TEST = f"{MODULE}/src/test/java/com/corp/user/UserServiceTest.java"


def _out(tmp_path, files):
    out = tmp_path / "out"
    for rel, content in files.items():
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return out


def test_module_root_is_everything_before_src_main_java():
    assert targets.module_root_of(SOURCE) == MODULE
    assert targets.module_root_of("src/main/java/com/corp/A.java") == ""
    assert targets.module_root_of("lib/A.java") == ""
    assert targets.test_root_for(SOURCE) == f"{MODULE}/src/test/java"
    assert targets.test_rel_path_for("com.corp.user", "UserService", SOURCE) == TEST
    # A single-module project is unchanged.
    assert targets.test_rel_path_for("com.corp.user", "UserService") == "src/test/java/com/corp/user/UserServiceTest.java"


def test_a_class_in_a_module_gets_its_test_in_that_modules_tree(tmp_path):
    out = _out(tmp_path, {SOURCE: SERVICE_JAVA})
    scan = scan_test_targets(str(out), str(tmp_path / "proj"))
    assert [t.test_rel_path for t in scan.targets] == [TEST]


def test_the_models_package_may_differ_but_the_module_may_not(tmp_path):
    out = _out(tmp_path, {SOURCE: SERVICE_JAVA})
    target = scan_test_targets(str(out), str(tmp_path / "proj")).targets[0]
    content = generated_test("com.corp.other.UserServiceTest")
    assert writer.destination_for(content, target) == f"{MODULE}/src/test/java/com/corp/other/UserServiceTest.java"
    written = writer.write_test_files({"../../etc/anything.java": content}, target, str(out))
    assert written == [str((out / MODULE / "src/test/java/com/corp/other/UserServiceTest.java").resolve())]
    assert not (out / "src/test/java").exists(), "nothing lands at the repository root"


def test_a_held_test_is_staged_under_its_module_too(tmp_path):
    out = _out(tmp_path, {SOURCE: SERVICE_JAVA})
    target = scan_test_targets(str(out), str(tmp_path / "proj")).targets[0]
    held = writer.stage_test_files({"t.java": generated_test("com.corp.user.UserServiceTest")}, target, str(out))
    assert held == [str((out / ".forge-staging" / TEST).resolve())]


def test_written_tests_are_recorded_so_landing_copies_them(tmp_path):
    out = _out(tmp_path, {SOURCE: SERVICE_JAVA})
    target = scan_test_targets(str(out), str(tmp_path / "proj")).targets[0]
    written = writer.write_test_files({"t.java": generated_test("com.corp.user.UserServiceTest")}, target, str(out))
    assert TEST in landing.unrecorded_files(str(out)), "unrecorded, landing would leave it behind"

    writer.record_written(str(out), written)
    assert run_manifest.load(str(out))[TEST] == writer.TESTGEN_PHASE
    assert TEST in landing.landable_files(str(out))
    assert TEST not in landing.unrecorded_files(str(out))

    writer.forget_written(str(out), written)
    assert TEST not in run_manifest.load(str(out))
