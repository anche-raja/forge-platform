# Continue.dev for the Legacy Java Migration

A focused reference for using Continue.dev to migrate Struts + Spring 4 + Java 8
+ Jackson 1 to a modern Spring 6 / Java 21 stack across BOM, common library,
and dependent apps. Covers the features that matter for this kind of work and
the practices that keep it from going off the rails.

---

## Table of contents

1. The four interaction modes — when to use each
2. Configuration layout — what lives where
3. Models — picking and configuring them for migration work
4. Rules — making the agent obey project conventions
5. Prompts — invoking specialised migration behaviour
6. Context — making the agent see the right code
7. MCP servers — extending the agent's capabilities
8. Tool policies — controlling what the agent can do unattended
9. Continue CLI (`cn`) — for batch automation
10. Migration-specific best practices
11. The full project setup, end to end

---

## 1. The four interaction modes — when to use each

Continue has three IDE modes (a fourth, Edit, is older). Each gives the model
a different level of capability. For a migration, you'll use all three but
for different phases.

| Mode  | Tools available                  | Use it for                                              |
|-------|----------------------------------|---------------------------------------------------------|
| Chat  | None — text only                 | Asking questions, sanity checks, no code changes        |
| Plan  | Read-only (read, list, search)   | Investigating before acting, drafting migration plans   |
| Agent | Full — read, write, run commands | Executing the migration, writing files, running tests   |

**Plan mode is the underrated one for this project.** Before migrating a
controller, run Plan mode against it: "Read this Struts action and the
related ActionForm and validation.xml, then describe what the equivalent
Spring controller should look like — but don't write any code yet." The model
explores the codebase using read-only tools, writes a plan, and you review.
Then you flip to Agent mode and say "implement the plan." This two-step keeps
you from being surprised by what the agent does.

`Cmd/Ctrl + .` cycles between the modes. The same chat window holds all three.

---

## 2. Configuration layout — what lives where

```
your-repo/
├── .continue/
│   ├── config.yaml              # the per-repo agent definition
│   ├── rules/                   # standing instructions, auto-applied
│   │   ├── 00-stack.md
│   │   ├── 10-java-modernize.md
│   │   ├── 20-struts-rules.md
│   │   ├── 30-spring-mvc.md
│   │   ├── 40-spring-jdbc.md
│   │   └── 50-testing.md
│   ├── prompts/
│   │   └── modernize.prompt     # the few-shot prompt we built
│   └── mcpServers/
│       ├── filesystem.yaml
│       └── git.yaml
├── MIGRATION_LOG.md             # the running migration log
├── pom.xml                      # has the OpenRewrite plugin block
├── rewrite.yml                  # the OpenRewrite recipe
└── src/
```

There's also `~/.continue/` (your home directory). It holds **global** config
that applies to every project. For migration work, keep almost everything in
the per-repo `.continue/` so it's versioned and team-shareable. Global config
is fine for personal preferences (autocomplete model, theme, etc.).

`config.yaml` extends and overrides `~/.continue/config.yaml`, and rules in
`.continue/rules/` are loaded automatically on top.

---

## 3. Models — picking and configuring them for migration work

You want three model roles configured, possibly using different models:

```yaml
name: java-modernization
version: 1.0.0
schema: v1

models:
  # Heavy-lift model for Agent mode: complex reasoning, multi-file edits
  - name: Claude Sonnet 4.6
    provider: anthropic
    model: claude-sonnet-4-6
    apiKey: ${{ secrets.ANTHROPIC_API_KEY }}
    roles:
      - chat
      - edit
      - agent
    defaultCompletionOptions:
      temperature: 0.1            # migrations are not creative writing
      maxTokens: 8000             # multi-file outputs need headroom
      contextLength: 200000       # whole-controller-plus-deps fits

  # Fast local model for autocomplete (optional but worth it)
  - name: Qwen Coder local
    provider: ollama
    model: qwen2.5-coder:7b
    roles: [autocomplete]
    autocompleteOptions:
      debounceDelay: 350
      maxPromptTokens: 1024

  # Embedding model for codebase search
  - name: Nomic Embed
    provider: ollama
    model: nomic-embed-text
    roles: [embed]
```

