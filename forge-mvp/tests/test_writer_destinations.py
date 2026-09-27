"""The model's path key is a hint; a rewritten file goes back where it came from.

On AMS the JUnit pack committed three tests at three wrong paths -- the
source's absolute path with its leading slash lost, a path from the module
root, a path missing one directory -- and the build failed on the two JUnit 4
originals it had left behind.
"""

from pathlib import Path

from forge.utils.file_writer import clear_source_index, resolve_relative, write_files, write_output

TEST = "package org.example.am.shared.service.impl;\nimport org.junit.jupiter.api.Test;\npublic class CalendarServiceImplTest {}\n"
REL = "ams-common/AssetManagementSharedServices/src/test/java/org/example/am/shared/service/impl/CalendarServiceImplTest.java"
OTHER = "ams-internal/AssetManagementInternalWeb/src/test/java/org/example/am/internal/web/interceptors/AddressValidationInterceptorTest.java"


def _reactor(tmp_path: Path) -> Path:
    clear_source_index()
    proj = tmp_path / "proj"
    for rel in (REL, OTHER, "ams-common/AssetManagementSharedServices/pom.xml"):
        (proj / rel).parent.mkdir(parents=True, exist_ok=True)
        (proj / rel).write_text("original", encoding="utf-8")
    (proj / "ams-common/AssetManagementSharedServices/target/classes").mkdir(parents=True)
    return proj


def _state(proj: Path, out: Path, files: dict, unit: str = REL, generate: bool = False, deleted=None) -> dict:
    output = {"files": files}
    if deleted is not None:
        output["deleted_files"] = deleted
    return {"dry_run": False, "output_dir": str(out), "source_dir": str(proj),
            "current_file": {"file_path": str(proj / unit), "transform_output": output, "generate": generate}}


def test_the_units_own_rewrite_lands_at_the_units_path_whatever_the_key(tmp_path):
    proj, out = _reactor(tmp_path), tmp_path / "out"
    for key in (str(proj / REL).lstrip("/"),                      # absolute path, leading slash lost
                "src/test/java/org/example/am/shared/service/impl/CalendarServiceImplTest.java",   # from the module root
                "AssetManagementSharedServices/" + REL.split("/", 1)[1],                          # one directory missing
                "CalendarServiceImplTest.java",                   # bare name
                "somewhere/else/CalendarServiceImplTest.java"):   # nonsense
        written = write_output(_state(proj, out, {key: TEST}))
        assert written == [str((out / REL).resolve())], key
        (out / REL).unlink()
    assert not list(out.rglob("*.java")), "nothing landed anywhere else"


def test_a_rename_is_honoured_when_the_transform_retires_the_original(tmp_path):
    proj, out = _reactor(tmp_path), tmp_path / "out"
    new = REL.replace("CalendarServiceImplTest", "CalendarServiceTest")
    written = write_output(_state(proj, out, {new: TEST}, deleted=[REL]))
    assert written == [str((out / new).resolve())]


def test_other_keys_match_the_tail_of_exactly_one_existing_file(tmp_path):
    proj = _reactor(tmp_path)
    assert resolve_relative("AssetManagementInternalWeb/src/test/java/org/example/am/internal/web/interceptors/"
                            "AddressValidationInterceptorTest.java", "x", str(proj)) == Path(OTHER)
    assert resolve_relative(str(proj / OTHER).lstrip("/"), "x", str(proj)) == Path(OTHER)
    # A tail that matches exactly one file is that file; ambiguous or unknown tails are left as
    # they are, so a genuinely new file is written where the key says.
    assert resolve_relative("pom.xml", "<project/>", str(proj)) == Path("ams-common/AssetManagementSharedServices/pom.xml")
    (proj / "pom.xml").write_text("<project/>", encoding="utf-8")
    clear_source_index()
    assert resolve_relative("pom.xml", "<project/>", str(proj)) == Path("pom.xml"), "two poms: ambiguous"
    assert resolve_relative("docs/NEW.md", "hello", str(proj)) == Path("docs/NEW.md")


def test_a_bare_name_is_rebuilt_from_its_package_and_then_matched_to_its_module(tmp_path):
    proj = _reactor(tmp_path)
    main = "ams-common/AssetManagementSharedServices/src/main/java/org/example/am/shared/service/impl/CalendarServiceImpl.java"
    (proj / main).parent.mkdir(parents=True)
    (proj / main).write_text("original", encoding="utf-8")
    clear_source_index()
    content = "package org.example.am.shared.service.impl;\npublic class CalendarServiceImpl {}\n"
    assert resolve_relative("CalendarServiceImpl.java", content, str(proj)) == Path(main)


def test_a_generated_target_and_a_multi_file_answer_keep_the_guarded_resolution(tmp_path):
    proj, out = _reactor(tmp_path), tmp_path / "out"
    target = proj / "ams-internal/AssetManagementInternalWeb/src/main/webapp/META-INF/context.xml"
    written = write_output(_state(proj, out, {str(target): "<Context/>"}, unit=str(target.relative_to(proj)), generate=True))
    assert written == [str((out / target.relative_to(proj)).resolve())]
    two = write_files({REL: TEST, "docs/NOTES.md": "n"}, str(proj), out, unit_path=str(proj / REL))
    assert sorted(Path(p).relative_to(out.resolve()).as_posix() for p in two) == sorted([REL, "docs/NOTES.md"])


def test_nothing_escapes_the_output_directory(tmp_path):
    proj, out = _reactor(tmp_path), tmp_path / "out"
    assert write_files({"../../evil.java": "x"}, str(proj), out) == []
    assert not list(tmp_path.rglob("evil.java"))
