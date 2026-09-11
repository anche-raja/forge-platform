"""Migration phase registry.

Each phase pairs a transform prompt with the reviewer rubric that scores its
output. Keeping the two together matters: the reviewer's checks must add up to
100 and must grade the same rules the transformer was told to apply. When they
drift apart, scores stop meaning anything.

The struts-spring6 rules are distilled from struts-to-spring_1.prompt, which
carries the full mapping tables and six worked examples.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

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

PHASE_NAMES = tuple(PHASES)


def get_phase(name: str) -> PhaseSpec:
    try:
        return PHASES[name]
    except KeyError:
        raise ValueError(
            f"Unknown phase '{name}'. Available: {', '.join(PHASE_NAMES)}"
        ) from None