**Why temperature 0.1 specifically.** Anything above 0.3 starts producing
inconsistent transformations across files. The same Struts pattern in two
controllers should produce the same Spring code; high temperature breaks
that promise. The few-shot prompt also fights you for compliance when the
model is sampling broadly.

**Why a separate autocomplete model.** Cloud-model autocomplete is slow and
expensive. Use a local 7B-class coder model for inline suggestions and save
the cloud model for chat/edit/agent calls. Codestral, Qwen Coder, and
StarCoder2 are all reasonable.

**Tool support matters for agent mode.** Not every model can do tools. Claude
3.5+, GPT-4+, and recent open models with tool support work; older or smaller
models will show a "Not Supported" message in the mode selector.

You can also override the system message per mode at the model level:

```yaml
models:
  - name: Claude Sonnet 4.6
    # ...
    chatOptions:
      baseSystemMessage: "You are a Java migration specialist."
      baseAgentSystemMessage: |
        You are executing a legacy Java migration. Always read MIGRATION_LOG.md
        before making changes. Always update it after a successful change.
        Always run tests before considering a file done.
      basePlanSystemMessage: |
        You are planning a legacy Java migration step. Use only read-only tools.
        Produce a numbered plan with file paths and a brief justification.
        Do not write code in plan mode.
```

---

## 4. Rules — making the agent obey project conventions

**This is the single highest-leverage feature for a migration project.**

Rules are markdown files in `.continue/rules/`. Each file has YAML frontmatter
that controls when it activates. Files load in lexicographical order, so
prefix with numbers to control priority.

Three activation modes:

- **`alwaysApply: true`** — included in every prompt, every mode. Use for the
  rules that must never be forgotten.
- **`globs: "**/*.java"`** with `alwaysApply: false` — only loaded when the
  agent is touching a file that matches. Use for language- or
  framework-specific rules.
- **`description: "..."`** with `alwaysApply: false` and no globs — the agent
  reads the description, decides if the rule is relevant, and pulls it in.
  Use for situational rules.

### A starter rule set for this migration

`.continue/rules/00-stack.md` — always-on, reminds the agent of targets:

```markdown
---
name: Target stack
alwaysApply: true
---

This codebase is being migrated. Always target:
- Java 21 (LTS), Jakarta EE 10 (jakarta.* namespace)
- Spring Framework 6.2.x / Spring Boot 3.3.x
- Jackson 2.17+ (com.fasterxml.jackson.*)
- JUnit 5, SLF4J 2.x, Hibernate 6.x

Never produce javax.* imports for Jakarta EE packages (servlet, persistence,
validation, annotation, inject, jms, mail, ws.rs). javax.crypto, javax.sql,
javax.net, javax.security.auth, javax.naming, and javax.xml stay javax —
they are JDK packages, not Jakarta EE.
```

`.continue/rules/10-java-modernize.md` — auto-attached for Java files:

```markdown
---
name: Java modernization conventions
globs: "**/*.java"
alwaysApply: false
description: Java idioms required when touching any .java file
---

- Constructor injection only. No field @Autowired. Mark dependencies final.
- SLF4J for logging: LoggerFactory.getLogger(X.class) with parameterized
  messages ("user {} logged in", id) — never string concatenation.
- Records for immutable DTOs, except JPA entities, classes with setters, or
  classes in an inheritance hierarchy.
- @GetMapping / @PostMapping / etc. instead of @RequestMapping(method = ...)
- text blocks (""") for multi-line strings
- @ConfigurationProperties records over scattered @Value injection
```

`.continue/rules/20-struts-rules.md` — auto-attached when Struts is touched:

```markdown
---
name: Struts -> Spring MVC
globs: ["**/*Action.java", "**/*ActionForm.java", "**/struts*.xml", "**/validation.xml"]
description: Apply Struts -> Spring MVC migration rules when Struts artifacts are present
alwaysApply: false
---

Follow the migration patterns in .continue/prompts/modernize.prompt. Specifically:
- Action / ActionSupport -> @Controller (or @RestController)
- ActionForm -> POJO or record + JSR-380 (jakarta.validation)
- struts-config.xml <action> -> @RequestMapping/@GetMapping/@PostMapping
- Struts interceptor -> Spring HandlerInterceptor

Always update MIGRATION_LOG.md with the file migrated and the resulting Spring
class names, in the same git commit as the code change.
```

