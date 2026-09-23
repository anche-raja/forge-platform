"""``web_bootstrap`` — the parsed web-tier descriptors of one module.

Everything a Jakarta EE bootstrap migration has to preserve lives in XML that no
single Java file references: the filter chain and its order, the servlet
mappings, security constraints, resource references, and whatever the vendor
descriptor (JBoss, WebLogic, WebSphere) added on top. This extractor parses
that set deterministically, in declaration order, and drops nothing — an
element the table does not know goes to ``raw_unmapped`` rather than away.

The parser is container- and build-tool-agnostic: it handles every Servlet
namespace (Sun, JCP, Jakarta, the 2.3 DTD), the three vendor descriptor
families in both ``.xml`` and EMF ``.xmi`` forms, EAR ``application.xml``,
WildFly ``*-ds.xml``, Tomcat ``context.xml``, ``persistence.xml``, Liberty
``server.xml``, and Maven or Gradle module layouts.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from forge.extract import Extractor, register
from forge.extract.selectors import server_config, servlet_components
from forge.utils.fs import is_test_path, prune_dirs
from forge.utils.java_checks import declared_package
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

NAME = "web_bootstrap"
VERSION = 1

_MAX_XML_BYTES = 5 * 1024 * 1024
_MAX_JAVA_BYTES = 1 * 1024 * 1024

_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_VENDOR_FILES = (
    ("jboss-web.xml", "jboss"),
    ("weblogic.xml", "weblogic"),
    ("ibm-web-bnd.xml", "websphere"),
    ("ibm-web-bnd.xmi", "websphere"),
    ("ibm-web-ext.xml", "websphere"),
    ("ibm-web-ext.xmi", "websphere"),
)

# Framework classes that show up in web.xml but never live in the project.
# Listed only so they are reported as "unresolved", never searched for.
_SECRET_ATTR = re.compile(r"password|passwd|secret|credential", re.IGNORECASE)


# ─── XML helpers ──────────────────────────────────────────────────────────────

def strip_ns(tag: str) -> str:
    """``{http://…}filter`` → ``filter``. Attribute keys use the same form."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _tag(e: ET.Element) -> str:
    return strip_ns(e.tag)


def _attrs(e: ET.Element) -> Dict[str, str]:
    return {strip_ns(k): v for k, v in e.attrib.items()}


def _text(e: Optional[ET.Element]) -> str:
    if e is None or e.text is None:
        return ""
    return e.text.strip()


def _children(e: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in e if _tag(c) == name]


def _child(e: ET.Element, name: str) -> Optional[ET.Element]:
    for c in e:
        if _tag(c) == name:
            return c
    return None


def _child_text(e: ET.Element, name: str) -> str:
    return _text(_child(e, name))


def _child_texts(e: ET.Element, name: str) -> List[str]:
    return [_text(c) for c in _children(e, name)]


def _bool(value: str) -> Optional[bool]:
    v = value.strip().lower()
    if v in ("true", "yes", "1"):
        return True
    if v in ("false", "no", "0"):
        return False
    return None


def _int(value: str) -> Optional[int]:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _params(e: ET.Element, name: str = "init-param") -> Dict[str, str]:
    return {
        _child_text(p, "param-name"): _child_text(p, "param-value")
        for p in _children(e, name)
    }


def _mask(attrs: Dict[str, str]) -> Dict[str, str]:
    """Hide literal secrets. ``${var}`` references are kept — they are config, not credentials."""
    out = {}
    for k, v in attrs.items():
        if _SECRET_ATTR.search(k) and not v.strip().startswith("${"):
            out[k] = "***"
        else:
            out[k] = v
    return out


def _raw(e: ET.Element, order: Optional[int] = None) -> Dict[str, Any]:
    """An element the table does not know, kept whole rather than dropped."""
    entry: Dict[str, Any] = {
        "element": _tag(e),
        "attrib": _mask(_attrs(e)),
        "text": _text(e),
        "children": [
            {"element": _tag(c), "attrib": _mask(_attrs(c)), "text": _text(c)} for c in e
        ],
    }
    if order is not None:
        entry["order"] = order
    return entry


def parse_xml(path: Path) -> Tuple[Optional[ET.Element], Optional[str]]:
    """Parse one XML file; never raises. Returns ``(root, None)`` or ``(None, reason)``.

    ``xml.etree`` does not fetch external DTDs, so a Servlet 2.3 descriptor with
    a ``<!DOCTYPE …>`` parses offline.
    """
    try:
        if path.stat().st_size > _MAX_XML_BYTES:
            return None, f"{path.name} exceeds {_MAX_XML_BYTES // (1024 * 1024)} MB; not parsed"
        return ET.parse(path).getroot(), None
    except ET.ParseError as e:
        return None, f"{path.name}: not well-formed XML: {e}"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"{path.name}: unreadable: {e}"


# ─── filesystem ───────────────────────────────────────────────────────────────

def _walk(root: Path) -> Iterator[Path]:
    """Files under ``root``, pruning build/VCS dirs and test sources."""
    root = Path(root)
    for dirpath, dirs, files in os.walk(root):
        prune_dirs(dirpath, dirs)
        dirs.sort()
        rel_dir = str(Path(dirpath).relative_to(root)).replace("\\", "/")
        if is_test_path(rel_dir + "/"):
            dirs[:] = []
            continue
        for f in sorted(files):
            yield Path(dirpath) / f


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def module_for(file_path: str, source_dir: str) -> str:
    """The module that owns ``file_path``.

    Nearest ancestor holding a build file wins (Maven or Gradle). Failing that,
    the directory above ``WEB-INF``'s webapp root; failing that, ``source_dir``.
    """
    src = Path(source_dir).resolve()
    p = Path(file_path).resolve()
    start = p if p.is_dir() else p.parent
    for d in (start, *start.parents):
        if any((d / b).is_file() for b in _BUILD_FILES):
            return str(d)
        if d == src:
            break
    # No build file: fall back to the webapp root's owner.
    for d in (start, *start.parents):
        if d.name == "WEB-INF":
            webapp = d.parent
            if webapp.name == "webapp" and webapp.parent.name == "main" and webapp.parent.parent.name == "src":
                return str(webapp.parent.parent.parent)
            return str(webapp)
        if d == src:
            break
    return str(src)


def _web_xml_candidates(module_dir: Path) -> List[Path]:
    found = [p for p in _walk(module_dir) if p.name == "web.xml" and p.parent.name == "WEB-INF"]
    # A conventional layout wins when there are several (e.g. an overlay copy).
    found.sort(key=lambda p: (0 if "src/main/webapp" in _rel(p, module_dir) else 1, str(p)))
    return found


def find_modules(source_dir: str) -> Tuple[str, ...]:
    """Every module under ``source_dir`` that carries a ``WEB-INF/web.xml``."""
    src = Path(source_dir).resolve()
    modules = set()
    for p in _walk(src):
        if p.name == "web.xml" and p.parent.name == "WEB-INF":
            modules.add(module_for(str(p), str(src)))
    return tuple(sorted(modules))


# ─── web.xml ──────────────────────────────────────────────────────────────────

def _parse_web_xml(path: Path, source_dir: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "path": _rel(path, source_dir),
        "schema_version": None,
        "namespace": None,
        "metadata_complete": None,
        "display_name": "",
        "distributable": False,
        "absolute_ordering": [],
        "context_params": [],
        "listeners": [],
        "filters": [],
        "filter_mappings": [],
        "servlets": [],
        "servlet_mappings": [],
        "error_pages": [],
        "welcome_files": [],
        "session_config": None,
        "security_constraints": [],
        "login_config": None,
        "security_roles": [],
        "resource_refs": [],
        "resource_env_refs": [],
        "env_entries": [],
        "ejb_refs": [],
        "data_sources": [],
        "mime_mappings": [],
        "jsp_config": None,
        "locale_encoding_mappings": [],
        "ids": {},
        "raw_unmapped": [],
        "parse_error": None,
    }
    root, err = parse_xml(path)
    if root is None:
        out["parse_error"] = err
        return out

    attrs = _attrs(root)
    out["namespace"] = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else None
    out["schema_version"] = attrs.get("version") or ("2.3-dtd" if out["namespace"] is None else None)
    mc = attrs.get("metadata-complete")
    out["metadata_complete"] = _bool(mc) if mc is not None else None

    for e in root.iter():
        eid = _attrs(e).get("id")
        if eid:
            out["ids"][eid] = _tag(e)

    for order, e in enumerate(root):
        tag = _tag(e)
        if tag == "display-name":
            out["display_name"] = _text(e)
        elif tag == "distributable":
            out["distributable"] = True
        elif tag == "absolute-ordering":
            out["absolute_ordering"] = [_tag(c) if _tag(c) == "others" else _text(c) for c in e]
        elif tag == "context-param":
            out["context_params"].append({
                "name": _child_text(e, "param-name"),
                "value": _child_text(e, "param-value"),
                "order": order,
            })
        elif tag == "listener":
            out["listeners"].append({"class": _child_text(e, "listener-class"), "order": order})
        elif tag == "filter":
            out["filters"].append({
                "name": _child_text(e, "filter-name"),
                "class": _child_text(e, "filter-class"),
                "init_params": _params(e),
                "async_supported": _bool(_child_text(e, "async-supported")),
                "order": order,
            })
        elif tag == "filter-mapping":
            out["filter_mappings"].append({
                "filter_name": _child_text(e, "filter-name"),
                "url_patterns": _child_texts(e, "url-pattern"),
                "servlet_names": _child_texts(e, "servlet-name"),
                "dispatchers": _child_texts(e, "dispatcher"),
                "order": order,
            })
        elif tag == "servlet":
            out["servlets"].append({
                "name": _child_text(e, "servlet-name"),
                "class": _child_text(e, "servlet-class"),
                "jsp_file": _child_text(e, "jsp-file"),
                "init_params": _params(e),
                "load_on_startup": _int(_child_text(e, "load-on-startup")),
                "async_supported": _bool(_child_text(e, "async-supported")),
                "order": order,
            })
        elif tag == "servlet-mapping":
            out["servlet_mappings"].append({
                "servlet_name": _child_text(e, "servlet-name"),
                "url_patterns": _child_texts(e, "url-pattern"),
                "order": order,
            })
        elif tag == "error-page":
            out["error_pages"].append({
                "code": _child_text(e, "error-code"),
                "exception_type": _child_text(e, "exception-type"),
                "location": _child_text(e, "location"),
                "order": order,
            })
        elif tag == "welcome-file-list":
            out["welcome_files"].extend(_child_texts(e, "welcome-file"))
        elif tag == "session-config":
            cookie = _child(e, "cookie-config")
            out["session_config"] = {
                "timeout": _int(_child_text(e, "session-timeout")),
                "cookie_config": {_tag(c): _text(c) for c in cookie} if cookie is not None else {},
                "tracking_modes": _child_texts(e, "tracking-mode"),
                "order": order,
            }
        elif tag == "security-constraint":
            out["security_constraints"].append({
                "name": _child_text(e, "display-name"),
                "web_resources": [
                    {
                        "name": _child_text(w, "web-resource-name"),
                        "url_patterns": _child_texts(w, "url-pattern"),
                        "http_methods": _child_texts(w, "http-method"),
                        "http_method_omissions": _child_texts(w, "http-method-omission"),
                    }
                    for w in _children(e, "web-resource-collection")
                ],
                "roles": [_text(r) for a in _children(e, "auth-constraint") for r in _children(a, "role-name")],
                "auth_constraint_present": _child(e, "auth-constraint") is not None,
                "transport": _child_text(udc, "transport-guarantee") if (udc := _child(e, "user-data-constraint")) is not None else "",
                "order": order,
            })
        elif tag == "login-config":
            form = _child(e, "form-login-config")
            out["login_config"] = {
                "auth_method": _child_text(e, "auth-method"),
                "realm": _child_text(e, "realm-name"),
                "form_login_page": _child_text(form, "form-login-page") if form is not None else "",
                "form_error_page": _child_text(form, "form-error-page") if form is not None else "",
                "order": order,
            }
        elif tag == "security-role":
            out["security_roles"].append(_child_text(e, "role-name"))
        elif tag == "resource-ref":
            out["resource_refs"].append({
                "name": _child_text(e, "res-ref-name"),
                "type": _child_text(e, "res-type"),
                "auth": _child_text(e, "res-auth"),
                "sharing": _child_text(e, "res-sharing-scope"),
                "description": _child_text(e, "description"),
                "lookup": _child_text(e, "lookup-name"),
                "id": _attrs(e).get("id", ""),
                "order": order,
            })
        elif tag == "resource-env-ref":
            out["resource_env_refs"].append({
                "name": _child_text(e, "resource-env-ref-name"),
                "type": _child_text(e, "resource-env-ref-type"),
                "lookup": _child_text(e, "lookup-name"),
                "order": order,
            })
        elif tag == "env-entry":
            out["env_entries"].append({
                "name": _child_text(e, "env-entry-name"),
                "type": _child_text(e, "env-entry-type"),
                "value": _child_text(e, "env-entry-value"),
                "order": order,
            })
        elif tag in ("ejb-ref", "ejb-local-ref"):
            out["ejb_refs"].append({
                "name": _child_text(e, "ejb-ref-name"),
                "kind": tag,
                "type": _child_text(e, "ejb-ref-type"),
                "home": _child_text(e, "home") or _child_text(e, "local-home"),
                "remote": _child_text(e, "remote"),
                "local": _child_text(e, "local"),
                "link": _child_text(e, "ejb-link"),
                "lookup": _child_text(e, "lookup-name"),
                "order": order,
            })
        elif tag == "data-source":
            out["data_sources"].append({
                "name": _child_text(e, "name"),
                "class_name": _child_text(e, "class-name"),
                "url": _child_text(e, "url"),
                "user": _child_text(e, "user"),
                "properties": {_child_text(p, "name"): _child_text(p, "value") for p in _children(e, "property")},
                "order": order,
            })
        elif tag == "mime-mapping":
            out["mime_mappings"].append({
                "extension": _child_text(e, "extension"),
                "mime_type": _child_text(e, "mime-type"),
            })
        elif tag == "jsp-config":
            out["jsp_config"] = {
                "taglibs": [
                    {"uri": _child_text(t, "taglib-uri"), "location": _child_text(t, "taglib-location")}
                    for t in _children(e, "taglib")
                ],
                "property_groups": [
                    {**{_tag(c): _text(c) for c in g if _tag(c) != "url-pattern"},
                     "url_patterns": _child_texts(g, "url-pattern")}
                    for g in _children(e, "jsp-property-group")
                ],
            }
        elif tag == "taglib":  # Servlet 2.3 placed taglib at the top level
            out["jsp_config"] = out["jsp_config"] or {"taglibs": [], "property_groups": []}
            out["jsp_config"]["taglibs"].append({
                "uri": _child_text(e, "taglib-uri"), "location": _child_text(e, "taglib-location"),
            })
        elif tag == "locale-encoding-mapping-list":
            out["locale_encoding_mappings"].extend(
                {"locale": _child_text(m, "locale"), "encoding": _child_text(m, "encoding")}
                for m in _children(e, "locale-encoding-mapping")
            )
        elif tag in ("description", "icon"):
            continue
        else:
            out["raw_unmapped"].append(_raw(e, order))
    return out


# ─── vendor descriptors ───────────────────────────────────────────────────────

def _vendor_base(kind: str, path: Path, source_dir: Path, fmt: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "path": _rel(path, source_dir),
        "format": fmt,
        "context_root": "",
        "classloader_policy": None,
        "prefer_application_packages": [],
        "session_timeout": None,
        "session_timeout_unit": None,
        "virtual_host": "",
        "security_domain": "",
        "resource_ref_bindings": [],
        "security_role_bindings": [],
        "ext": {},
        "raw_unmapped": [],
        "parse_error": None,
    }


def _parse_jboss_web(root: ET.Element, v: Dict[str, Any]) -> None:
    for e in root:
        tag = _tag(e)
        if tag == "context-root":
            v["context_root"] = _text(e)
        elif tag == "security-domain":
            v["security_domain"] = _text(e)
        elif tag == "virtual-host":
            v["virtual_host"] = _text(e)
        elif tag in ("resource-ref", "resource-env-ref"):
            v["resource_ref_bindings"].append({
                "name": _child_text(e, "res-ref-name") or _child_text(e, "resource-env-ref-name"),
                "jndi": _child_text(e, "jndi-name"),
            })
        elif tag == "security-role":
            v["security_role_bindings"].append({
                "role": _child_text(e, "role-name"),
                "groups": [],
                "users": [],
                "principals": _child_texts(e, "principal-name"),
            })
        elif tag == "class-loading":
            compliance = _attrs(e).get("java2ClassLoadingCompliance", "")
            if _bool(compliance) is False:
                v["classloader_policy"] = "parent-last"
            elif _bool(compliance) is True:
                v["classloader_policy"] = "parent-first"
            v["raw_unmapped"].append(_raw(e))
        elif tag == "session-config":
            v["session_timeout"] = _int(_child_text(e, "session-timeout"))
            v["session_timeout_unit"] = "minutes"
        else:
            v["raw_unmapped"].append(_raw(e))


def _parse_weblogic(root: ET.Element, v: Dict[str, Any]) -> None:
    for e in root:
        tag = _tag(e)
        if tag == "context-root":
            v["context_root"] = _text(e)
        elif tag == "virtual-host-name":
            v["virtual_host"] = _text(e)
        elif tag == "container-descriptor":
            if _bool(_child_text(e, "prefer-web-inf-classes")) is True:
                v["classloader_policy"] = "parent-last"
            pap = _child(e, "prefer-application-packages")
            if pap is not None:
                v["prefer_application_packages"] = _child_texts(pap, "package-name")
                if not v["classloader_policy"]:
                    v["classloader_policy"] = "parent-last (selected packages)"
            v["raw_unmapped"].append(_raw(e))
        elif tag == "session-descriptor":
            secs = _int(_child_text(e, "timeout-secs"))
            if secs is not None:
                v["session_timeout"] = secs
                v["session_timeout_unit"] = "seconds"
            v["raw_unmapped"].append(_raw(e))
        elif tag == "resource-description":
            v["resource_ref_bindings"].append({
                "name": _child_text(e, "res-ref-name"),
                "jndi": _child_text(e, "jndi-name"),
            })
        elif tag == "security-role-assignment":
            v["security_role_bindings"].append({
                "role": _child_text(e, "role-name"),
                "groups": [],
                "users": [],
                "principals": _child_texts(e, "principal-name"),
            })
        else:
            v["raw_unmapped"].append(_raw(e))


def _parse_ibm_bnd_xml(root: ET.Element, v: Dict[str, Any]) -> None:
    for e in root:
        tag, a = _tag(e), _attrs(e)
        if tag == "virtual-host":
            v["virtual_host"] = a.get("name", "")
        elif tag in ("resource-ref", "resource-env-ref", "data-source"):
            v["resource_ref_bindings"].append({
                "name": a.get("name", ""), "jndi": a.get("binding-name", ""),
            })
        elif tag == "security-role":
            v["security_role_bindings"].append({
                "role": a.get("name", ""),
                "groups": [_attrs(g).get("name", "") for g in _children(e, "group")],
                "users": [_attrs(u).get("name", "") for u in _children(e, "user")],
                "principals": [_attrs(s).get("type", "") for s in _children(e, "special-subject")],
            })
        else:
            v["raw_unmapped"].append(_raw(e))


_IBM_EXT_FLAGS = {
    "enable-reloading": "reloading_enabled",
    "enable-file-serving": "file_serving_enabled",
    "enable-directory-browsing": "directory_browsing_enabled",
    "enable-serving-servlets-by-class-name": "serve_servlets_by_classname_enabled",
    "reload-interval": "reload_interval",
}


def _parse_ibm_ext_xml(root: ET.Element, v: Dict[str, Any]) -> None:
    for e in root:
        tag, a = _tag(e), _attrs(e)
        if tag == "context-root":
            v["context_root"] = a.get("uri", "")
        elif tag in _IBM_EXT_FLAGS:
            v["ext"][_IBM_EXT_FLAGS[tag]] = _bool(a.get("value", "")) if "value" in a else a
        elif tag == "default-error-page":
            v["ext"]["default_error_page"] = a.get("uri", "")
        else:
            v["raw_unmapped"].append(_raw(e))


_XMI_EXT_ATTRS = {
    "reloadingEnabled": "reloading_enabled",
    "fileServingEnabled": "file_serving_enabled",
    "directoryBrowsingEnabled": "directory_browsing_enabled",
    "serveServletsByClassnameEnabled": "serve_servlets_by_classname_enabled",
    "reloadInterval": "reload_interval",
    "defaultErrorPage": "default_error_page",
    "contextRoot": "context_root",
}


def _href_id(href: str) -> str:
    return href.rsplit("#", 1)[1] if "#" in href else href


def _parse_ibm_xmi(root: ET.Element, v: Dict[str, Any], web_ids: Dict[str, str], notes: List[str]) -> None:
    """Best-effort EMF parse. Known attributes are mapped; the rest is kept whole."""
    a = _attrs(root)
    if "virtualHostName" in a:
        v["virtual_host"] = a["virtualHostName"]
    for key, target in _XMI_EXT_ATTRS.items():
        if key not in a:
            continue
        if target == "context_root":
            v["context_root"] = a[key]
        else:
            flag = _bool(a[key])
            v["ext"][target] = flag if flag is not None else a[key]
    leftover = {k: val for k, val in a.items() if k not in _XMI_EXT_ATTRS and k not in ("virtualHostName", "id", "version")}
    if leftover:
        v["raw_unmapped"].append({"element": _tag(root), "attrib": _mask(leftover), "text": "", "children": []})

    for e in root:
        tag, ea = _tag(e), _attrs(e)
        if tag in ("resRefBindings", "resEnvRefBindings", "dataSource"):
            # Never `a or b` on Elements: one with no children is falsy.
            ref = _child(e, "bindingResourceRef")
            if ref is None:
                ref = _child(e, "bindingResourceEnvRef")
            href = _attrs(ref).get("href", "") if ref is not None else ""
            rid = _href_id(href)
            name = web_ids.get(rid) and f"{web_ids[rid]}#{rid}" or href
            if href and rid not in web_ids:
                notes.append(f"{v['path']}: binding href '{href}' does not resolve to a web.xml id")
            v["resource_ref_bindings"].append({"name": name, "jndi": ea.get("jndiName", ""), "href": href})
        elif tag == "roleAssignments":
            role = _child(e, "role")
            href = _attrs(role).get("href", "") if role is not None else ""
            v["security_role_bindings"].append({
                "role": _href_id(href) or href,
                "groups": [_attrs(g).get("name", "") for g in _children(e, "groups")],
                "users": [_attrs(u).get("name", "") for u in _children(e, "users")],
                "principals": [_attrs(s).get("name", "") or _tag(s) for s in _children(e, "specialSubjects")],
                "href": href,
            })
        elif tag == "webapp":
            continue
        else:
            v["raw_unmapped"].append(_raw(e))


def _parse_vendors(web_inf: Path, source_dir: Path, web_ids: Dict[str, str], notes: List[str]) -> List[Dict[str, Any]]:
    vendors: List[Dict[str, Any]] = []
    for fname, kind in _VENDOR_FILES:
        path = web_inf / fname
        if not path.is_file():
            continue
        fmt = "xmi" if fname.endswith(".xmi") else "xml"
        v = _vendor_base(kind, path, source_dir, fmt)
        root, err = parse_xml(path)
        if root is None:
            v["parse_error"] = err
            vendors.append(v)
            continue
        if kind == "jboss":
            _parse_jboss_web(root, v)
        elif kind == "weblogic":
            _parse_weblogic(root, v)
        elif fmt == "xmi":
            _parse_ibm_xmi(root, v, web_ids, notes)
        elif "bnd" in fname:
            _parse_ibm_bnd_xml(root, v)
        else:
            _parse_ibm_ext_xml(root, v)
        if v["ext"].get("serve_servlets_by_classname_enabled") is True:
            notes.append(f"{v['path']}: serveServletsByClassname is enabled — do not carry across")
        vendors.append(v)
    return vendors


# ─── EAR, datasources, Liberty ────────────────────────────────────────────────

def _parse_ear(source_dir: Path, module_dir: Path) -> Optional[Dict[str, Any]]:
    ear: Optional[Dict[str, Any]] = None
    for p in _walk(source_dir):
        if p.name == "application.xml" and p.parent.name == "META-INF":
            root, err = parse_xml(p)
            ear = {
                "path": _rel(p, source_dir), "display_name": "", "version": None,
                "modules": [], "library_directory": "", "security_roles": [], "parse_error": err,
            }
            if root is None:
                break
            ear["version"] = _attrs(root).get("version")
            ear["display_name"] = _child_text(root, "display-name")
            ear["library_directory"] = _child_text(root, "library-directory")
            ear["security_roles"] = [_child_text(r, "role-name") for r in _children(root, "security-role")]
            for m in _children(root, "module"):
                for c in m:
                    kind = _tag(c)
                    if kind == "web":
                        ear["modules"].append({
                            "type": "web", "uri": _child_text(c, "web-uri"),
                            "context_root": _child_text(c, "context-root"),
                        })
                    elif kind in ("ejb", "java", "connector"):
                        ear["modules"].append({"type": kind, "uri": _text(c), "context_root": ""})
            break
    # An EAR pom often declares the context root even when application.xml is generated.
    pom_roots = []
    for p in _walk(source_dir):
        if p.name != "pom.xml":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<packaging>ear</packaging>" not in text:
            continue
        for m in re.finditer(r"<webModule>(.*?)</webModule>", text, re.DOTALL):
            block = m.group(1)
            art = re.search(r"<artifactId>([^<]+)</artifactId>", block)
            cr = re.search(r"<contextRoot>([^<]+)</contextRoot>", block)
            if art and cr:
                pom_roots.append({
                    "artifact_id": art.group(1).strip(), "context_root": cr.group(1).strip(),
                    "pom": _rel(p, source_dir),
                })
    if pom_roots:
        ear = ear or {"path": "", "display_name": "", "version": None, "modules": [],
                      "library_directory": "", "security_roles": [], "parse_error": None}
        ear["pom_context_roots"] = pom_roots
    elif ear is not None:
        ear["pom_context_roots"] = []
    return ear


def _parse_liberty_server_xml(path: Path, source_dir: Path) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "path": _rel(path, source_dir), "features": [], "applications": [], "datasources": [],
        "libraries": [], "http_endpoints": [], "registries": [], "other": [], "parse_error": None,
    }
    root, err = parse_xml(path)
    if root is None:
        out["parse_error"] = err
        return out
    for e in root:
        tag, a = _tag(e), _attrs(e)
        if tag == "featureManager":
            out["features"].extend(_child_texts(e, "feature"))
        elif tag in ("application", "webApplication", "enterpriseApplication"):
            out["applications"].append({
                "element": tag, "id": a.get("id", ""), "location": a.get("location", ""),
                "name": a.get("name", ""), "type": a.get("type", ""),
                "context_root": a.get("context-root", "") or a.get("contextRoot", ""),
                "classloader": {**_attrs(c)} if (c := _child(e, "classloader")) is not None else None,
            })
        elif tag == "dataSource":
            cm = _child(e, "connectionManager")
            drv = _child(e, "jdbcDriver")
            props = next((c for c in e if _tag(c).startswith("properties")), None)
            out["datasources"].append({
                "id": a.get("id", ""), "jndi_name": a.get("jndiName", ""),
                "attrs": _mask({k: v for k, v in a.items() if k not in ("id", "jndiName")}),
                "driver": _attrs(drv) if drv is not None else {},
                "properties_element": _tag(props) if props is not None else "",
                "properties": _mask(_attrs(props)) if props is not None else {},
                "pool": _attrs(cm) if cm is not None else None,
            })
        elif tag == "library":
            out["libraries"].append({
                "id": a.get("id", ""),
                "filesets": [_attrs(f) for f in _children(e, "fileset")],
                "files": [_attrs(f) for f in _children(e, "file")],
            })
        elif tag == "httpEndpoint":
            out["http_endpoints"].append(_attrs(e))
        elif tag.endswith("Registry"):
            out["registries"].append({"kind": tag, "attrs": _mask(a)})
        else:
            out["other"].append(_raw(e))
    return out


