# Enterprise Java Coding Standards

<!-- This file is uploaded to S3 and ingested into the Bedrock Knowledge Base.
     FORGE transform agents use it to generate code matching your enterprise conventions.
     Fill in each section with your actual standards before running Phase 6. -->

## Package Naming Conventions

<!-- TODO: Define your package naming rules.
Example:
- Root package: com.corp.{domain}.{service}
- Controller layer: com.corp.{domain}.web.controller
- Service layer: com.corp.{domain}.service
- Repository layer: com.corp.{domain}.repository
- Domain models: com.corp.{domain}.model
-->

## Class Naming Rules

<!-- TODO: Define class naming conventions.
Example:
- Controllers: {Entity}Controller (e.g. OrderController)
- Services: {Entity}Service / {Entity}ServiceImpl
- Repositories: {Entity}Repository
- DTOs: {Entity}Dto or {Entity}Request / {Entity}Response
- Exceptions: {Reason}Exception
-->

## Method Naming Rules

<!-- TODO: Define method naming conventions.
Example:
- Finders: find{Entity}By{Criteria}
- Creators: create{Entity}
- Updaters: update{Entity}
- Deleters: delete{Entity}By{Criteria}
- Boolean checks: is{Condition} / has{Condition}
-->

## Annotation Usage Standards

<!-- TODO: Specify approved annotations and their required configuration.
Example:
- @RestController preferred over @Controller + @ResponseBody
- @RequestMapping at class level for base path only
- @GetMapping / @PostMapping / @PutMapping / @DeleteMapping at method level
- @Validated (not @Valid) at controller method parameter level
- @Transactional only on service layer, never on repository or controller
-->

## Logging Standards

<!-- TODO: Define logging standards.
Example:
- Use SLF4J: private static final Logger log = LoggerFactory.getLogger(MyClass.class);
- INFO: business events (order created, payment processed)
- WARN: recoverable errors, deprecated usage
- ERROR: unrecoverable errors with full stack trace
- DEBUG: detailed flow for development only — guarded by if (log.isDebugEnabled())
- Never log PII (email, SSN, card numbers)
-->

## Exception Handling Patterns

<!-- TODO: Define exception handling strategy.
Example:
- Checked exceptions: only at system boundaries (IO, external APIs)
- Runtime exceptions: for business rule violations
- Global handler: @ControllerAdvice with @ExceptionHandler
- Custom exception hierarchy rooted at BaseAppException
- HTTP status mapping: ValidationException → 400, NotFoundException → 404, etc.
-->