`.continue/rules/30-spring-mvc.md` — controllers:

```markdown
---
name: Spring MVC controllers
globs: "**/*Controller.java"
alwaysApply: false
---

If the input is already @Controller / @RestController, do NOT treat as Struts
conversion. Apply only Spring 4 -> 6 idiom upgrades:
- @RequestMapping(method=GET) -> @GetMapping
- field @Autowired -> constructor injection
- javax.* -> jakarta.*
- @Controller + @ResponseBody on every method -> @RestController (only if
  every handler returns JSON)

Preserve method signatures unless the parameter is genuinely unused. If a
HttpServletRequest parameter is referenced (even via attribute lookup), keep
it and switch to jakarta.servlet.
```

`.continue/rules/40-spring-jdbc.md` — DAOs:

```markdown
---
name: Spring JDBC DAOs
globs: ["**/*Dao.java", "**/*Repository.java"]
description: Spring JDBC migration patterns
alwaysApply: false
---

JdbcTemplate / NamedParameterJdbcTemplate are NOT deprecated. Working code
stays. Only refactor when touching the file for other reasons.

If refactoring:
- JdbcTemplate -> JdbcClient (Spring 6.1+)
- Anonymous RowMapper -> DataClassRowMapper<T> as a static final constant
- BeanPropertyRowMapper.newInstance(Foo.class) -> new DataClassRowMapper<>(Foo.class)
  — but only if Foo is a record or has a single all-args constructor
- queryForObject + EmptyResultDataAccessException -> JdbcClient.query(...).optional()
  — but treat as a contract change and update all callers

DataSource stays javax.sql.DataSource (JDK package, not Jakarta EE).
```

`.continue/rules/50-testing.md`:

```markdown
---
name: Test conventions
globs: "**/src/test/**/*.java"
alwaysApply: false
---

- JUnit 5 (org.junit.jupiter.api.*), not JUnit 4
- AssertJ for assertions, not Hamcrest
- Mockito 5.x with byte-buddy-agent (Java 21 dynamic agent loading warnings)
- @BeforeEach / @AfterEach, not @Before / @After
- @ExtendWith(MockitoExtension.class), not @RunWith
```

### Rule discipline

- **Keep rules under ~25 lines each.** Long rules get ignored by the model.
- **Be specific and testable.** "Write clean code" is useless. "Maximum
  function length: 40 lines" is enforceable.
- **One concern per file.** Don't mix testing rules with logging rules.
- **Lexicographic prefixes.** `00-`, `10-`, `20-` give you room to insert
  later without renumbering everything.

---

## 5. Prompts — invoking specialised migration behaviour

Prompts are slash commands. The `modernize.prompt` file we built lives at
`.continue/prompts/modernize.prompt` and gets invoked by typing `/modernize`
in the chat input.

A prompt file is markdown with optional YAML frontmatter:

```markdown
---
name: modernize
description: Migrate legacy Java to modern stack
invokable: true
---

<system>
Long, detailed system block with rules and examples
</system>

User instruction: {{{ input }}}

Selected code:
```
{{{ currentFile }}}
```
```

**Rules vs. prompts — what's the difference?**

- **Rules** are always (or contextually) on. They steer behaviour
  continuously. Use for "this is true for every file in this project."
- **Prompts** are explicit invocations. The user types `/modernize` to get a
  specific transformation. Use for "do this specific thing on demand."

For this migration: rules tell the agent _how_ to write modern Java when it's
writing any Java; the prompt is for "given this Struts code, produce the
Spring equivalent right now, with these examples."

You can have multiple prompts for sub-tasks:

- `/modernize` — the main legacy migration
- `/jsp-to-thymeleaf` — JSP view layer
- `/explain-migration` — read-only, explains what would change
- `/validation` — converts validation.xml to JSR-380 annotations

---

## 6. Context — making the agent see the right code

The agent has built-in tools to navigate your codebase (read files, search,
list directories). But you can also push context manually with `@`:

