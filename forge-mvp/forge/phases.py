"""Migration phase registry.

Each phase pairs a transform prompt with the reviewer rubric that scores its
output. Keeping the two together matters: the reviewer's checks must add up to
100 and must grade the same rules the transformer was told to apply. When they
drift apart, scores stop meaning anything.

The struts-spring6 rules are distilled from struts-to-spring_1.prompt, which
carries the full mapping tables and six worked examples.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

from forge.utils.telemetry import get_logger

_log = get_logger(__name__)

# ─── java21 ───────────────────────────────────────────────────────────────────

_JAVA21_TRANSFORM = """You are a Java migration expert. Transform the provided Java source code by applying these rules in order:

Rule 1 — Namespace migration (HIGHEST PRIORITY, zero tolerance):
- javax.servlet.*     → jakarta.servlet.*
- javax.persistence.* → jakarta.persistence.*
- javax.validation.*  → jakarta.validation.*
- javax.transaction.* → jakarta.transaction.*
Every single javax.* import MUST become jakarta.*. Zero javax.* allowed in output.
LEAVE ALONE the JDK's own javax packages — javax.crypto, javax.sql, javax.net,
javax.naming, javax.security.auth, javax.xml.parsers, javax.xml.transform.
Rewriting those to jakarta.* breaks the code.

Rule 2 — Deprecated API replacement:
- Thread.stop()                → throw new InterruptedException("Thread interrupted")
- finalize() method            → add comment: // DEPRECATED: replace with Cleaner API
- StringBuffer in loops        → StringBuilder
- System.runFinalizersOnExit() → remove the call entirely

Rule 3 — Date/Time modernisation:
- new Date() for current time  → Instant.now()
- Calendar usage               → LocalDateTime
- SimpleDateFormat             → DateTimeFormatter
- java.sql.Date                → java.time.LocalDate

Rule 4 — Local variable inference (conservative):
- Apply `var` only where the type is completely obvious from the right-hand side
- Never apply `var` to parameters or fields

Rule 5 — Flag but do not change:
- sun.misc.Unsafe usage        → add comment: // MANUAL: review Unsafe usage
- Illegal reflective access    → add comment: // MANUAL: review reflective access

Respond ONLY with valid JSON — no markdown fences, no explanation:
{
  "files": {"<original_file_path>": "<full_transformed_content>"},
  "manual_flags": [{"file": "<path>", "line": <n>, "reason": "<why>"}]
}"""

_JAVA21_REVIEW = """You are a Java migration code reviewer. Score the transformed Java code on 5 checks (total 100 points).

Check 1 — Namespace completeness (20 pts):
Zero Jakarta-EE javax.* imports remain. All replaced with jakarta.*. Full 20 if clean, 0 if any remain.
JDK javax packages (javax.crypto, javax.sql, javax.net, javax.naming, javax.xml.parsers)
are CORRECT as javax.* — do not penalise them.

Check 2 — Deprecated API removal (20 pts):
No Thread.stop(), no finalize() bodies, no Calendar, no SimpleDateFormat. Partial credit allowed.

Check 3 — Date/Time modernisation (25 pts):
Instant.now() replaces new Date(), LocalDateTime replaces Calendar, DateTimeFormatter replaces SimpleDateFormat. Partial credit allowed.

Check 4 — Safe var inference (20 pts):
var used only where type is obvious from RHS. Never on parameters or fields. Partial credit allowed.

Check 5 — No regressions (15 pts):
Original structure preserved. Error handling intact. Null checks preserved. No logic changes.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON — no markdown, no explanation:
{
  "score": <0-100>,
  "verdict": "PASS"|"RETRY"|"MANUAL",
  "feedback": "<specific actionable issues for retry, or empty string if PASS>",
  "checks": {
    "namespace": <0-20>,
    "deprecated": <0-20>,
    "datetime": <0-25>,
    "var_inference": <0-20>,
    "no_regressions": <0-15>
  }
}"""

# ─── struts-spring6 ───────────────────────────────────────────────────────────

_STRUTS_TRANSFORM = """You are an expert Java engineer specialising in legacy modernisation. Convert the
provided legacy source into idiomatic modern Java targeting:
  Java 21 (LTS) · Spring Framework 6.2.x (Boot 3.3.x) · Jakarta EE 10 · Jackson 2.17+ · JUnit 5

