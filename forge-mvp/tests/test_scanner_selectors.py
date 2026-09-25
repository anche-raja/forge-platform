"""The scanner resolves a pack's selectors through its registered extractor,
and still refuses a pack whose extractor is not built."""

from pathlib import Path

import pytest

from forge.extract import clear_context_cache
from forge.utils.file_scanner import runnable_phases, scan_java_files
from tests.test_extract_web_bootstrap import make_module
from tests.test_packs import _fm, write_pack

WEBAPP = "webapp-bootstrap-jakarta10"
LIBERTY = "liberty-server-config"


@pytest.fixture(autouse=True)
def _fresh():
    clear_context_cache()
    yield
    clear_context_cache()


@pytest.fixture
def fresh_registry(monkeypatch):
    from forge import phases

    def use(directory):
        monkeypatch.setenv("FORGE_PACKS_DIR", str(directory))
        phases._packs.cache_clear()
        return phases

    yield use
    phases._packs.cache_clear()


def _names(paths):
    return sorted(Path(p).name for p in paths)


def test_both_web_bootstrap_packs_are_runnable_now():
    assert {WEBAPP, LIBERTY} <= set(runnable_phases())


def test_webapp_bootstrap_scan_returns_descriptors_plus_resolved_components(tmp_path):
    mod = make_module(tmp_path, vendors=("jboss-web.xml",),
                      java=("LoggingFilter.java", "StartupListener.java", "AuditFilter.java", "CodeTable.java"))
    scan = scan_java_files(str(tmp_path), WEBAPP)
    # Globs: the descriptors. Selector: every filter/listener class, declared or annotated.
    assert _names(scan.files) == ["AuditFilter.java", "LoggingFilter.java", "StartupListener.java",
                                  "jboss-web.xml", "web.xml"]
    assert "CodeTable.java" not in _names(scan.files), "a service class is not a servlet component"
    assert scan.generated == ()


def test_liberty_scan_yields_exactly_one_generated_unit_per_module_and_no_files(tmp_path):
    a = make_module(tmp_path, name="a")
    b = make_module(tmp_path, name="b")
    scan = scan_java_files(str(tmp_path), LIBERTY)
    assert scan.files == []
    assert scan.generated == tuple(sorted(str(m / "src/main/liberty/config/server.xml") for m in (a, b)))
    for g in scan.generated:
        assert not Path(g).exists()


def test_liberty_scan_edits_an_existing_server_xml_in_place(tmp_path):
    """If the generate target already exists, it is a file to migrate, not to
    create — the transform reads it rather than being told to invent it."""
    mod = make_module(tmp_path, extras=(("server.xml", "src/main/liberty/config/server.xml"),))
    scan = scan_java_files(str(tmp_path), LIBERTY)
    target = str(mod / "src/main/liberty/config/server.xml")
    assert target in scan.files
    assert scan.generated == ()


TOMCAT = "tomcat-context-config"


def test_tomcat_scan_generates_one_context_xml_per_module_and_takes_liberty_images(tmp_path):
    a = make_module(tmp_path, name="a")
    b = make_module(tmp_path, name="b")
    (a / "Dockerfile").write_text("FROM websphere-liberty:26.0.0.8-full-java8-ibmjava\n", encoding="utf-8")
    (b / "Dockerfile").write_text("FROM eclipse-temurin:21-jre\n", encoding="utf-8")
    scan = scan_java_files(str(tmp_path), TOMCAT)
    assert scan.generated == tuple(sorted(str(m / "src/main/webapp/META-INF/context.xml") for m in (a, b)))
    assert [Path(f).resolve() for f in scan.files] == [(a / "Dockerfile").resolve()], \
        "only the image built on Liberty"


def test_a_named_file_the_pack_would_create_is_generated_only_for_its_own_selector():
    from forge.extract.selectors import is_generated_target
    from forge.phases import get_phase

    context_xml, server_xml = "/absent/src/main/webapp/META-INF/context.xml", "/absent/src/main/liberty/config/server.xml"
    assert is_generated_target(get_phase(TOMCAT), context_xml)
    assert not is_generated_target(get_phase(TOMCAT), server_xml)
    assert is_generated_target(get_phase(LIBERTY), server_xml)
    assert not is_generated_target(get_phase(LIBERTY), context_xml)


def test_selector_files_respect_scope_prefix_and_test_source_exclusion(tmp_path):
    mod = make_module(tmp_path, java=("LoggingFilter.java", "StartupListener.java"))
    test_src = mod / "src/test/java/com/acme/orders/web"
    test_src.mkdir(parents=True)
    (test_src / "AuditFilter.java").write_text(
        "package com.acme.orders.web;\n@javax.servlet.annotation.WebFilter(\"/*\")\npublic class AuditFilter implements javax.servlet.Filter {}\n",
        encoding="utf-8")
    scan = scan_java_files(str(tmp_path), WEBAPP, scope_package_prefix="com.other")
    # Both project classes are outside com.other → skipped, not silently dropped.
    assert _names(scan.files) == ["web.xml"]
    assert {Path(s.path).name for s in scan.skipped} == {"LoggingFilter.java", "StartupListener.java"}
    assert all("com.other" in s.reason for s in scan.skipped)


def test_two_modules_each_resolve_their_own_components(tmp_path):
    a = make_module(tmp_path, name="a", java=("LoggingFilter.java",))
    b = make_module(tmp_path, name="b", java=("StartupListener.java",))
    scan = scan_java_files(str(tmp_path), WEBAPP)
    files = [Path(f) for f in scan.files]
    assert any(f.name == "LoggingFilter.java" and a in f.parents for f in files)
    assert any(f.name == "StartupListener.java" and b in f.parents for f in files)
    assert sum(1 for f in files if f.name == "web.xml") == 2


def test_unregistered_context_is_still_refused_with_the_runnable_list(fresh_registry, tmp_path):
    packs_dir = tmp_path / "packs"
    write_pack(packs_dir, "routing", frontmatter=_fm(
        "routing", context="struts_routing_table", applies_to='\n  - file_glob: "**/struts*.xml"\n  - selector: struts_actions'))
    fresh_registry(packs_dir)
    make_module(tmp_path)
    with pytest.raises(ValueError) as exc:
        scan_java_files(str(tmp_path), "routing")
    msg = str(exc.value)
    assert "no such extractor is registered" in msg
    assert "worse than not running at all" in msg
    assert "Runnable today:" in msg and "java21" in msg
    assert "routing" not in runnable_phases()


def test_glob_hits_and_selector_hits_are_deduplicated(tmp_path):
    """A class both matched by a glob and named by a selector appears once."""
    make_module(tmp_path, java=("LoggingFilter.java",))
    scan = scan_java_files(str(tmp_path), WEBAPP)
    assert len(scan.files) == len(set(scan.files))