| Reference         | What it does                                                |
|-------------------|-------------------------------------------------------------|
| `@<file>`         | Pulls a specific file into context                          |
| `@<folder>`       | Pulls a whole folder                                        |
| `@open`           | All currently open editor tabs                              |
| `@problems`       | The IDE's current diagnostics (compile errors, lints)       |
| `@terminal`       | Recent terminal output                                      |
| `@diff`           | Current git diff                                            |
| `@MIGRATION_LOG.md` | The migration log (always include this)                   |

**Migration-specific context discipline.** When migrating a Struts action,
the minimum context the agent needs is:

```
@LoginAction.java @LoginForm.java @struts-config.xml @validation.xml @MIGRATION_LOG.md
```

Without all five, the agent will invent. With them, it has everything to
produce a correct Spring controller and update the log.

> **Note.** The `@codebase` and `@docs` providers were deprecated and replaced
> by built-in agent file/search tools and MCP servers. If you find old
> tutorials referencing `@codebase`, ignore them — the modern workflow is
> "let the agent search using its tools" or "set up an MCP server."

---

## 7. MCP servers — extending the agent's capabilities

MCP (Model Context Protocol) lets the agent call external tools. For this
migration, three MCP servers are worth setting up:

### Filesystem (already implicit, but explicit MCP gives more control)

```yaml
# .continue/mcpServers/filesystem.yaml
name: Filesystem
version: 0.0.1
schema: v1
mcpServers:
  - name: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/your/repo"]
```

### Git (the agent can read commit history, branches)

```yaml
# .continue/mcpServers/git.yaml
name: Git
version: 0.0.1
schema: v1
mcpServers:
  - name: git
    command: uvx
    args: ["mcp-server-git", "--repository", "."]
```

This is genuinely useful: "Show me the last 10 commits to LoginAction.java —
were there bug fixes I should know about before I migrate it?"

### Internal docs (if you have a Confluence/Notion with migration patterns)

```yaml
mcpServers:
  - name: confluence
    command: npx
    args: ["-y", "mcp-server-confluence"]
    env:
      CONFLUENCE_URL: ${{ secrets.CONFLUENCE_URL }}
      CONFLUENCE_TOKEN: ${{ secrets.CONFLUENCE_TOKEN }}
```

**MCP only works in Agent mode**, not Chat or Plan. The agent decides when
to call MCP tools based on the prompt and the tool descriptions.

---

## 8. Tool policies — controlling what the agent can do unattended

The agent's three tool policies:

- **Ask First** (default) — agent requests permission for every tool call.
  Safe but slow.
- **Automatic** — runs without asking. Fast but dangerous if a malformed
  tool call wipes a file.
- **Excluded** — never available to the agent.

For a migration project, my recommended policy is:

| Tool                                    | Policy        | Reason                                |
|-----------------------------------------|---------------|---------------------------------------|
| read_file, list_directory, search       | Automatic     | Read-only, no risk                    |
| edit_file, create_file                  | Ask First     | This is where mistakes happen         |
| run_terminal_command                    | Ask First     | Especially for `mvn`, `git`, etc.     |
| `git push` specifically                 | Excluded      | Push manually after PR review         |
| MCP filesystem write                    | Excluded      | Use the built-in edit tool instead    |

There's a known issue worth being aware of: in agent mode, the file edit tool
occasionally overwrites a file with the model's reasoning instead of the
intended change. **Always commit between agent actions** so a bad edit can
be reverted with one `git restore`. Don't let the agent run unattended on 20
files in a row.

---

## 9. Continue CLI (`cn`) — for batch automation

The IDE extension is interactive. The CLI (`cn`) is the same agent in
headless mode — perfect for batch migration scripts.

```bash
# Install
npm i -g @continuedev/cli

# Authenticate
export CONTINUE_API_KEY=<your-key>

# Headless single-shot
cn -p "Migrate src/main/java/com/acme/web/LoginAction.java per /modernize"

# With piped input
cat src/main/java/com/acme/web/LoginAction.java | \
  cn -p "Apply the modernize prompt to this file"

# JSON output for scripting
cn -p "Find all Struts actions and list them" --format json

# Tool permission control
cn --allow Read --ask Edit --exclude Bash -p "Plan migration of CustomerAction"
```