Apply ALL applicable transformations in a SINGLE pass. Never migrate the framework
but leave Java 8 idioms or Codehaus Jackson untouched in the same file.

Rule 1 — Preserve business logic verbatim (HIGHEST PRIORITY):
Never silently change conditionals, error handling, transactions, ordering, or data
access. Where the original behaviour is ambiguous, emit a `// TODO(migration):`
comment rather than guessing.

Rule 2 — Struts → Spring MVC:
- Action / ActionSupport          → @Controller (or @RestController for JSON endpoints)
- DispatchAction                  → @Controller with multiple @*Mapping methods
- ActionForm / Struts 2 properties→ POJO or record + jakarta.validation constraints
- struts-config.xml <action>      → @GetMapping / @PostMapping / @RequestMapping
- validation.xml / validate()     → @Valid with @NotBlank / @Email / @Size
- <forward name="success"/>       → return "viewName"; or ResponseEntity
- Struts interceptors             → HandlerInterceptor or Spring AOP
- Struts Tiles                    → flag as TODO(migration), do not attempt

Rule 3 — Spring 4 → Spring 6:
- XML <bean> definitions          → @Configuration class with @Bean methods
- Field @Autowired                → constructor injection, dependencies final
- @RequestMapping(method=GET)     → @GetMapping (and Post/Put/Delete/Patch)
- RestTemplate                    → RestClient (use WebClient ONLY if already reactive)
- AsyncRestTemplate               → WebClient
- WebMvcConfigurerAdapter         → implement WebMvcConfigurer directly
- @EnableGlobalMethodSecurity     → @EnableMethodSecurity
- WebSecurityConfigurerAdapter    → SecurityFilterChain @Bean

Rule 4 — javax → jakarta (mandatory, zero tolerance):
javax.servlet/persistence/validation/transaction/inject/ws.rs/jms/mail/annotation
/ejb/enterprise/faces/el/websocket/interceptor and javax.xml.bind → jakarta.*
LEAVE ALONE the JDK's own packages: javax.crypto, javax.sql, javax.net,
javax.security.auth, javax.naming, javax.xml.parsers, javax.xml.transform,
javax.xml.stream, javax.annotation.processing. These are NOT Jakarta EE.

Rule 5 — Jackson 1.x → 2.x:
- org.codehaus.jackson.map.ObjectMapper → com.fasterxml.jackson.databind.ObjectMapper
- org.codehaus.jackson.annotate.*       → com.fasterxml.jackson.annotation.*
- JsonSerialize.Inclusion.NON_NULL      → @JsonInclude(JsonInclude.Include.NON_NULL)
- SerializationConfig.Feature.X         → SerializationFeature.X
- DeserializationConfig.Feature.X       → DeserializationFeature.X
- For java.time types ALWAYS register JavaTimeModule and disable
  SerializationFeature.WRITE_DATES_AS_TIMESTAMPS

Rule 6 — Java 8 → Java 21 idioms (conservative):
- Pre-records data carriers → record, when the type is a pure immutable carrier
- Anonymous classes         → lambdas where the interface is functional
- if-else type cascades     → switch expressions / pattern matching
- Apply `var` only where the type is obvious from the right-hand side; never on
  parameters or fields

Rule 7 — XML configuration files:
When converting an XML config to Java config, emit the new .java file AND include
the original XML path in "deleted_files" so it can be removed. Never leave both.

Respond ONLY with valid JSON — no markdown fences, no explanation:
{
  "files": {"<file_path>": "<full_transformed_content>"},
  "deleted_files": ["<path of XML config replaced by Java config>"],
  "manual_flags": [{"file": "<path>", "line": <n>, "reason": "<why>"}]
}"""

_STRUTS_REVIEW = """You are a Java modernisation reviewer. Score the transformed code on 5 checks (total 100 points).

Check 1 — Framework migration completeness (25 pts):
No Struts types remain (Action, ActionSupport, ActionForm, ActionMapping, ActionForward).
Controllers use @Controller/@RestController with correct @*Mapping annotations.
Struts XML action definitions became annotated handler methods. Partial credit allowed.

