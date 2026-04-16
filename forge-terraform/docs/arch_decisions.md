# Architecture Decisions (ADRs)

<!-- This file is uploaded to S3 and ingested into the Bedrock Knowledge Base.
     Paste your Architecture Decision Records here, particularly those relevant to the
     target architecture FORGE is migrating toward.
     FORGE agents use this context when making structural decisions during transformation. -->

## How to Use This File

Add ADRs in the format below. Focus on decisions that affect:
- Framework and library choices (Spring version, security library, ORM)
- Package and module structure
- Error handling strategy
- API design (REST vs GraphQL, versioning approach)
- Data access patterns (JPA vs JDBC, caching layer)
- Configuration management (profiles, secrets management)

---

## ADR Template

### ADR-NNNN: [Short Decision Title]

**Date:** YYYY-MM-DD
**Status:** Accepted | Deprecated | Superseded by ADR-XXXX

**Context:**
<!-- What is the issue that motivated this decision? What is the background? -->

**Decision:**
<!-- What was decided? State the decision in full sentences. -->

**Consequences:**
<!-- What becomes easier or harder as a result of this decision? -->

---

## ADRs

<!-- TODO: Paste your project's ADRs below this line. -->

### ADR-0001: Example — Use Spring Boot 3.x / Spring 6

**Date:** 2024-01-01
**Status:** Accepted

**Context:**
The existing application runs on Spring Boot 2.x (Spring Framework 5) and Java 8.
Java 8 reaches end-of-life support and Spring Boot 2.x follows in November 2023.

**Decision:**
Migrate to Spring Boot 3.x (Spring Framework 6) and Java 21 LTS.

**Consequences:**
- Jakarta EE namespace migration required (javax.* → jakarta.*)
- Spring Security 6 configuration API (no WebSecurityConfigurerAdapter)
- Native compilation support via GraalVM becomes available
- Minimum Java version is now 17 (targeting 21 LTS)
