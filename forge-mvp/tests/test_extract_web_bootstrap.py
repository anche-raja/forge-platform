"""The web_bootstrap extractor: deterministic parsing of the web-tier descriptor set.

Fixtures are generic (com.acme) and deliberately cover what any one real project
would not: every Servlet namespace, all three vendor descriptor families in both
XML and EMF .xmi form, EAR, WildFly/Tomcat/persistence datasource sources, and a
lookup() call that must NOT be mistaken for JNDI. A real project can be pointed
at via FORGE_SAMPLE_WEBAPP for a smoke check at the end.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from forge.extract import (
    EXTRACTORS,
    Selection,
    clear_context_cache,
    get_context,
    get_extractor,
)
from forge.extract.selectors import SERVER_XML_REL, is_generated_target, server_config, servlet_components
from forge.extract.web_bootstrap import find_modules, module_for, parse_xml, run, strip_ns

FIXTURES = Path(__file__).parent / "fixtures" / "web_bootstrap"


# ─── layout helper ────────────────────────────────────────────────────────────

def make_module(root: Path, *, web_xml="webapp31-web.xml", vendors=(), java=(), extras=(), pom=True,
                name="orders") -> Path:
    """Lay fixture files out as a Maven web module and return the module dir.

    ``extras`` are (fixture_name, relative_destination) pairs.
    """
    mod = root / name
    web_inf = mod / "src/main/webapp/WEB-INF"
    web_inf.mkdir(parents=True)
    if pom:
        (mod / "pom.xml").write_text("<project><packaging>war</packaging></project>", encoding="utf-8")
    shutil.copy(FIXTURES / web_xml, web_inf / "web.xml")
    for v in vendors:
        shutil.copy(FIXTURES / v, web_inf / v)
    for j in java:
        src = (FIXTURES / j).read_text(encoding="utf-8")
        pkg = next(l for l in src.splitlines() if l.startswith("package ")).split()[1].rstrip(";")
        dest = mod / "src/main/java" / pkg.replace(".", "/") / j
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src, encoding="utf-8")
    for fixture, rel in extras:
        dest = mod / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, dest)
    return mod


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_context_cache()
    yield
    clear_context_cache()


@pytest.fixture
def full_module(tmp_path):
    return make_module(
        tmp_path,
        vendors=("jboss-web.xml",),
        java=("DataSourceConfig.java", "LoggingFilter.java", "StartupListener.java",
              "AuditFilter.java", "SessionCleanup.java", "CodeTable.java"),
        extras=(("server.xml", "src/main/liberty/config/server.xml"),
                ("persistence.xml", "src/main/resources/META-INF/persistence.xml"),
                ("context.xml", "src/main/webapp/META-INF/context.xml"),
                ("orders-ds.xml", "src/main/webapp/WEB-INF/orders-ds.xml")),
    )


# ─── registry ─────────────────────────────────────────────────────────────────

def test_extractor_is_registered_with_both_selectors():
    ext = get_extractor("web_bootstrap")
    assert ext is not None and ext is EXTRACTORS["web_bootstrap"]
    assert ext.provides("servlet_components") and ext.provides("server_config")
    assert not ext.provides("struts_actions")


def test_get_context_caches_per_module(tmp_path, full_module):
    a = get_context("web_bootstrap", str(tmp_path), str(full_module))
    b = get_context("web_bootstrap", str(tmp_path), str(full_module))
    assert a.data is b.data
    clear_context_cache()
    c = get_context("web_bootstrap", str(tmp_path), str(full_module))
    assert c.data is not a.data and c.data == a.data


def test_unknown_context_name_is_a_key_error(tmp_path):
    with pytest.raises(KeyError, match="no extractor registered"):
        get_context("routing_table_of_doom", str(tmp_path), str(tmp_path))


# ─── web.xml: order and completeness ──────────────────────────────────────────

def test_declaration_order_is_preserved(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    assert [f["name"] for f in w["filters"]] == ["springSecurityFilterChain", "loggingFilter", "assetCache", "struts2"]
    assert [l["class"].rsplit(".", 1)[-1] for l in w["listeners"]] == ["ContextLoaderListener", "StartupListener"]
    orders = [f["order"] for f in w["filters"]]
    assert orders == sorted(orders)
    # Filters and their mappings interleave in the document; order numbers reflect that.
    assert w["filter_mappings"][0]["order"] > w["filters"][0]["order"]


def test_filter_chain_flattens_mappings_in_order_with_dispatchers(tmp_path, full_module):
    chain = run(str(tmp_path), str(full_module))["filter_chain"]
    assert [(c["filter_name"], c["url_patterns"]) for c in chain] == [
        ("springSecurityFilterChain", ["/*"]),
        ("loggingFilter", ["/*"]),
        ("assetCache", ["/js/*"]),
        ("assetCache", ["/css/*"]),
        ("struts2", ["/*"]),
    ]
    assert chain[0]["dispatchers"] == ["REQUEST", "ERROR"]
    assert chain[0]["class"].endswith("DelegatingFilterProxy")


def test_context_params_and_init_params_are_captured_verbatim_and_trimmed(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    params = {p["name"]: p["value"] for p in w["context_params"]}
    assert params["contextClass"] == "org.springframework.web.context.support.AnnotationConfigWebApplicationContext"
    assert w["filters"][1]["init_params"] == {"level": "INFO"}
    assert w["servlets"][0]["init_params"]["contextConfigLocation"] == "com.acme.orders.config.MvcConfig"
    assert w["servlets"][0]["load_on_startup"] == 1
    assert w["servlet_mappings"][0]["url_patterns"] == ["/api/*"]


def test_session_error_pages_welcome_and_refs(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    assert w["session_config"]["timeout"] == 30
    assert w["session_config"]["cookie_config"] == {"http-only": "true", "secure": "true"}
    assert w["session_config"]["tracking_modes"] == ["COOKIE"]
    assert w["welcome_files"] == ["index.jsp"]
    assert [(e["code"], e["exception_type"]) for e in w["error_pages"]] == [("404", ""), ("", "java.lang.Throwable")]
    assert w["resource_refs"][0]["name"] == "jdbc/ordersDS"
    assert w["resource_refs"][0]["id"] == "ResourceRef_1"
    assert w["env_entries"][0] == {"name": "feature/newCheckout", "type": "java.lang.Boolean", "value": "false",
                                   "order": w["env_entries"][0]["order"]}


def test_security_constraint_and_login_config(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    sc = w["security_constraints"][0]
    assert sc["web_resources"][0]["url_patterns"] == ["/admin/*"]
    assert sc["web_resources"][0]["http_methods"] == ["GET", "POST"]
    assert sc["roles"] == ["admin"] and sc["transport"] == "CONFIDENTIAL"
    assert w["login_config"]["auth_method"] == "FORM"
    assert w["login_config"]["form_login_page"] == "/login.jsp"
    assert w["security_roles"] == ["admin"]


def test_unknown_top_level_elements_land_in_raw_unmapped_not_dropped(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    names = {r["element"] for r in w["raw_unmapped"]}
    assert {"module-name", "deny-uncovered-http-methods"} <= names
    module_name = next(r for r in w["raw_unmapped"] if r["element"] == "module-name")
    assert module_name["text"] == "acme-orders"


def test_ids_are_indexed_for_xmi_href_resolution(tmp_path, full_module):
    w = run(str(tmp_path), str(full_module))["web_xml"]
    assert w["ids"]["ResourceRef_1"] == "resource-ref"
    assert w["ids"]["SecurityRole_1"] == "security-role"
    assert w["ids"]["WebApp_ID"] == "web-app"


@pytest.mark.parametrize("fixture,version,ns_fragment,first_filter", [
    ("webapp31-web.xml", "3.1", "xmlns.jcp.org", "springSecurityFilterChain"),
    ("jakarta-web.xml", "6.0", "jakarta.ee", "audit"),
    ("servlet24-web.xml", "2.4", "java.sun.com", "audit"),
    ("servlet23-web.xml", "2.3-dtd", None, "audit"),
])
def test_every_servlet_namespace_parses_the_same_way(tmp_path, fixture, version, ns_fragment, first_filter):
    mod = make_module(tmp_path, web_xml=fixture, java=("LoggingFilter.java",))
    ctx = run(str(tmp_path), str(mod))
    w = ctx["web_xml"]
    assert w["schema_version"] == version
    assert (ns_fragment in (w["namespace"] or "")) if ns_fragment else w["namespace"] is None
    assert w["filters"][0]["name"] == first_filter
    assert ctx["filter_chain"][0]["url_patterns"] == ["/*"]
    assert w["parse_error"] is None


def test_servlet23_top_level_taglib_is_folded_into_jsp_config(tmp_path):
    mod = make_module(tmp_path, web_xml="servlet23-web.xml")
    w = run(str(tmp_path), str(mod))["web_xml"]
    assert w["jsp_config"]["taglibs"] == [{"uri": "/acme", "location": "/WEB-INF/acme.tld"}]


def test_malformed_web_xml_yields_parse_error_not_exception(tmp_path):
    mod = make_module(tmp_path)
    (mod / "src/main/webapp/WEB-INF/web.xml").write_text("<web-app><filter>", encoding="utf-8")
    ctx = run(str(tmp_path), str(mod))
    assert ctx["web_xml"]["parse_error"] and "not well-formed" in ctx["web_xml"]["parse_error"]
    assert ctx["web_xml"]["parse_error"] in ctx["summary"]["notes"]
    assert ctx["filter_chain"] == []


def test_module_without_web_xml_is_a_value_error(tmp_path):
    (tmp_path / "lib").mkdir()
    with pytest.raises(ValueError, match="no WEB-INF/web.xml"):
        run(str(tmp_path), str(tmp_path / "lib"))


# ─── vendor descriptors ───────────────────────────────────────────────────────

def test_jboss_web_context_root_security_domain_and_parent_last(tmp_path):
    mod = make_module(tmp_path, vendors=("jboss-web.xml",))
    v = run(str(tmp_path), str(mod))["vendors"][0]
    assert v["kind"] == "jboss" and v["format"] == "xml"
    assert v["context_root"] == "/orders"
    assert v["security_domain"] == "java:/jaas/acme"
    assert v["classloader_policy"] == "parent-last"
    assert v["resource_ref_bindings"] == [{"name": "jdbc/ordersDS", "jndi": "java:jboss/datasources/OrdersDS"}]
    assert v["security_role_bindings"][0]["principals"] == ["acme-admins"]
    assert any(r["element"] == "max-active-sessions" for r in v["raw_unmapped"])


def test_weblogic_prefer_web_inf_classes_and_timeout_unit(tmp_path):
    mod = make_module(tmp_path, vendors=("weblogic.xml",))
    v = run(str(tmp_path), str(mod))["vendors"][0]
    assert v["kind"] == "weblogic"
    assert v["classloader_policy"] == "parent-last"
    assert v["prefer_application_packages"] == ["org.slf4j.*"]
    assert (v["session_timeout"], v["session_timeout_unit"]) == (1800, "seconds")
    assert v["resource_ref_bindings"] == [{"name": "jdbc/ordersDS", "jndi": "OrdersDS"}]
    assert any(r["element"] == "jsp-descriptor" for r in v["raw_unmapped"])


def test_ibm_xml_bindings_and_extensions(tmp_path):
    mod = make_module(tmp_path, vendors=("ibm-web-bnd.xml", "ibm-web-ext.xml"))
    vendors = run(str(tmp_path), str(mod))["vendors"]
    bnd = next(v for v in vendors if v["path"].endswith("ibm-web-bnd.xml"))
    ext = next(v for v in vendors if v["path"].endswith("ibm-web-ext.xml"))
    assert bnd["virtual_host"] == "default_host"
    assert bnd["resource_ref_bindings"] == [{"name": "jdbc/ordersDS", "jndi": "jdbc/OrdersDS"}]
    assert bnd["security_role_bindings"][0] == {"role": "admin", "groups": ["acme-admins"], "users": ["svc-orders"], "principals": []}
    assert ext["context_root"] == "/orders"
    assert ext["ext"]["serve_servlets_by_classname_enabled"] is False
    assert ext["ext"]["default_error_page"] == "/errors/default.jsp"


def test_ibm_xmi_resolves_hrefs_through_web_xml_ids_and_flags_the_unresolved(tmp_path):
    mod = make_module(tmp_path, vendors=("ibm-web-bnd.xmi", "ibm-web-ext.xmi"))
    ctx = run(str(tmp_path), str(mod))
    bnd = next(v for v in ctx["vendors"] if v["path"].endswith("bnd.xmi"))
    ext = next(v for v in ctx["vendors"] if v["path"].endswith("ext.xmi"))
    assert bnd["format"] == "xmi" and bnd["virtual_host"] == "default_host"
    resolved = next(b for b in bnd["resource_ref_bindings"] if b["jndi"] == "jdbc/OrdersDS")
    assert resolved["name"] == "resource-ref#ResourceRef_1"
    ghost = next(b for b in bnd["resource_ref_bindings"] if b["jndi"] == "jdbc/GhostDS")
    assert ghost["name"].endswith("#ResourceRef_99")
    assert any("ResourceRef_99" in n for n in ctx["summary"]["notes"])
    assert bnd["security_role_bindings"][0]["role"] == "SecurityRole_1"
    assert bnd["security_role_bindings"][0]["groups"] == ["acme-admins"]
    assert ext["context_root"] == "/legacy"
    assert ext["ext"]["serve_servlets_by_classname_enabled"] is True
    assert any("serveServletsByClassname" in n for n in ctx["summary"]["notes"])
    # An attribute the table does not know is kept, not dropped.
    assert any(r["attrib"].get("autoRequestEncoding") == "true" for r in ext["raw_unmapped"])
    assert any(r["element"] == "jspAttributes" for r in ext["raw_unmapped"])


def test_multiple_vendor_descriptors_are_all_kept(tmp_path):
    mod = make_module(tmp_path, vendors=("jboss-web.xml", "weblogic.xml", "ibm-web-bnd.xml"))
    kinds = [v["kind"] for v in run(str(tmp_path), str(mod))["vendors"]]
    assert kinds == ["jboss", "weblogic", "websphere"]


# ─── EAR, Liberty, datasources ────────────────────────────────────────────────

def test_ear_application_xml_modules_and_library_directory(tmp_path):
    mod = make_module(tmp_path)
    ear_dir = tmp_path / "ear/src/main/application/META-INF"
    ear_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "application.xml", ear_dir / "application.xml")
    ear = run(str(tmp_path), str(mod))["ear"]
    assert ear["display_name"] == "acme-ear"
    assert ear["modules"] == [
        {"type": "web", "uri": "orders.war", "context_root": "/orders"},
        {"type": "ejb", "uri": "orders-ejb.jar", "context_root": ""},
    ]
    assert ear["library_directory"] == "lib" and ear["security_roles"] == ["admin"]


def test_ear_context_root_is_recovered_from_the_ear_pom_when_application_xml_is_generated(tmp_path):
    mod = make_module(tmp_path)
    ear = tmp_path / "ear"
    ear.mkdir()
    (ear / "pom.xml").write_text(
        "<project><packaging>ear</packaging><build><plugins><plugin><configuration><modules>"
        "<webModule><artifactId>orders</artifactId><contextRoot>/orders</contextRoot></webModule>"
        "</modules></configuration></plugin></plugins></build></project>", encoding="utf-8")
    # A generated application.xml under target/ must be ignored.
    gen = ear / "target/application/META-INF"
    gen.mkdir(parents=True)
    shutil.copy(FIXTURES / "application.xml", gen / "application.xml")
    ctx = run(str(tmp_path), str(mod))
    assert ctx["ear"]["path"] == ""
    assert ctx["ear"]["pom_context_roots"] == [{"artifact_id": "orders", "context_root": "/orders", "pom": "ear/pom.xml"}]


def test_existing_liberty_server_xml_carries_pool_sizes_and_masks_literal_secrets(tmp_path, full_module):
    lib = run(str(tmp_path), str(full_module))["existing_server_xml"]
    assert lib["features"] == ["servlet-3.1", "jsp-2.3", "jdbc-4.1"]
    ds = lib["datasources"][0]
    assert ds["jndi_name"] == "jdbc/ordersDS"
    assert ds["pool"]["maxPoolSize"] == "40" and ds["pool"]["minPoolSize"] == "4"
    assert ds["properties"]["password"] == "${db.password}", "variable references are config, not secrets"
    assert lib["applications"][0]["context_root"] == "/orders"
    assert lib["applications"][0]["classloader"] == {"delegation": "parentLast"}
    assert lib["registries"][0]["kind"] == "basicRegistry"
    keystore = next(o for o in lib["other"] if o["element"] == "keyStore")
    assert keystore["attrib"]["password"] == "***"


def test_datasources_are_the_union_of_every_source(tmp_path, full_module):
    ds = run(str(tmp_path), str(full_module))["datasources"]
    by_kind = {}
    for d in ds:
        by_kind.setdefault(d["normalized"], set()).add(d["kind"])
    assert by_kind["jdbc/ordersDS"] >= {"resource-ref", "liberty-server-xml", "persistence-unit", "tomcat-context", "java-literal"}
    assert "jdbc/OrdersDS" in by_kind or "java:jboss/datasources/OrdersDS" in by_kind  # vendor binding
    assert "jms/orderQueue" in by_kind and "java-literal" in by_kind["jms/orderQueue"]
    tomcat = next(d for d in ds if d["kind"] == "tomcat-context")
    assert tomcat["pool"] == {"maxTotal": "25", "maxIdle": "5", "minIdle": "2"}
    wildfly = next(d for d in ds if d["kind"] == "wildfly-ds")
    assert wildfly["pool"] == {"min-pool-size": "4", "max-pool-size": "40"}


def test_java_comp_env_prefix_is_normalised(tmp_path, full_module):
    ds = run(str(tmp_path), str(full_module))["datasources"]
    pu = next(d for d in ds if d["kind"] == "persistence-unit")
    assert pu["jndi_name"] == "java:comp/env/jdbc/ordersDS" and pu["normalized"] == "jdbc/ordersDS"


def test_code_table_lookup_is_not_mistaken_for_jndi(tmp_path, full_module):
    """registry.lookup("0100") is a code-table read, not a resource. A JNDI name
    has a scheme or a path; anything else from lookup() is noise."""
    names = {d["jndi_name"] for d in run(str(tmp_path), str(full_module))["datasources"]}
    assert "0100" not in names and "0500" not in names
    assert "java:comp/env/jms/orderQueue" in names


def test_jndi_helper_usage_is_noted(tmp_path, full_module):
    notes = run(str(tmp_path), str(full_module))["summary"]["notes"]
    assert any("JndiDataSourceLookup" in n and "InitialContext" in n for n in notes)


# ─── servlet components ───────────────────────────────────────────────────────

def test_declared_classes_resolve_to_source_files_and_library_classes_are_unresolved(tmp_path, full_module):
    ctx = run(str(tmp_path), str(full_module))
    declared = {c["class"].rsplit(".", 1)[-1]: c for c in ctx["servlet_components"] if c["declared_in_web_xml"]}
    assert set(declared) == {"LoggingFilter", "StartupListener"}
    assert declared["LoggingFilter"]["kind"] == "filter" and declared["LoggingFilter"]["via"] == "web.xml"
    assert declared["LoggingFilter"]["file"].endswith("com/acme/orders/web/LoggingFilter.java")
    unresolved = {u["class"].rsplit(".", 1)[-1] for u in ctx["unresolved_classes"]}
    # Library classes have no source here; AssetCacheFilter is a project class the
    # fixture declares but never provides — declared-but-missing is exactly what
    # "unresolved" must surface, not hide.
    assert unresolved == {"DelegatingFilterProxy", "StrutsPrepareAndExecuteFilter", "ContextLoaderListener",
                          "DispatcherServlet", "AssetCacheFilter"}


def test_annotation_and_interface_components_not_in_web_xml_are_found(tmp_path, full_module):
    ctx = run(str(tmp_path), str(full_module))
    extra = {c["class"].rsplit(".", 1)[-1]: c for c in ctx["servlet_components"] if not c["declared_in_web_xml"]}
    assert set(extra) == {"AuditFilter", "SessionCleanup"}
    assert extra["AuditFilter"]["via"] == "annotation" and extra["AuditFilter"]["kind"] == "filter"
    assert extra["SessionCleanup"]["via"] == "interface" and extra["SessionCleanup"]["kind"] == "listener"


def test_sibling_module_classes_resolve_and_are_marked(tmp_path):
    mod = make_module(tmp_path)
    common = tmp_path / "common/src/main/java/com/acme/orders/web"
    common.mkdir(parents=True)
    (tmp_path / "common/pom.xml").write_text("<project/>", encoding="utf-8")
    shutil.copy(FIXTURES / "LoggingFilter.java", common / "LoggingFilter.java")
    ctx = run(str(tmp_path), str(mod))
    lf = next(c for c in ctx["servlet_components"] if c["class"].endswith("LoggingFilter"))
    assert lf["via"] == "web.xml (sibling module)"
    assert "common/" in lf["rel_path"]


def test_excluded_dirs_and_test_sources_are_skipped(tmp_path):
    mod = make_module(tmp_path, java=("LoggingFilter.java",))
    for rel in ("target/classes/com/acme/orders/web", "src/test/java/com/acme/orders/web"):
        d = mod / rel
        d.mkdir(parents=True)
        shutil.copy(FIXTURES / "AuditFilter.java", d / "AuditFilter.java")
    ctx = run(str(tmp_path), str(mod))
    assert not any(c["class"].endswith("AuditFilter") for c in ctx["servlet_components"])


# ─── modules ──────────────────────────────────────────────────────────────────

def test_module_for_prefers_nearest_build_file_then_webapp_root(tmp_path):
    maven = make_module(tmp_path, name="maven")
    assert module_for(str(maven / "src/main/webapp/WEB-INF/web.xml"), str(tmp_path)) == str(maven.resolve())
    gradle = make_module(tmp_path, name="gradle", pom=False)
    (gradle / "build.gradle.kts").write_text("", encoding="utf-8")
    assert module_for(str(gradle / "src/main/webapp/WEB-INF/web.xml"), str(tmp_path)) == str(gradle.resolve())
    bare = make_module(tmp_path, name="bare", pom=False)
    assert module_for(str(bare / "src/main/webapp/WEB-INF/web.xml"), str(tmp_path)) == str(bare.resolve())
    flat = tmp_path / "flat/WEB-INF"
    flat.mkdir(parents=True)
    (flat / "web.xml").write_text("<web-app/>", encoding="utf-8")
    assert module_for(str(flat / "web.xml"), str(tmp_path)) == str((tmp_path / "flat").resolve())


def test_find_modules_lists_each_webapp_once_and_skips_target(tmp_path):
    a = make_module(tmp_path, name="a")
    b = make_module(tmp_path, name="b")
    stale = tmp_path / "a/target/a-1.0/WEB-INF"
    stale.mkdir(parents=True)
    shutil.copy(FIXTURES / "webapp31-web.xml", stale / "web.xml")
    assert find_modules(str(tmp_path)) == (str(a.resolve()), str(b.resolve()))


def test_multiple_web_xml_in_one_module_prefers_the_conventional_one_and_notes_it(tmp_path):
    mod = make_module(tmp_path)
    overlay = mod / "overlay/WEB-INF"
    overlay.mkdir(parents=True)
    shutil.copy(FIXTURES / "jakarta-web.xml", overlay / "web.xml")
    ctx = run(str(tmp_path), str(mod))
    assert ctx["web_xml"]["path"].endswith("src/main/webapp/WEB-INF/web.xml")
    assert ctx["web_xml"]["schema_version"] == "3.1"
    assert any("2 web.xml files" in n for n in ctx["summary"]["notes"])
    assert len(ctx["summary"]["web_xml_candidates"]) == 2


def test_metadata_complete_true_is_noted(tmp_path):
    mod = make_module(tmp_path)
    p = mod / "src/main/webapp/WEB-INF/web.xml"
    p.write_text(p.read_text(encoding="utf-8").replace('metadata-complete="false"', 'metadata-complete="true"'), encoding="utf-8")
    ctx = run(str(tmp_path), str(mod))
    assert ctx["web_xml"]["metadata_complete"] is True
    assert any("metadata-complete=true" in n for n in ctx["summary"]["notes"])


def test_mapping_to_undeclared_filter_is_noted(tmp_path):
    mod = make_module(tmp_path, web_xml="jakarta-web.xml")
    p = mod / "src/main/webapp/WEB-INF/web.xml"
    p.write_text(p.read_text(encoding="utf-8").replace("<filter-name>audit</filter-name><url-pattern>",
                                                       "<filter-name>ghost</filter-name><url-pattern>"), encoding="utf-8")
    notes = run(str(tmp_path), str(mod))["summary"]["notes"]
    assert any("undeclared filter 'ghost'" in n for n in notes)


# ─── contract ─────────────────────────────────────────────────────────────────

def test_output_is_json_serialisable_and_stable_across_runs(tmp_path, full_module):
    first = run(str(tmp_path), str(full_module))
    clear_context_cache()
    second = run(str(tmp_path), str(full_module))
    assert json.loads(json.dumps(first)) == first
    assert first == second


def test_every_top_level_key_is_always_present_even_for_a_minimal_module(tmp_path):
    mod = make_module(tmp_path, web_xml="jakarta-web.xml")
    ctx = run(str(tmp_path), str(mod))
    assert set(ctx) >= {"web_xml", "vendors", "ear", "existing_server_xml", "datasources",
                        "servlet_components", "unresolved_classes", "filter_chain", "authz", "summary"}
    assert ctx["vendors"] == [] and ctx["ear"] is None and ctx["existing_server_xml"] is None
    assert ctx["authz"]["security_constraints"] == [] and ctx["authz"]["login_config"] is None


def test_strip_ns_and_parse_xml_helpers(tmp_path):
    assert strip_ns("{http://x}filter") == "filter" and strip_ns("filter") == "filter"
    root, err = parse_xml(FIXTURES / "servlet23-web.xml")
    assert root is not None and err is None
    root, err = parse_xml(tmp_path / "missing.xml")
    assert root is None and "unreadable" in err


# ─── selectors ────────────────────────────────────────────────────────────────

def test_servlet_components_selector_returns_sorted_unique_source_files(tmp_path, full_module):
    ctx = run(str(tmp_path), str(full_module))
    sel = servlet_components(ctx, str(full_module))
    assert isinstance(sel, Selection) and sel.generated == ()
    assert sel.files == tuple(sorted(sel.files)) and len(sel.files) == len(set(sel.files))
    assert {Path(f).name for f in sel.files} == {"LoggingFilter.java", "StartupListener.java", "AuditFilter.java", "SessionCleanup.java"}


def test_server_config_selector_yields_one_generated_target_per_module(tmp_path, full_module):
    sel = server_config({}, str(full_module))
    assert sel.files == () and sel.generated == (str(full_module / SERVER_XML_REL),)


def test_is_generated_target_only_for_a_missing_server_xml_on_a_server_config_pack():
    class Spec:
        selectors = ("server_config",)

    class Other:
        selectors = ("servlet_components",)

    assert is_generated_target(Spec(), "/nowhere/src/main/liberty/config/server.xml")
    assert not is_generated_target(Other(), "/nowhere/src/main/liberty/config/server.xml")
    assert not is_generated_target(Spec(), "/nowhere/web.xml")
    assert not is_generated_target(Spec(), str(FIXTURES / "server.xml")), "an existing file is edited, not generated"


# ─── any real project ─────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("FORGE_SAMPLE_WEBAPP"), reason="set FORGE_SAMPLE_WEBAPP=<a web module> to smoke-test a real project")
def test_a_real_web_module_extracts_cleanly():
    module = Path(os.environ["FORGE_SAMPLE_WEBAPP"]).resolve()
    source = Path(os.environ.get("FORGE_SAMPLE_ROOT", module)).resolve()
    ctx = run(str(source), str(module))
    json.dumps(ctx)
    w = ctx["web_xml"]
    assert w["parse_error"] is None
    assert w["filters"] or w["servlets"] or w["listeners"], "a web module declares something"
    orders = [c["order"] for c in ctx["filter_chain"]]
    assert orders == sorted(orders)
    declared = {r["name"] for r in w["resource_refs"]}
    seen = {d["jndi_name"] for d in ctx["datasources"]}
    assert declared <= seen, "every resource-ref must surface as a datasource"