The CLI uses the same `.continue/` directory the IDE does, so your rules,
prompts, and MCP servers all work in headless mode.

### Where the CLI fits in this project

**Use the CLI for:**
- Looping over many similar files (50 ActionForm classes, all the same shape)
- CI checks ("does this PR violate any migration rules?")
- One-shot transformations across the whole repo (e.g., add a missing
  `@Override` annotation everywhere it's missing)

**Don't use the CLI for:**
- The first migration of a new file pattern — do that interactively in IDE
  agent mode where you can correct course
- Anything where you need to review every diff before commit — IDE is faster
  for that

A typical CLI batch script:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Find every Struts ActionForm and migrate it
for f in $(grep -rln 'extends ActionForm' src/main/java); do
  echo "Migrating $f..."
  cn -p "Migrate $f per the modernize prompt. Update MIGRATION_LOG.md. \
        Run mvn compile after the change. If compile fails, revert and exit 1." \
     --allow Read --ask Edit --ask Bash || { echo "Failed on $f"; exit 1; }
  git add -A
  git commit -m "Migrate ActionForm: $f" || true
  mvn -q test || { echo "Tests failed after $f"; exit 1; }
done
```

This is the closest you'll get to "autonomous migration." It still pauses on
edits, runs tests as a gate, and commits per file so you can review.

---

## 10. Migration-specific best practices

### Per-session discipline

1. **Open the file you want to migrate** before invoking the prompt. The
   agent uses the active editor as default context.
2. **Pull in MIGRATION_LOG.md** with `@MIGRATION_LOG.md`. The agent reads
   what's done, what's blocked, what TODOs are open.
3. **Pull in related files** (`@LoginForm.java`, `@struts-config.xml`).
   Don't make the agent guess what's in the supporting files.
4. **Use Plan mode first** for any non-trivial migration. Get the strategy
   on paper, review it, then switch to Agent mode and say "execute".
5. **One file per agent turn.** Don't say "migrate all the customer
   controllers." Say "migrate CustomerAction." Review the diff. Commit.
   Move to the next.
6. **Run tests before committing.** Either tell the agent to run them or
   do it yourself. Never commit a migration that fails compile or tests.
7. **Update MIGRATION_LOG.md in the same commit** as the code change. The
   rule for that is in `.continue/rules/`.

### Per-day discipline

- **Start the day by reviewing MIGRATION_LOG.md.** Where did yesterday end?
  Are there any BLOCKED items that have new information today?
- **Run `mvn verify` at start and end of day.** Catches drift between
  sessions.
- **Push the migration branch daily.** Don't sit on a week of work locally.

### Per-week discipline

- **Audit the rules.** Are they being followed? `git log --grep "TODO(migration)"`
  shows the TODOs the agent flagged. Walk through them.
- **Update the rules with lessons learned.** "I keep having to remind the
  agent that we use Lombok" → add it to a rule.

### Things I'd tell the team on day one

- **The agent will sometimes confidently produce wrong code.** Specifically:
  hallucinated imports, wrong method signatures on Spring 6 (it'll mix Spring
  5 and Spring 6 APIs), and incorrect `Optional` semantics. Read every diff.
- **The agent has no memory between chat sessions.** That's why
  `MIGRATION_LOG.md` exists. If you start a fresh chat, the first thing it
  needs is `@MIGRATION_LOG.md`.
- **Don't let it batch.** "Do all the actions in package X" is asking for
  trouble. One file, one diff, one commit.
- **Plan mode is your friend.** Especially for the first instance of a new
  pattern, where you don't yet know what the right Spring shape is.

### Multi-repo coordination

For your BOM + common + apps setup:

- **Each repo has its own `.continue/` directory** with the same rules and
  prompts. Symlinking is fragile; just copy the directory.
- **A common library change should be done first**, snapshot published,
  then app updates pull the snapshot.
- **Don't share a single `MIGRATION_LOG.md` across repos.** Each repo's log
  is independent. A top-level tracking spreadsheet (or Jira) is fine for
  cross-repo status.
- **The same rules in every repo** matter more than you'd think. If common
  uses constructor injection but app uses field injection, the agent will
  be confused. Pick one and enforce everywhere.

---

## 11. The full project setup, end to end

When you start the migration for a new repo, here is the literal sequence
of commands and decisions:

### Day 0: One-time setup

```bash
# In each repo (BOM is simpler — see notes below)
cd <repo>
git checkout -b migration-java21

# Create the Continue config tree
mkdir -p .continue/rules .continue/prompts .continue/mcpServers
cp /shared/migration-toolkit/rules/*.md .continue/rules/
cp /shared/migration-toolkit/modernize.prompt .continue/prompts/
cp /shared/migration-toolkit/MIGRATION_LOG.md ./MIGRATION_LOG.md
cp /shared/migration-toolkit/rewrite.yml ./
# Edit pom.xml: add the OpenRewrite plugin block

# Verify the agent picks it up
# Open IDE -> open Continue -> chat input -> see rules listed when you
# click the pen icon above the toolbar

git add -A
git commit -m "chore(migration): scaffold Continue + OpenRewrite config"
git push -u origin migration-java21
```

The BOM repo skips OpenRewrite (no Java code) but still gets the
`.continue/` directory and the migration log — its log only has Phase 1
entries (parent version bumps).

### Day 1: Phase 1 — OpenRewrite

```bash
mvn -U rewrite:dryRun
less target/site/rewrite/rewrite.patch  # review carefully
mvn rewrite:run
mvn clean verify                        # gate
git add -A && git commit -m "Phase 1: OpenRewrite mechanical migration"
```

In Continue, open `MIGRATION_LOG.md` in agent mode and say:

> Phase 1 complete. Update the Phase 1 section of MIGRATION_LOG.md with the
> commit SHA and the file counts from target/rewrite/datatables/.

### Day 2+: Phase 2 — Struts removal, file by file

For each Struts file:

1. Open the file in the editor.
2. Switch Continue to Plan mode.
3. Pull context: `@<the file> @<related ActionForm> @<struts-config.xml> @MIGRATION_LOG.md`
4. Type: "Plan the migration of this file per /modernize."
5. Review the plan. Push back where needed.
6. Switch to Agent mode. Type: "Execute the plan."
7. Review the diff in the IDE.
8. Run tests: `mvn -pl <module> test`.
9. Commit: code + log update in one commit.

### Phase 3 and 4 work the same way

Same loop, different files. Phase 3 (JSPs) and Phase 4 (Java 21 polish) are
each just sequences of single-file migrations.

### When something goes wrong

The agent edited a file badly:
```bash
git restore <file>
# Try again with more context, or a tighter prompt
```

The agent's output looks fine but tests fail:
- Don't commit. Switch back to Plan mode and ask: "Tests are failing with
  X. Read the test and the source. What's wrong?"
- Often the agent forgot to update a related file.

The agent is stuck in a loop:
- New chat. Re-pull context including `MIGRATION_LOG.md`. Long contexts
  drift; fresh sessions help.

The migration is taking longer than expected:
- That's normal. Real Struts → Spring is roughly one engineer-week per 50
  controllers, regardless of LLM. The agent saves typing, not thinking.

---

## Quick reference card

| Want to do this                                | Do this                                              |
|------------------------------------------------|------------------------------------------------------|
| Apply rules every time                         | `.continue/rules/00-stack.md` with `alwaysApply: true` |
| Apply rules only to .java files                | `globs: "**/*.java"` in frontmatter                  |
| Invoke a specific migration transformation     | `/modernize` slash command                           |
| Stop the agent from writing without permission | Tool policy: Ask First on edit_file                  |
| Investigate before acting                      | Plan mode (Cmd/Ctrl + . to switch)                   |
| Batch over many similar files                  | `cn -p` in a shell loop                              |
| Get the agent to remember progress             | `@MIGRATION_LOG.md` in every session                 |
| Lock down for reproducibility                  | `temperature: 0.1`, pin model versions               |
| Reuse context across team members              | Commit `.continue/` to git                           |
| External tool access                           | MCP server in `.continue/mcpServers/`                |
