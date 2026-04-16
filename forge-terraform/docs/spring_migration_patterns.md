# Approved Spring MVC Migration Patterns

<!-- This file is uploaded to S3 and ingested into the Bedrock Knowledge Base.
     FORGE uses it to select approved patterns when migrating Struts 2 controllers to Spring MVC.
     Fill in each section with your team's approved patterns before running Phase 6. -->

## Approved @Controller Patterns

<!-- TODO: Specify how controllers should be structured post-migration.
Example:
- Use @RestController for all API controllers
- One controller per business domain resource
- Constructor injection only (no @Autowired on fields)
- Return ResponseEntity<T> for all endpoints to allow explicit status codes
- Use @RequestMapping("/api/v1/{resource}") at class level
-->

## Approved Security Config Patterns

<!-- TODO: Define the approved Spring Security configuration.
Example:
- SecurityFilterChain bean approach (not WebSecurityConfigurerAdapter — deprecated in Spring 6)
- JWT validation filter placement
- CORS configuration via CorsConfigurationSource bean
- CSRF: disabled for stateless REST APIs, enabled for server-side rendered apps
- Method-level security: @PreAuthorize with SpEL expressions
-->

## Approved Data Access Patterns

<!-- TODO: Define approved data access layer patterns.
Example:
- Spring Data JPA repositories: extend JpaRepository<T, ID>
- Named queries in @Query annotation — not XML
- Pagination: Pageable parameter, Page<T> return type
- No EntityManager injection outside custom repository implementations
- @Transactional at service layer — read-only = true for query-only service methods
-->

## Approved Exception Handling

<!-- TODO: Define the global exception handling pattern.
Example:
- @RestControllerAdvice class: GlobalExceptionHandler
- @ExceptionHandler(MethodArgumentNotValidException.class) → 400 with field errors
- @ExceptionHandler(EntityNotFoundException.class) → 404
- @ExceptionHandler(Exception.class) → 500 with correlation ID (no stack trace in response body)
- ProblemDetail (RFC 7807) response format for all errors
-->

## Approved Validation Patterns

<!-- TODO: Specify the validation approach.
Example:
- Jakarta Bean Validation (jakarta.validation.*) — not javax.validation
- @Valid on @RequestBody parameters
- Custom validators: implement ConstraintValidator<A, T>
- Service-layer validation: throw ConstraintViolationException or custom ValidationException
- No validation logic in controllers beyond @Valid annotation
-->
