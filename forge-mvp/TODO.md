# FORGE MVP — Next-cycle backlog

Actionable backlog for the next development cycle. Context for each item lives in
[ARCHITECTURE.md](ARCHITECTURE.md) §11 (Known gaps). Ordered by priority.

## P0 — Build / compile verification (the missing safety gate)

- [ ] **Add a `compile_check` node** to the LangGraph pipeline ([forge/graph.py](forge/graph.py))
      after `guardrails_post` / `write_file`. Run `mvn -q compile` (or `gradle compileJava`) on the
      migrated tree via `subprocess`. This converts the reviewer's *opinion* of "syntactically valid"
      into a *fact*.
- [ ] **Route compile failures into the existing retry loop** — non-zero exit → re-enter
      `java_upgrade` with the `javac`/build errors injected as feedback (mirror the
      `route_reviewer` retry mechanism); exhausted retries → `manual_queue`.
- [ ] Make the build tool + command configurable in [agents.yaml](agents.yaml)
      (`build_tool: maven|gradle|none`, `build_command`, working dir).
- [ ] Capture and persist compiler output in `FileStatus` (new field, e.g. `compile_errors`) so it
      shows in the audit trail and `migration-report.md`.
- [ ] Decide granularity: per-file compile is unreliable (no classpath) — likely a **whole-module
      compile pass** after a batch of files is written, not per-file. Note the trade-off in the report.

## P1 — Verification quality

- [ ] **Give the reviewer project context.** Today it sees one file truncated to 8,000 chars
      ([java_reviewer.py](forge/review/java_reviewer.py)) — no classpath, no `pom.xml`, no cross-file
      refs. Consider feeding dependency/`pom.xml` context, and handle files >8,000 chars (chunk or raise).
- [ ] **Wire RAG.** `knowledge_base_id` is empty; no agent retrieves from the Bedrock KB. Hook the
      transform/review agents to the KB so enterprise standards actually ground the output.

## P2 — Cost & config hardening

- [ ] **Add `--estimate-cost` dry-run** to [migrate.py](migrate.py) — it already counts
      `bedrock_calls`; project spend before a real run (~$0.07/avg file; see cost analysis).
- [ ] **Cheaper guardrail checks.** `guardrails_pre` / `guardrails_post` use Sonnet 4.5 for their LLM
      pass (~$0.017/file). Switching those two to Haiku 4.5 cuts per-file cost ~25% with little quality
      loss on a yes/no safety check.
- [ ] **Resolve the placeholder guardrail.** `agents.yaml` ships
      `guardrail_id: "REPLACE_WITH_GUARDRAIL_ID"` — the first node fails without a real ID. Document
      creating the Guardrail (Terraform `foundation` module) and generating `agents.yaml` from outputs.
- [ ] **Externalize the remaining prompts** the same way as `java_upgrade.md` — `java_reviewer`,
      `guardrails_pre`, `guardrails_post` still have inline `_SYSTEM` strings. Loader already exists
      ([forge/utils/prompts.py](forge/utils/prompts.py)); each is a ~3-line change.

## P3 — Roadmap (beyond Phase 0, from the deck)

- [ ] Build the next transform/review agent pairs (Spring X→Y, Struts2→MVC, Discovery, Risk-Scorer,
      Containerize, Test-Gen) — currently only `java21` exists.
- [ ] **Test-Gen agent** + run generated JUnit 5 tests as a second verification gate (complements the
      compile gate above).
- [ ] **Review portal** (`review_portal.py`) over `manual-review-queue.json` for human approve/reject.
- [ ] Fix the stale line in [../CLAUDE.md](../CLAUDE.md): it says `forge-mvp/` is "not yet built" — it is.