def _find_existing_server_xml(module_dir: Path, source_dir: Path) -> Optional[Dict[str, Any]]:
    for p in _walk(module_dir):
        if p.name != "server.xml":
            continue
        root, _ = parse_xml(p)
        if root is not None and _tag(root) == "server":
            return _parse_liberty_server_xml(p, source_dir)
    return None


def _normalize_jndi(name: str) -> str:
    n = name.strip()
    for prefix in ("java:comp/env/", "java:/comp/env/", "java:global/", "java:app/", "java:module/", "java:/"):
        if n.startswith(prefix):
            return n[len(prefix):]
    return n


_JAVA_JNDI_PATTERNS = (
    re.compile(r'"(java:[^"\s]+)"'),
    re.compile(r'"(jdbc/[^"\s]+)"'),
    re.compile(r'"(jms/[^"\s]+)"'),
    re.compile(r'"(mail/[^"\s]+)"'),
    # Any method called lookup(...) matches here — a code-table lookup("0100")
    # is not a JNDI name — so the value must have JNDI shape: a scheme or a path.
    re.compile(r'\.lookup\(\s*"((?:java:|[\w.-]+/)[^"\s]+)"'),
)
_RESOURCE_ANN = re.compile(r"@Resource\s*\(([^)]*)\)")
_RESOURCE_ARG = re.compile(r'(?:name|lookup|mappedName)\s*=\s*"([^"]+)"')
_JNDI_HELPERS = re.compile(r"\b(JndiDataSourceLookup|JndiObjectFactoryBean|JndiTemplate|InitialContext)\b")


