---
id: java8-to-java21
version: 1.0.0
title: Java 8 -> Java 21 LTS
tier: language
detect:
  any:
    - property_lt: {name: "maven.compiler.source", value: "21"}
    - property_lt: {name: "maven.compiler.target", value: "21"}
    - property_lt: {name: "maven.compiler.release", value: "21"}
    - gradle_property_lt: {name: "sourceCompatibility", value: "21"}
applies_to:
  # Only files carrying something a transform rule below acts on: a removed
  # API (Rule 1), an idiom candidate -- anonymous class, instanceof, switch,
  # multi-line string concatenation (Rule 2), a default-charset or exec call
  # (Rule 3), or Date/Calendar/SimpleDateFormat (Rule 4). A file with none of
  # these has nothing to rewrite and costs no model call. Not selected by
  # content: `record` and `var` candidates, which no pattern can recognise
  # reliably -- a file is still offered them when another trigger sends it.
  # Keep this list in step with the rules: a rule with no trigger here never runs.
  - content_match:
      glob: "**/*.java"
      pattern: '\.(stop|suspend|resume)\(\)|runFinalizersOnExit|\bvoid\s+finalize\s*\(|\bSecurityManager\b|\bAccessController\b|\.newInstance\(\)|\bnew\s+(Integer|Long|Short|Byte|Character|Boolean|Double|Float)\s*\(|\bsun\.|\bnew\s+[\w.]+(<[^>]*>)?\s*\([^()]*\)\s*\{|\binstanceof\b|\bswitch\s*\(|"\s*\+\s*\n\s*"|"\s*\n\s*\+\s*"|\bnew\s+String\s*\(|\.getBytes\(\s*\)|\bnew\s+(FileReader|FileWriter|InputStreamReader|OutputStreamWriter|PrintWriter|PrintStream)\s*\(|\.exec\s*\(|\bnew\s+Date\s*\(|\bCalendar\b|\bSimpleDateFormat\b'
context: none
depends_on: [build-maven-modernize]
decisions: [idiom_aggressiveness]
eliminates: []
acceptance:
  - no_match: 'Thread\.stop\(|\.runFinalizersOnExit\(|new (Integer|Long|Double|Boolean|Character)\('
    scope: "**/*.java"
  - build: "mvn -q -DskipTests compile"
---

## transform

You are modernising a Java 8 source file to Java 21 LTS. The file already compiles under Java 8.

The default posture is **conservative**: a missed modernisation costs nothing, a wrong one costs a
production incident. Apply `idiom_aggressiveness` from the profile — `conservative` means apply a
rewrite only when it is locally provable from the code in front of you.

Rule 1 — Removed and unusable APIs (mandatory, the build fails without these):
- `Thread.stop()`, `Thread.suspend()`, `Thread.resume()` → these throw `UnsupportedOperationException`
  on 21. Replace with interruption, and flag the call site — the surrounding shutdown logic almost
  certainly assumed synchronous termination.
- `System.runFinalizersOnExit()` → removed; delete the call.
- `finalize()` overrides → no longer invoked reliably. Add
  `// TODO(migration): finalize() is not called on Java 21 — move to Cleaner or try-with-resources`
  and do **not** delete the body.
- `SecurityManager`, `AccessController.doPrivileged` → deprecated for removal. Flag, do not rewrite.
- `Class.newInstance()` → `getDeclaredConstructor().newInstance()` (the exception signature changes;
  adjust the catch).
- Boxed constructors `new Integer(x)` → `Integer.valueOf(x)`.
- `sun.misc.Unsafe`, `sun.*` internals, illegal reflective access → `// MANUAL:` comment only.
  Never rewrite these; the strong encapsulation on 21 may need a `--add-opens` flag instead.

Rule 2 — Language idioms, applied only where locally provable:
- pure immutable data carrier (all-final fields, constructor, getters, equals/hashCode/toString)
  → `record`. Not if it is JPA-mapped, serialised by a framework expecting a bean, or subclassed.
- anonymous class implementing a single-method interface → lambda. Not if it references `this`.
- if-else cascade on `instanceof` with a cast → pattern matching for `instanceof`.
- switch on enum/String returning a value → switch expression with `->`.
- multi-line string concatenation of SQL/XML/JSON → text block, **preserving exact whitespace and
  trailing newline semantics** — a text block strips incidental indentation and this changes the
  string if the original relied on its own spacing.
- `var` only where the right-hand side makes the type unambiguous (`new Foo()`, a cast, a literal).
  Never on fields, never on parameters, never where the RHS is a method call whose return type is
  not evident from the line.

Rule 3 — Library and runtime behaviour changes that Java 21 exposes:
- Default charset is UTF-8 since 18. Any `new String(bytes)`, `FileReader`, `PrintWriter` or
  `getBytes()` without an explicit charset may change behaviour on a platform that was not UTF-8.
  Make the charset explicit; flag where the original intent is unclear.
- `Locale` provider defaults changed; date and number formatting output may differ.
- Integer caching, `String.format` and `Collections` iteration order are unchanged — do not "fix".
- `Runtime.exec(String)` parsing is deprecated → use the array/`ProcessBuilder` form.

Rule 4 — Date/time modernisation, all-or-nothing per value:
`new Date()` for "now" → `Instant.now()`; `Calendar` → `LocalDateTime`; `SimpleDateFormat` →
`DateTimeFormatter` (note: `SimpleDateFormat` is mutable and not thread-safe, `DateTimeFormatter`
is immutable and safe — a static field that was a bug becomes correct).
**Only migrate a value if every use of it in this file migrates too.** A field that becomes
`Instant` but is still passed to a `java.util.Date` API is a compile error; leaving it alone is
correct. `java.sql.Date`/`Timestamp` at a JDBC boundary stays as-is unless the JDBC layer migrates.

Rule 5 — Do not change: logging framework, exception types, method signatures on public API,
serialization form (`serialVersionUID`), or anything a framework reflects over.

Respond ONLY with valid JSON:
{"files": {"<path>": "<full content>"}, "deleted_files": [], "manual_flags": [...]}

## review

Score the migrated file on 5 checks (total 100).

Check 1 — Removed APIs handled (25 pts):
No `Thread.stop()`, `runFinalizersOnExit`, boxed constructors, or `Class.newInstance()`.
`finalize()` bodies preserved and flagged rather than deleted. `sun.misc.Unsafe` untouched and
commented. Full or partial credit per item; a deleted `finalize()` body scores 0.

Check 2 — Idioms applied safely (20 pts):
records, lambdas, pattern matching, switch expressions, text blocks and `var` used only where
provably correct. **Penalise a speculative rewrite more heavily than a missed one**: a record
where the class is JPA-mapped, a lambda where the anonymous class used `this`, or a text block
that changed the string all score 0 for this check.

Check 3 — Charset and locale explicitness (15 pts):
Implicit-charset APIs made explicit or flagged. No silently changed formatting behaviour.

Check 4 — Date/time migration is complete per value (20 pts):
No value is half-migrated. A field changed to `java.time` is used as `java.time` everywhere in
this file. JDBC boundary types left alone. A mixed `Date`/`Instant` value scores 0.

Check 5 — No regressions (20 pts):
Logic, conditionals, ordering, error handling, null checks, public signatures and
`serialVersionUID` are unchanged. Ambiguity marked `TODO(migration)`.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"removed_apis": <0-25>, "idioms_safe": <0-20>, "charset_locale": <0-15>,
            "datetime_complete": <0-20>, "no_regressions": <0-20>}}