Check 2 — Namespace migration (20 pts):
Zero Jakarta-EE javax.* imports remain. JDK packages (javax.crypto, javax.sql,
javax.net, javax.naming, javax.xml.parsers) are CORRECT as javax.* — do not penalise.
Full 20 if clean, 0 if any Jakarta-EE javax.* remains.

Check 3 — Spring 6 and Jackson 2 API currency (20 pts):
No RestTemplate/AsyncRestTemplate, no WebMvcConfigurerAdapter, no
WebSecurityConfigurerAdapter, no @EnableGlobalMethodSecurity. No
org.codehaus.jackson.* imports. Constructor injection over field @Autowired.
Partial credit allowed.

Check 4 — Java 21 idioms applied safely (15 pts):
records, lambdas, switch expressions and var used only where clearly correct.
Penalise unsafe or speculative rewrites as heavily as missed opportunities.

Check 5 — No regressions (20 pts):
Business logic, conditionals, transaction boundaries, ordering, error handling and
null checks are byte-for-byte equivalent in behaviour. Ambiguity is marked with
TODO(migration) rather than guessed. This is the most important check — a
behaviour change scores 0 here regardless of how clean the migration looks.

Scoring: PASS >= 80, RETRY 50-79, MANUAL < 50.

Respond ONLY with valid JSON — no markdown, no explanation:
{
  "score": <0-100>,
  "verdict": "PASS"|"RETRY"|"MANUAL",
  "feedback": "<specific actionable issues for retry, or empty string if PASS>",
  "checks": {
    "framework_migration": <0-25>,
    "namespace": <0-20>,
    "api_currency": <0-20>,
    "java21_idioms": <0-15>,
    "no_regressions": <0-20>
  }
}"""


# ─── registry ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhaseSpec:
    name: str
    description: str
    transform_prompt: str
    review_prompt: str
    extensions: Tuple[str, ...]
    # Config files matched by exact name, so a phase can pull in struts-config.xml
    # without sweeping up every pom.xml in the tree.
    config_filenames: Tuple[str, ...] = ()

    def includes(self, path: str) -> bool:
        name = Path(path).name.lower()
        if name in self.config_filenames:
            return True
        return any(name.endswith(ext) for ext in self.extensions)


PHASES: Dict[str, PhaseSpec] = {
    "java21": PhaseSpec(
        name="java21",
        description="Java 8 -> 21, javax.* -> jakarta.*, deprecated and date/time APIs",
        transform_prompt=_JAVA21_TRANSFORM,
        review_prompt=_JAVA21_REVIEW,
        extensions=(".java",),
    ),
    "struts-spring6": PhaseSpec(
        name="struts-spring6",
        description="Struts 1/2 -> Spring MVC 6, Spring 4 -> 6, Jackson 1 -> 2, Java 8 -> 21",
        transform_prompt=_STRUTS_TRANSFORM,
        review_prompt=_STRUTS_REVIEW,
        extensions=(".java",),
        config_filenames=(
            "struts-config.xml",
            "struts.xml",
            "struts-default.xml",
            "struts-plugin.xml",
            "validation.xml",
            "validators.xml",
            "tiles-defs.xml",
            "tiles.xml",
        ),
    ),
}

# ─── packs ────────────────────────────────────────────────────────────────────
#
# The two phases above are Phase 0: hardcoded, and the only thing the deployed
# pipeline runs today. Everything else is a pack on disk under prompts/packs/,
# loaded lazily so that a malformed pack cannot stop `--phase java21` from
# working. A PackSpec exposes .name/.description/.includes, so from here down a
# pack and a PhaseSpec are interchangeable.


@lru_cache(maxsize=1)
def _packs():
    """The pack registry, or None if the library could not be loaded."""
    from forge.packs import load_packs
    from forge.packs.spec import PackError

    try:
        return load_packs()
    except PackError as e:
        # Loud, but not fatal: Phase 0 must keep running even while the pack
        # library is mid-edit. `migrate.py --list-packs` reports it in full.
        _log.warning("Pack library did not load, continuing with built-in phases only: %s", e)
        return None


def pack_names() -> Tuple[str, ...]:
    """Ids of packs that are complete enough to name on the command line."""
    registry = _packs()
    if registry is None:
        return ()
    return tuple(p.id for p in registry.complete)


def all_phase_names() -> Tuple[str, ...]:
    return tuple(PHASES) + pack_names()


# The two hardcoded phases, as distinct from packs loaded off disk. Phase 0
# invariants are asserted against these; pack invariants live in test_packs.py.
BUILTIN_PHASE_NAMES = tuple(PHASES)

PHASE_NAMES = all_phase_names()


def get_phase(name: str):
    """Resolve a phase name to its spec — a built-in phase or a pack."""
    if name in PHASES:
        return PHASES[name]

    registry = _packs()
    if registry is not None and name in registry:
        return registry[name]

    raise ValueError(
        f"Unknown phase '{name}'. Available: {', '.join(all_phase_names())}"
    )


# ─── test generation ──────────────────────────────────────────────────────────
#
# The migration's last gate is "it compiles". That is not "it still does what it
# did". Test-Gen writes the JUnit 5 tests the migrated code never had, and its
# prompt lives here for the same reason every other prompt does: the rubric that
# grades the tests must change in the same commit as the rules that produce
# them. `test_testgen.py` asserts the weights still total 100 and still match
# the response schema's per-check maxima, in order.

_TESTGEN_GENERATE = """You are a Java test engineer. Write JUnit 5 unit tests for ONE class that has just been
migrated to Java 21 / Jakarta EE 10 / Spring Framework 6 / Mockito 5.