def _java_files(root: Path) -> Iterator[Path]:
    for p in _walk(root):
        if p.suffix == ".java":
            yield p


def _collect_datasources(web: Dict[str, Any], vendors: List[Dict[str, Any]], liberty: Optional[Dict[str, Any]],
                         module_dir: Path, source_dir: Path, notes: List[str]) -> List[Dict[str, Any]]:
    found: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(name: str, source_file: str, kind: str, pool: Optional[Dict[str, str]] = None, **extra):
        name = name.strip()
        if not name:
            return
        key = (_normalize_jndi(name), source_file)
        entry = found.setdefault(key, {
            "jndi_name": name, "normalized": _normalize_jndi(name),
            "source_file": source_file, "kind": kind, "pool": None,
        })
        if pool:
            entry["pool"] = pool
        entry.update(extra)

    for r in web["resource_refs"]:
        add(r["name"], web["path"], "resource-ref", type=r["type"])
    for d in web["data_sources"]:
        add(d["name"], web["path"], "web-xml-data-source")
    for v in vendors:
        for b in v["resource_ref_bindings"]:
            add(b["jndi"], v["path"], "vendor-binding", ref=b["name"])
    if liberty:
        for d in liberty["datasources"]:
            add(d["jndi_name"], liberty["path"], "liberty-server-xml", pool=d["pool"])

    for p in _walk(module_dir):
        if p.name.endswith("-ds.xml"):
            root, _ = parse_xml(p)
            if root is None:
                continue
            for ds in root.iter():
                if _tag(ds) in ("datasource", "xa-datasource"):
                    pool_el = _child(ds, "pool")
                    pool = {_tag(c): _text(c) for c in pool_el} if pool_el is not None else None
                    add(_child_text(ds, "jndi-name") or _attrs(ds).get("jndi-name", ""),
                        _rel(p, source_dir), "wildfly-ds", pool=pool)
        elif p.name == "context.xml" and p.parent.name == "META-INF":
            root, _ = parse_xml(p)
            if root is None:
                continue
            for res in root.iter():
                if _tag(res) == "Resource":
                    a = _attrs(res)
                    pool = {k: v for k, v in a.items() if k in ("maxTotal", "maxActive", "maxIdle", "minIdle", "initialSize", "maxWaitMillis", "maxWait")}
                    add(a.get("name", ""), _rel(p, source_dir), "tomcat-context", pool=pool or None, type=a.get("type", ""))
        elif p.name == "persistence.xml" and p.parent.name == "META-INF":
            root, _ = parse_xml(p)
            if root is None:
                continue
            for pu in _children(root, "persistence-unit"):
                for kind in ("jta-data-source", "non-jta-data-source"):
                    add(_child_text(pu, kind), _rel(p, source_dir), "persistence-unit",
                        unit=_attrs(pu).get("name", ""), ref_kind=kind)

    helpers_seen = set()
    for p in _java_files(module_dir):
        try:
            if p.stat().st_size > _MAX_JAVA_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(p, source_dir)
        for pat in _JAVA_JNDI_PATTERNS:
            for m in pat.finditer(text):
                add(m.group(1), rel, "java-literal")
        for m in _RESOURCE_ANN.finditer(text):
            for n in _RESOURCE_ARG.findall(m.group(1)):
                add(n, rel, "java-resource-annotation")
        for h in _JNDI_HELPERS.findall(text):
            helpers_seen.add(h)
    if helpers_seen:
        notes.append(f"JNDI lookup helpers in use: {', '.join(sorted(helpers_seen))}")

    return sorted(found.values(), key=lambda d: (d["normalized"], d["kind"], d["source_file"]))


