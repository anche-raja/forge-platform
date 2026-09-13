"""Rendering an extracted context into a bounded, deterministic prompt block."""

import pytest

from forge.context.render import BEGIN, END, context_digest, render_context, section_priority


def _ctx(**overrides):
    base = {
        "extractor": "web_bootstrap", "version": 1, "module_dir": "/abs/mod", "module_rel": "mod",
        "web_xml": {"path": "mod/WEB-INF/web.xml", "filters": [{"name": "a", "order": 1}],
                    "ids": {"ResourceRef_1": "resource-ref"}, "raw_unmapped": [{"element": "module-name", "text": "x"}]},
        "filter_chain": [{"filter_name": "a", "url_patterns": ["/*"], "order": 2}],
        "vendors": [], "servlet_components": [], "unresolved_classes": [],
        "datasources": [{"jndi_name": "jdbc/x", "kind": "resource-ref"}],
        "ear": None, "existing_server_xml": None,
        "authz": {"security_constraints": [], "login_config": None},
        "summary": {"counts": {"filters": 1}, "notes": ["one note"]},
    }
    base.update(overrides)
    return base


def test_render_is_deterministic_for_equal_input():
    a = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    b = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    assert a == b
    assert a.startswith(BEGIN.format(name="web_bootstrap")) and a.endswith(END)
    assert "target: /m/web.xml" in a and "module: mod" in a


def test_sections_render_as_yaml_with_headers_and_raw_unmapped_survives():
    out = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    assert "## web_xml" in out and "## filter_chain" in out and "## datasources" in out
    assert "module-name" in out, "raw_unmapped is the 'nothing dropped' guarantee and must render"
    assert "jdbc/x" in out


def test_internal_and_duplicate_sections_are_not_rendered():
    out = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    assert "## authz" not in out, "authz restates web_xml sections"
    assert "ResourceRef_1" not in out, "ids only serve .xmi href resolution"
    assert "## extractor" not in out and "## module_dir" not in out


def test_priority_differs_for_a_generated_server_xml_target():
    assert section_priority("/m/web.xml")[:2] == ("web_xml", "filter_chain")
    assert section_priority("/m/src/main/liberty/config/server.xml")[:2] == ("datasources", "existing_server_xml")
    edit = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    gen = render_context("web_bootstrap", _ctx(), for_target="/m/server.xml")
    assert edit.index("## web_xml") < edit.index("## datasources")
    assert gen.index("## datasources") < gen.index("## web_xml")


def test_cap_omits_lower_priority_sections_with_a_notice_and_keeps_summary():
    big = _ctx(datasources=[{"jndi_name": f"jdbc/ds{i}", "kind": "resource-ref"} for i in range(400)])
    out = render_context("web_bootstrap", big, for_target="/m/web.xml", max_chars=2500)
    assert "## web_xml\n" in out, "the top-priority section is rendered in full"
    assert "## datasources: OMITTED (" in out and "migration-context.json" in out
    assert "## summary" in out and "one note" in out, "summary is never omitted"
    assert out.endswith(END)
    assert len(out) <= 2500, "the cap is a guarantee: notices and footer are budgeted"


def test_once_omitting_every_later_section_is_omitted_even_if_small():
    big = _ctx(filter_chain=[{"filter_name": f"f{i}", "url_patterns": ["/*"], "order": i} for i in range(300)])
    out = render_context("web_bootstrap", big, for_target="/m/web.xml", max_chars=2000)
    assert "## filter_chain: OMITTED" in out
    # vendors is tiny but comes after filter_chain in priority — still omitted,
    # so the reader knows the block is a prefix, not a selection.
    assert "## vendors: OMITTED" in out


def test_top_section_alone_over_cap_is_hard_truncated_with_a_marker():
    big = _ctx(web_xml={"path": "p", "filters": [{"name": "f" * 50, "order": i} for i in range(200)]})
    out = render_context("web_bootstrap", big, for_target="/m/web.xml", max_chars=1500)
    assert "## web_xml" in out and "[TRUNCATED at" in out
    assert "## filter_chain: OMITTED" in out
    assert out.endswith(END)


def test_digest_is_stable_and_changes_with_content():
    a = render_context("web_bootstrap", _ctx(), for_target="/m/web.xml")
    b = render_context("web_bootstrap", _ctx(summary={"counts": {}, "notes": ["different"]}), for_target="/m/web.xml")
    assert context_digest(a) == context_digest(a)
    assert context_digest(a) != context_digest(b)
    assert len(context_digest(a)) == 64


def test_unknown_extra_sections_render_after_the_priority_list():
    out = render_context("web_bootstrap", _ctx(zeta_extra={"k": "v"}), for_target="/m/web.xml")
    assert "## zeta_extra" in out
    assert out.index("## zeta_extra") > out.index("## existing_server_xml")