You are given the migrated source of the class under test and — where they could be
resolved — the public signatures of the collaborators it declares. That is the whole of
the API you may call.

Rule 1 — Never invent API (HIGHEST PRIORITY, zero tolerance):
Call only constructors, methods, fields and enum constants that appear in the source you
were given. Never guess a getter, a builder, a static factory or a constructor arity. If
a member cannot be exercised without guessing, leave it untested and say so in
"untested" — an honest gap is worth more than a test that does not compile.

Rule 2 — Shape:
- Exactly one test class per class under test, named <Type>Test, in the SAME package
- Path: src/test/java/<package as directories>/<Type>Test.java
- JUnit 5 only: org.junit.jupiter.api.Test / @BeforeEach / @AfterEach / @DisplayName /
  @Nested / @ParameterizedTest, and org.junit.jupiter.api.Assertions.*
- NEVER JUnit 4: no org.junit.Test, @RunWith, @Before, @After, @Ignore, org.junit.Assert
- jakarta.* never javax.*, except the JDK's own (javax.crypto, javax.sql, javax.naming,
  javax.net, javax.xml.parsers, javax.xml.transform)
- Complete imports, no wildcard imports except static Assertions/Mockito members, no TODO
  placeholders, no commented-out code. The file must compile as written.

Rule 3 — Isolation. A unit test touches nothing outside the JVM:
- Mock every collaborator with Mockito 5: @ExtendWith(MockitoExtension.class), @Mock,
  @InjectMocks, when(...)/thenReturn, verify(...)
- No network, no database, no filesystem, no Thread.sleep, no System.getenv
- No dependence on the current time, on random values, or on test execution order. Where
  the class reads the clock, pass a fixed Clock if it accepts one; otherwise assert on a
  range, never on an exact instant

Rule 4 — What to test, by kind (the kind is given to you):
- controller  -> MockMvcBuilders.standaloneSetup(controller) with mocked services; assert
                 status, view or body, and the arguments passed downstream
- service     -> business behaviour with mocked repositories and clients: the happy path,
                 every branch you can reach, and the exceptions the code throws
- repository  -> only what is real logic (query building, mapping); never boot a database
- entity      -> construction, accessors, equals/hashCode when overridden, and
                 jakarta.validation constraints through a Validator
- config      -> the @Bean methods return what they claim, wired with mocks; do not start
                 a Spring context
- plain       -> public behaviour, boundaries, and the documented exceptions

Rule 5 — Quality over count:
- One behaviour per test; a @DisplayName that states the behaviour, not the method name
- Assert the actual outcome. assertNotNull alone is not a test
- Cover the edge cases the code itself distinguishes: nulls it checks, empty collections
  it branches on, limits it compares against, exceptions it throws (assertThrows), and
  both sides of every boolean it returns
- Do NOT encode a bug as expected behaviour. If the migrated code looks wrong, still test
  what it does, and name it in "notes"

Rule 6 — Secrets and fixtures:
Invent no credentials, tokens, keys, real hostnames or personal data. Use obvious
placeholders such as "user@example.com" or "test-token".

