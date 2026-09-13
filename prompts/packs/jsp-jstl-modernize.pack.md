---
id: jsp-jstl-modernize
version: 1.0.0
title: JSP + JSTL 1.x -> Jakarta JSTL 3.0 (and framework taglib removal)
tier: view
detect:
  any:
    - file_glob: "**/*.jsp"
    - content_match: 'java\.sun\.com/jsp/jstl'
    - dependency: "javax.servlet:jstl"
applies_to:
  - file_glob: "**/*.jsp"
  - file_glob: "**/*.jspf"
  - file_glob: "**/*.tag"
  - file_glob: "**/*.tagf"
context: view_bindings
depends_on: [struts2-to-springmvc6, struts1-to-springmvc6, javax-to-jakarta]
decisions: [views, url_compat]
eliminates:
  - "javax.servlet:jstl"
acceptance:
  - no_match: 'java\.sun\.com/jsp/jstl'
    scope: "**/*.jsp"
  - no_match: '<(s|html|bean|logic|nested):'
    scope: "**/*.jsp"
---

## transform

You are migrating one JSP from JSTL 1.x (and, where present, a framework taglib) to Jakarta
JSTL 3.0 on Jakarta EE 10.

**You are given the view bindings**: the controller that renders this view, the model attribute
names and types it exposes, the form-backing object where one exists, and the migrated URL for
every endpoint this page links to.

Rule 1 — Taglib URIs. These change in **every** JSP, including ones with no framework tags —
Jakarta EE 10 does not serve the old URIs and the page fails at render:
- `http://java.sun.com/jsp/jstl/core`      → `jakarta.tags.core`
- `http://java.sun.com/jsp/jstl/fmt`       → `jakarta.tags.fmt`
- `http://java.sun.com/jsp/jstl/functions` → `jakarta.tags.functions`
- `http://java.sun.com/jsp/jstl/sql`       → `jakarta.tags.sql` (and flag it — SQL in a view is a
  defect worth reporting, though not one to fix here)
- `http://java.sun.com/jsp/jstl/xml`       → `jakarta.tags.xml`

Rule 2 — Struts 2 tags (`/struts-tags`):
- `<s:property value="x"/>` → `<c:out value="${x}"/>`. **Struts `property` HTML-escapes by
  default**; a bare `${x}` does not. Use `<c:out>` unless the original set `escapeHtml="false"`.
- `<s:iterator value="l" var="i">` → `<c:forEach items="${l}" var="i">` (note `IteratorStatus`
  → `varStatus`)
- `<s:if test>` / `<s:elseif>` / `<s:else>` → `<c:if>` / `<c:choose><c:when><c:otherwise>`
- `<s:form>` → `<form:form modelAttribute="..." action="...">`
- `<s:textfield>`/`<s:password>`/`<s:textarea>` → `<form:input>`/`<form:password>`/`<form:textarea>` with `path`
- `<s:select list="...">` → `<form:select items="${...}">`
- `<s:checkbox>`/`<s:radio>` → `<form:checkbox>`/`<form:radiobuttons>`
- `<s:url>`/`<s:a>` → `<c:url>` / `<a href="<c:url .../>">`
- `<s:text name="k"/>` → `<spring:message code="k"/>`
- `<s:actionerror/>`/`<s:fielderror/>` → `<form:errors path="*"/>` / `<form:errors path="f"/>`
- `<s:token/>` → the CSRF token field for the target framework

Rule 3 — Struts 1 tags (`html:`, `bean:`, `logic:`, `nested:`):
- `<bean:write name="x" property="y"/>` → `<c:out value="${x.y}"/>` (also escapes by default)
- `<bean:message key="k"/>` → `<spring:message code="k"/>`
- `<logic:iterate>` → `<c:forEach>`; `<logic:equal>`/`<logic:present>`/`<logic:notEmpty>` → `<c:if>`
- `<html:form action="/x">` → `<form:form modelAttribute="..." action="...">`
- `<html:text property="p"/>` → `<form:input path="p"/>`
- `<html:errors/>` → `<form:errors path="*"/>`
- `<nested:*>` tags → the equivalent with the full path; the nesting context disappears, so paths
  must become absolute against the form object

Rule 4 — Expression languages. Struts OGNL and Struts 1 `name`/`property` pairs are **not** EL:
- `#session.foo` → `${sessionScope.foo}`; `#request.foo` → `${requestScope.foo}`;
  `#application.foo` → `${applicationScope.foo}`
- `%{expr}` → `${expr}` only when `expr` is a plain property path
- Value-stack expressions with no EL equivalent — top-of-stack access, indexed OGNL projections
  (`list.{?#this.x}`), method calls with arguments, `#attr` — must be emitted as
  `TODO(migration)` with the original expression preserved verbatim in a comment.
  **Never guess an EL equivalent for a non-trivial OGNL expression.** A wrong expression renders
  blank or wrong data with no error.

Rule 5 — Every URL in the markup must match a migrated endpoint from the bindings you were given,
after the `url_compat` decision. A link to a path with no handler is a broken page, and it will not
be caught by any compiler.

Rule 6 — Scriptlets (`<% %>`, `<%= %>`). Do not rewrite them into EL unless the expression is a
trivial property read. Flag every scriptlet containing logic. They still work; a bad rewrite does
not.

Rule 7 — Do not restructure markup. No reformatting, no div reorganisation, no class or id
changes, no accessibility or style "improvements", no whitespace normalisation in
whitespace-sensitive regions (`<pre>`, inline scripts, textareas).

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Escaping preserved (30 pts):
Every output that the original taglib escaped by default is still escaped. An `<s:property>` or
`<bean:write>` that became a bare `${...}` on user-controlled data is a **stored XSS regression**
and scores 0 for this check. This is weighted highest deliberately: it is the one error in this
pack that creates a vulnerability rather than a visible bug.

Check 2 — Expression correctness (25 pts):
Every expression is either a correct EL equivalent against a model attribute the controller
actually exposes, or flagged `TODO(migration)` with the original preserved. A guessed equivalent
for a non-trivial OGNL expression scores 0 — flagging is strictly better than guessing here.

Check 3 — Taglib URIs and framework tag removal (20 pts):
Every JSTL URI is `jakarta.tags.*`; no `java.sun.com` URI and no framework taglib declaration
remains; no `<s:`, `<html:`, `<bean:`, `<logic:` or `<nested:` tag survives. Full 20 or 0.

Check 4 — URLs resolve (15 pts):
Every link, form action and redirect target corresponds to a migrated endpoint under the active
`url_compat` decision.

Check 5 — Markup untouched (10 pts):
Structure, classes, ids, inline scripts and whitespace-sensitive content unchanged apart from the
substitutions above.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"escaping_preserved": <0-30>, "expression_correctness": <0-25>, "taglib_uris": <0-20>,
            "urls_resolve": <0-15>, "markup_untouched": <0-10>}}
