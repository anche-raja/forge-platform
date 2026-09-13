---
id: junit4-to-junit5
version: 1.0.0
title: JUnit 4 + legacy Mockito -> JUnit 5 + Mockito 5
tier: test
detect:
  any:
    - dependency: "junit:junit"
    - import_prefix: "org.junit.Test"
    - import_prefix: "org.junit.runner"
    - dependency_lt: {coord: "org.mockito:mockito-core", value: "5.0.0"}
applies_to:
  - file_glob: "**/src/test/java/**/*.java"
context: test_subject
depends_on: [java8-to-java21]
decisions: []
eliminates:
  - "junit:junit"
acceptance:
  - no_match: 'org\.junit\.(Test|Before|After|Ignore|runner|Assert)'
    scope: "**/src/test/java/**/*.java"
  - test_parity: true
---

## transform

You are migrating one test class from JUnit 4 + legacy Mockito to JUnit 5 + Mockito 5 on Java 21.
You are given the class under test **in its migrated form**.

A migrated test suite that passes but asserts less than the original is worse than no migration:
it reports success over an unverified codebase. Fidelity of assertions outranks everything else
in this pack.

Rule 1 — JUnit 4 → 5 mechanics:
- `org.junit.Test` → `org.junit.jupiter.api.Test`
- `@Before`/`@After` → `@BeforeEach`/`@AfterEach`; `@BeforeClass`/`@AfterClass` → `@BeforeAll`/`@AfterAll`
  (these must be `static` unless the class is `@TestInstance(PER_CLASS)`)
- `@Ignore` → `@Disabled`, carrying the reason across
- `@RunWith(MockitoJUnitRunner.class)` → `@ExtendWith(MockitoExtension.class)`
- `@RunWith(SpringJUnit4ClassRunner.class)` / `SpringRunner` → `@ExtendWith(SpringExtension.class)`
  with `@ContextConfiguration` / `@WebAppConfiguration` as before. **Not `@SpringBootTest`** —
  there is no Spring Boot in this target, and the existing `@ContextConfiguration` already names
  the config classes.
- `@RunWith(Parameterized.class)` → `@ParameterizedTest` + `@MethodSource`, restructuring the
  constructor-injected fields into method parameters
- `@Rule ExpectedException` → `assertThrows(...)`, asserting the same type **and** message check
- `@Rule TemporaryFolder` → `@TempDir`
- `@Test(expected = X.class)` → `assertThrows(X.class, () -> ...)` around the statement that was
  actually expected to throw, not the whole method body
- `@Test(timeout = n)` → `@Timeout(value = n, unit = MILLISECONDS)`
- `org.junit.Assert.*` → `org.junit.jupiter.api.Assertions.*`

Rule 2 — The assertion-message trap. JUnit 4 is
`assertEquals(String message, expected, actual)`; JUnit 5 is
`assertEquals(expected, actual, String message)`. **The message moved from first to last.** A
mechanical rename leaves the message as `expected` and the real expected as `actual` — the test
still compiles, often still passes, and reports failures backwards. Check every three-argument
assertion. The same applies to `assertTrue`, `assertNull`, `assertSame` and `assertArrayEquals`.
Also: `assertEquals(double, double)` without a delta is removed in 5.

Rule 3 — Mockito:
- `org.mockito.Matchers` → `org.mockito.ArgumentMatchers`
- `org.mockito.runners.MockitoJUnitRunner` → `MockitoExtension`
- `anyString()`, `anyInt()` etc. **no longer match null** in Mockito 2+. A stub that relied on
  that now silently does not match and returns the default. Check every matcher against a nullable
  argument and use `nullable(String.class)` where null was possible.
- `MockitoExtension` enforces **strict stubbing**: an unused stub fails the test. Remove genuinely
  unused stubs. Only where removal would change what the test covers, use
  `@MockitoSettings(strictness = Strictness.LENIENT)` with a `TODO(migration)` explaining why.
- Mockito 1.x could mock final classes/methods only with plugins; Mockito 5 uses the inline mock
  maker by default. Where a test worked around the limitation (a hand-written fake, an extracted
  interface), leave the workaround — do not opportunistically rewrite it.
- `verifyZeroInteractions` → `verifyNoMoreInteractions` / `verifyNoInteractions`

Rule 4 — Framework test scaffolding (Struts `StrutsTestCase`/`ActionProxy`, Spring
`AbstractTransactionalJUnit4SpringContextTests`) → `MockMvc` or the modern Spring test support,
asserting the **same** status, view name, model attributes and side effects the original asserted.

Rule 5 — No test is silently lost. The migrated class has the same number of test methods. A test
you cannot migrate faithfully is kept, annotated `@Disabled("TODO(migration): <reason>")`, and
listed in `manual_flags`. Never delete a test, never merge two tests, never weaken an assertion to
make it pass, and never add assertions the original did not make.

Respond ONLY with valid JSON:
{"files": {...}, "deleted_files": [], "manual_flags": [...]}

## review

Score on 5 checks (total 100).

Check 1 — Assertion semantics identical (35 pts):
Every original assertion is present with the same meaning. **Argument order on every migrated
`assertEquals`/`assertTrue`/`assertNull` is correct and any message moved to the trailing
position.** An inverted expected/actual, a weakened assertion, or an added one scores 0. This is
the check that stops a green suite from hiding a real failure.

Check 2 — Test count preserved (25 pts):
The same number of test methods. Any test not migrated is `@Disabled` with a reason and flagged,
not deleted or merged. A missing test scores 0.

Check 3 — Mockito 5 correctness (20 pts):
`ArgumentMatchers` used; nullable arguments handled with `nullable()` rather than `any*()`; strict
stubbing satisfied by removing dead stubs rather than blanket leniency; existing final-class
workarounds left intact.

Check 4 — JUnit 5 migration complete (10 pts):
No `org.junit.Test`, `@RunWith`, `@Before`, `@Rule`, `Assert.*`. Lifecycle methods have correct
`static` modifiers. Full 10 or 0.

Check 5 — Scaffolding replaced faithfully (10 pts):
Framework-specific test bases replaced with equivalents asserting the same status, view, model
and side effects.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON:
{"score": <0-100>, "verdict": "PASS"|"RETRY"|"MANUAL", "feedback": "<actionable>",
 "checks": {"assertion_semantics": <0-35>, "test_count": <0-25>, "mockito5": <0-20>,
            "junit5_complete": <0-10>, "scaffolding_replaced": <0-10>}}