Respond ONLY with valid JSON — no markdown fences, no explanation:
{
  "files": {"src/test/java/<pkg>/<Type>Test.java": "<full file content>"},
  "cases": [{"name": "<test method>", "covers": "<member or behaviour>"}],
  "untested": [{"member": "<signature>", "reason": "<why it could not be tested>"}],
  "dependencies": ["<group:artifact needed at test scope>"],
  "notes": ["<anything a human should look at>"]
}"""

_TESTGEN_REVIEW = """You are reviewing generated JUnit 5 unit tests for a class that was just migrated to
Java 21 / Jakarta EE 10 / Spring 6. You are given the class under test and the test file.
Score on 5 checks (total 100 points).

Check 1 — Framework and mechanics (20 pts):
JUnit 5 only — no org.junit.Test, @RunWith, @Before, @Ignore or org.junit.Assert. No
Jakarta-EE javax.* imports (the JDK's javax.crypto/sql/naming/net/xml.parsers are
correct). The test class is <Type>Test in the package of the class under test. Imports
are complete and the file would compile as written. Full 20 only if all of that holds.

Check 2 — Behaviour coverage (25 pts):
The public behaviour that carries risk is exercised: the happy path, each branch the code
itself distinguishes, boundaries, and the exceptions it throws. Trivial or duplicated
tests earn nothing. Partial credit.

Check 3 — Assertion quality (20 pts):
Each test asserts the real outcome — returned values, state changes, and the arguments
passed to collaborators (verify). Penalise assertNotNull-only tests, tests with no
assertion at all, and assertions that merely restate the stub that was just configured.
Partial credit.

Check 4 — Isolation and determinism (20 pts):
Collaborators are mocked; nothing touches network, database, filesystem, environment or
sleep; nothing depends on the current time, on random values or on execution order. A
test that would pass today and fail tomorrow scores 0 here. Partial credit.

Check 5 — Faithfulness to the source (15 pts):
Every constructor, method and field the test calls exists in the class under test or in
the collaborator signatures provided. No invented API, no reflection into privates, no
production code re-implemented in the test. An invented member scores 0 here — it is the
one failure that cannot compile. Partial credit otherwise.

Scoring: PASS >= 75, RETRY 50-74, MANUAL < 50.

Respond ONLY with valid JSON — no markdown, no explanation:
{
  "score": <0-100>,
  "verdict": "PASS"|"RETRY"|"MANUAL",
  "feedback": "<specific, actionable issues for the retry, or empty string if PASS>",
  "checks": {
    "framework": <0-20>,
    "coverage": <0-25>,
    "assertions": <0-20>,
    "isolation": <0-20>,
    "faithfulness": <0-15>
  }
}"""


@dataclass(frozen=True)
class TestGenSpec:
    """A test-generation prompt and the rubric that grades what it produces.

    Same contract as ``PhaseSpec``: the two prompts change together, and
    ``checks`` carries the rubric's weights in the order the response schema
    lists them, totalling 100.
    """

    name: str
    description: str
    generate_prompt: str
    review_prompt: str
    checks: Tuple[Tuple[str, int], ...]

    @property
    def total_weight(self) -> int:
        return sum(weight for _, weight in self.checks)


TESTGEN_STYLES: Dict[str, TestGenSpec] = {
    "junit5": TestGenSpec(
        name="junit5",
        description="JUnit 5 + Mockito 5 unit tests for migrated Java 21 / Jakarta EE 10 / Spring 6 code",
        generate_prompt=_TESTGEN_GENERATE,
        review_prompt=_TESTGEN_REVIEW,
        checks=(
            ("framework", 20),
            ("coverage", 25),
            ("assertions", 20),
            ("isolation", 20),
            ("faithfulness", 15),
        ),
    ),
}

TESTGEN_STYLE_NAMES = tuple(TESTGEN_STYLES)


def get_testgen_spec(style: str = "junit5") -> TestGenSpec:
    """Resolve a test style to its spec. One style today; the registry is the seam."""
    try:
        return TESTGEN_STYLES[style]
    except KeyError:
        raise ValueError(
            f"Unknown test style '{style}'. Available: {', '.join(TESTGEN_STYLE_NAMES)}"
        )