# ─── servlet components ───────────────────────────────────────────────────────

@lru_cache(maxsize=16)
def _java_index(source_dir: str) -> Dict[str, str]:
    """FQCN → absolute path for every Java type under ``source_dir``."""
    index: Dict[str, str] = {}
    for p in _java_files(Path(source_dir)):
        try:
            if p.stat().st_size > _MAX_JAVA_BYTES:
                continue
            head = p.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        pkg = declared_package(head)
        fqcn = f"{pkg}.{p.stem}" if pkg else p.stem
        index.setdefault(fqcn, str(p))
    return index


def clear_caches() -> None:
    _java_index.cache_clear()


_ANNOTATED = re.compile(r"@(WebFilter|WebListener|WebServlet)\b")
_IMPLEMENTS = re.compile(
    r"\bimplements\b[^{]*\b(Filter|ServletContextListener|HttpSessionListener|ServletRequestListener|"
    r"ServletContextAttributeListener|HttpSessionAttributeListener)\b"
)
_EXTENDS = re.compile(r"\bextends\s+(HttpServlet|GenericServlet|HttpFilter|GenericFilter)\b")
def _kind_for(marker: str) -> str:
    """Classify by the marker's suffix so every *Listener interface counts."""
    if marker.endswith("Filter"):
        return "filter"
    if marker.endswith("Listener"):
        return "listener"
    if marker.endswith("Servlet"):
        return "servlet"
    return "unknown"


