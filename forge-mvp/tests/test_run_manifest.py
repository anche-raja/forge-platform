"""Two packs must not silently overwrite each other in one output directory.

Packs read the ORIGINAL source — verified against a live run: after
`javax-to-jakarta` produced `import jakarta.servlet`, the next pack's queue
entry held `import javax.servlet`, the untouched original. So the second pack's
output replaces the first's rather than building on it, and nothing fails. These
tests pin the refusal that now stops it.
"""

from pathlib import Path

import pytest

from forge.utils import run_manifest


@pytest.fixture
def out(tmp_path):
    d = tmp_path / "migrated"
    d.mkdir()
    return str(d)


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "app"
    (d / "src/main/java/com/corp").mkdir(parents=True)
    return str(d)


def _unit(src, rel):
    p = Path(src) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("class X {}", encoding="utf-8")
    return str(p)


SERVER_XML = "web/src/main/liberty/config/server.xml"


def test_a_file_a_later_pack_retires_is_no_longer_a_write(out):
    """Or landing would copy the Liberty server.xml straight back in after the
    Tomcat pack retired it."""
    run_manifest.record(out, "liberty-server-config", [SERVER_XML])
    run_manifest.record(out, "tomcat-context-config", [], deleted=[SERVER_XML])
    assert SERVER_XML not in run_manifest.load(out)
    assert run_manifest.deleted_paths(out) == [SERVER_XML]

    run_manifest.record(out, "liberty-server-config", [SERVER_XML])
    assert run_manifest.load(out)[SERVER_XML] == "liberty-server-config"
    assert run_manifest.deleted_paths(out) == [], "writing it again brings it back"


def test_a_retired_file_is_not_landed_even_if_it_was_approved(out):
    from forge.leader.landing import recorded_files

    run_manifest.record(out, "liberty-server-config", [SERVER_XML, "web/pom.xml"])
    run_manifest.record(out, "tomcat-context-config", [], deleted=[SERVER_XML])
    assert recorded_files(out) == {"web/pom.xml"}


def test_an_absolute_deleted_path_is_recorded_relative_to_the_tree_the_run_read(src):
    from forge.service import _tree_rel

    assert _tree_rel(str(Path(src) / SERVER_XML), src) == SERVER_XML
    assert _tree_rel(SERVER_XML, src) == SERVER_XML
    outside = str(Path(src).parent / "elsewhere/server.xml")
    assert _tree_rel(outside, src) == Path(outside).as_posix(), "outside the tree: kept, landing bounds it"


def test_an_empty_directory_has_no_conflicts(out, src):
    unit = _unit(src, "src/main/java/com/corp/A.java")
    assert run_manifest.conflicts(out, "java8-to-java21", src, [unit]) == []


def test_a_second_pack_over_the_same_file_conflicts(out, src):
    rel = "src/main/java/com/corp/A.java"
    unit = _unit(src, rel)
    run_manifest.record(out, "javax-to-jakarta", [str(Path(out) / rel)])

    clashes = run_manifest.conflicts(out, "java8-to-java21", src, [unit])
    assert clashes == [(rel, "javax-to-jakarta")]

    message = run_manifest.refusal("java8-to-java21", clashes)
    assert "javax-to-jakarta" in message, "the message names who owns the file"
    assert rel in message
    # Both remedies, because refusing without one is just an obstacle.
    assert "--phase java21" in message
    assert "./step1" in message


def test_rerunning_the_same_pack_is_never_a_conflict(out, src):
    """A pack must stay re-runnable — after a prompt fix, or a partial failure."""
    rel = "src/main/java/com/corp/A.java"
    unit = _unit(src, rel)
    run_manifest.record(out, "javax-to-jakarta", [str(Path(out) / rel)])
    assert run_manifest.conflicts(out, "javax-to-jakarta", src, [unit]) == []


def test_packs_touching_different_files_do_not_conflict(out, src):
    """The common safe case: liberty-server-config and jsp-jstl-modernize."""
    a = _unit(src, "src/main/webapp/WEB-INF/web.xml")
    b = _unit(src, "src/main/webapp/index.jsp")
    run_manifest.record(out, "webapp-bootstrap-jakarta10", [str(Path(out) / "src/main/webapp/WEB-INF/web.xml")])
    assert run_manifest.conflicts(out, "jsp-jstl-modernize", src, [b]) == []
    assert run_manifest.conflicts(out, "jsp-jstl-modernize", src, [a, b])


def test_an_unreadable_manifest_does_not_block_a_run(out, src):
    """A guard that cannot read its own notes must not stop work."""
    unit = _unit(src, "src/main/java/com/corp/A.java")
    (Path(out) / run_manifest.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert run_manifest.load(out) == {}
    assert run_manifest.conflicts(out, "java8-to-java21", src, [unit]) == []


def test_recording_nothing_writes_no_manifest(out, src):
    """A run that wrote nothing — all held or blocked — claims no files."""
    run_manifest.record(out, "javax-to-jakarta", [])
    assert not (Path(out) / run_manifest.MANIFEST_NAME).exists()


def test_the_refusal_summarises_instead_of_listing_hundreds(out, src):
    clashes = [(f"src/main/java/com/corp/F{i}.java", "javax-to-jakarta") for i in range(40)]
    message = run_manifest.refusal("java8-to-java21", clashes)
    assert "40 file(s)" in message
    assert "… and 35 more" in message
    assert message.count("written by javax-to-jakarta") == 5, "five shown, the rest counted"
