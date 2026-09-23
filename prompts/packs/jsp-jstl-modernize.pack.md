---
id: jsp-jstl-modernize
version: 2.0.0
title: JSP + JSTL 1.x -> Jakarta JSTL 3.0 (taglib URIs only; framework tags preserved)
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
depends_on: [struts2-modernize, javax-to-jakarta]
decisions: [views]
eliminates:
  - "javax.servlet:jstl"
acceptance:
  - no_match: 'java\.sun\.com/jsp/jstl'
    scope: "**/*.jsp"
---

## transform

You are moving one JSP from JSTL 1.x to Jakarta JSTL 3.0 on Jakarta EE 10.

**The application is staying on Struts, upgraded to Struts 7.** This is a narrow change: the JSTL
taglib URIs move to their Jakarta names, and **nothing else in the page changes**. Struts 7 still
ships `/struts-tags`, so every `<s:` tag still works and every one of them stays.

All four rules apply. Rule 1 is the edit; Rules 2 to 4 are the things that must survive it, and
they are where this pack goes wrong — rewriting Struts tags into JSTL looks like the modernisation
being asked for, and is the one change that breaks the page.

Rule 1 — Taglib URIs. These change in **every** JSP, including ones with no framework tags —
Jakarta EE 10 does not serve the old URIs and the page fails at render:
- `http://java.sun.com/jsp/jstl/core`      → `jakarta.tags.core`
- `http://java.sun.com/jsp/jstl/fmt`       → `jakarta.tags.fmt`
- `http://java.sun.com/jsp/jstl/functions` → `jakarta.tags.functions`
- `http://java.sun.com/jsp/jstl/sql`       → `jakarta.tags.sql` (and flag it — SQL in a view is a
  defect worth reporting, though not one to fix here)
- `http://java.sun.com/jsp/jstl/xml`       → `jakarta.tags.xml`

Rule 2 — **Leave the framework taglib exactly as it is.** This is the rule most likely to be
broken, because rewriting Struts tags into JSTL looks like the modernisation being asked for. It is
not. Struts 7 still ships `/struts-tags`, the application still runs on Struts, and removing a tag
removes the thing rendering the page:
- Keep every `<s:...>` tag, attribute for attribute.
- Keep the `<%@ taglib uri="/struts-tags" %>` declaration.
- Keep every OGNL expression — `%{...}`, `#session.foo`, `#request.foo`, value-stack access —
  unchanged. OGNL is not EL, and a "corrected" expression renders blank or wrong data with no
  error at all.
- Keep `<s:property>` as `<s:property>`. It HTML-escapes by default; a bare `${x}` does not, so
  converting one is a stored XSS regression as well as a framework removal.

The same applies to any Struts 1 taglib (`html:`, `bean:`, `logic:`, `nested:`) still present.

Rule 3 — Scriptlets (`<% %>`, `<%= %>`). Do not rewrite them into EL unless the expression is a
trivial property read. Flag every scriptlet containing logic. They still work; a bad rewrite does
not.

Rule 4 — Do not restructure markup. No reformatting, no div reorganisation, no class or id
changes, no accessibility or style "improvements", no whitespace normalisation in
whitespace-sensitive regions (`<pre>`, inline scripts, textareas).

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 4 checks (total 100).

Check 1 — Framework taglib preserved (40 pts):
Every `<s:...>` tag, the `/struts-tags` declaration, and every OGNL expression are byte-identical
to the original. Any Struts tag rewritten into JSTL, Spring form tags or a bare `${...}` scores 0
for this check. This is weighted highest deliberately: the application still runs on Struts, so
removing a tag removes the code rendering the page — and converting an escaping `<s:property>` to
a bare `${...}` is a stored XSS regression on top of it. The same applies to any Struts 1
`html:`/`bean:`/`logic:`/`nested:` taglib still present.

Check 2 — JSTL URIs migrated (35 pts):
Every `java.sun.com/jsp/jstl/*` URI is now the matching `jakarta.tags.*` URI, and none is left
behind. Jakarta EE 10 does not serve the old URIs, so a missed one fails at render. Full 35 or 0.

Check 3 — Expressions and scriptlets untouched (15 pts):
EL expressions, scriptlets and `<%= %>` blocks are unchanged, except that a scriptlet containing
logic may be flagged. No scriptlet was rewritten into EL beyond a trivial property read.

Check 4 — Markup untouched (10 pts):
Structure, classes, ids, inline scripts and whitespace-sensitive content unchanged apart from the
URI substitutions above.

Checks that do not apply: a check that does not apply to this file earns its full points. A check
applies when the file contains what it is about, or when this file is where the transform had to
introduce it; it does not apply when there is nothing here for it to judge (a check about Java
code, on a descriptor that holds none). Name the checks that did not apply in `feedback`. Never
score a check 0 for having nothing to examine: 0 is for a subject that is present and wrong, or
missing where this file had to supply it.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"taglib_preserved": <0-40>, "jstl_uris": <0-35>, "expressions_untouched": <0-15>,
            "markup_untouched": <0-10>}}