def _collect_components(web: Dict[str, Any], module_dir: Path, source_dir: Path
                        ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    index = _java_index(str(source_dir))
    components: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    declared = set()

    def resolve(fqcn: str, kind: str) -> None:
        if not fqcn or fqcn in declared:
            return
        declared.add(fqcn)
        path = index.get(fqcn)
        if path:
            in_module = str(Path(path).resolve()).startswith(str(module_dir.resolve()) + os.sep)
            components.append({
                "class": fqcn, "file": path, "rel_path": _rel(Path(path), source_dir),
                "declared_in_web_xml": True, "kind": kind,
                "via": "web.xml" if in_module else "web.xml (sibling module)",
            })
        else:
            unresolved.append({"class": fqcn, "kind": kind})

    for f in web["filters"]:
        resolve(f["class"], "filter")
    for l in web["listeners"]:
        resolve(l["class"], "listener")
    for s in web["servlets"]:
        if s["class"]:
            resolve(s["class"], "servlet")

    for p in _java_files(module_dir):
        try:
            if p.stat().st_size > _MAX_JAVA_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pkg = declared_package(text)
        fqcn = f"{pkg}.{p.stem}" if pkg else p.stem
        if fqcn in declared:
            continue
        marker = None
        via = None
        m = _ANNOTATED.search(text)
        if m:
            marker, via = m.group(1), "annotation"
        else:
            m = _IMPLEMENTS.search(text) or _EXTENDS.search(text)
            if m:
                marker, via = m.group(1), "interface"
        if marker:
            declared.add(fqcn)
            components.append({
                "class": fqcn, "file": str(p), "rel_path": _rel(p, source_dir),
                "declared_in_web_xml": False, "kind": _kind_for(marker), "via": via,
            })

    components.sort(key=lambda c: (c["kind"], c["class"]))
    unresolved.sort(key=lambda c: (c["kind"], c["class"]))
    return components, unresolved


# ─── run ──────────────────────────────────────────────────────────────────────

def run(source_dir: str, module_dir: Optional[str] = None) -> dict:
    """Extract the web-tier context of one module. JSON-serialisable; never raises on bad XML."""
    src = Path(source_dir).resolve()
    mod = Path(module_dir).resolve() if module_dir else src
    notes: List[str] = []

    candidates = _web_xml_candidates(mod)
    if not candidates:
        raise ValueError(f"no WEB-INF/web.xml under module {mod}")
    web_xml_path = candidates[0]
    if len(candidates) > 1:
        notes.append(
            f"{len(candidates)} web.xml files in module; using {_rel(web_xml_path, src)} "
            f"(others: {', '.join(_rel(c, src) for c in candidates[1:])})"
        )

    web = _parse_web_xml(web_xml_path, src)
    if web["parse_error"]:
        notes.append(web["parse_error"])
    if web["metadata_complete"] is True:
        notes.append("metadata-complete=true: annotation-driven components are ignored by the container")

    def dupes(items, key):
        seen, out = set(), []
        for it in items:
            if it[key] in seen:
                out.append(it[key])
            seen.add(it[key])
        return out

    for dup in dupes(web["filters"], "name"):
        notes.append(f"duplicate filter name '{dup}'")
    for dup in dupes(web["servlets"], "name"):
        notes.append(f"duplicate servlet name '{dup}'")
    filter_names = {f["name"] for f in web["filters"]}
    for fm in web["filter_mappings"]:
        if fm["filter_name"] not in filter_names:
            notes.append(f"filter-mapping refers to undeclared filter '{fm['filter_name']}'")
    servlet_names = {s["name"] for s in web["servlets"]}
    for sm in web["servlet_mappings"]:
        if sm["servlet_name"] not in servlet_names:
            notes.append(f"servlet-mapping refers to undeclared servlet '{sm['servlet_name']}'")
    if web["absolute_ordering"]:
        notes.append("absolute-ordering present: web-fragment order is pinned")

    vendors = _parse_vendors(web_xml_path.parent, src, web["ids"], notes)
    for v in vendors:
        if v["parse_error"]:
            notes.append(v["parse_error"])

    ear = _parse_ear(src, mod)
    liberty = _find_existing_server_xml(mod, src)
    datasources = _collect_datasources(web, vendors, liberty, mod, src, notes)
    components, unresolved = _collect_components(web, mod, src)

    class_by_filter = {f["name"]: f["class"] for f in web["filters"]}
    filter_chain = [
        {
            "filter_name": fm["filter_name"], "class": class_by_filter.get(fm["filter_name"], ""),
            "url_patterns": fm["url_patterns"], "servlet_names": fm["servlet_names"],
            "dispatchers": fm["dispatchers"], "order": fm["order"],
        }
        for fm in web["filter_mappings"]
    ]
    authz = {
        "security_constraints": web["security_constraints"],
        "login_config": web["login_config"],
        "security_roles": web["security_roles"],
        "role_bindings": [b for v in vendors for b in v["security_role_bindings"]],
    }

    ctx = {
        "extractor": NAME,
        "version": VERSION,
        "module_dir": str(mod),
        "module_rel": _rel(mod, src) if mod != src else ".",
        "web_xml": web,
        "vendors": vendors,
        "ear": ear,
        "existing_server_xml": liberty,
        "datasources": datasources,
        "servlet_components": components,
        "unresolved_classes": unresolved,
        "filter_chain": filter_chain,
        "authz": authz,
        "summary": {
            "counts": {
                "context_params": len(web["context_params"]), "listeners": len(web["listeners"]),
                "filters": len(web["filters"]), "filter_mappings": len(web["filter_mappings"]),
                "servlets": len(web["servlets"]), "security_constraints": len(web["security_constraints"]),
                "resource_refs": len(web["resource_refs"]), "vendors": len(vendors),
                "datasources": len(datasources), "servlet_components": len(components),
                "unresolved_classes": len(unresolved), "raw_unmapped": len(web["raw_unmapped"]),
            },
            "notes": notes,
            "web_xml_candidates": [_rel(c, src) for c in candidates],
        },
    }
    # Guarantee the contract: what we hand to prompts and snapshots must serialise.
    json.dumps(ctx)
    return ctx


EXTRACTOR = register(Extractor(
    name=NAME,
    run=run,
    selectors={"servlet_components": servlet_components, "server_config": server_config},
    find_modules=find_modules,
    module_for=module_for,
))
