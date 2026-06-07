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
- [x] **Wire RAG.** Done as **prompt-stuffing** (`rag_mode: prompt_stuff`): the transform + review
      agents inject relevant enterprise-standards docs selected by file type
      ([forge/rag/corpus.py](forge/rag/corpus.py), [retriever.py](forge/rag/retriever.py)). The
      Bedrock KB path (`rag_mode: knowledge_base`) is a stubbed future branch — avoids ~$175/mo
      OpenSearch. Reviewer-context (above) would further improve grounding.

## P2 — Cost & config hardening

- [ ] **Add `--estimate-cost` dry-run** to [migrate.py](migrate.py) — it already counts
      `bedrock_calls`; project spend before a real run (~$0.07/avg file; see cost analysis).
- [ ] **Cheaper guardrail checks.** `guardrails_pre` / `guardrails_post` use Sonnet 4.5 for their LLM
      pass (~$0.017/file). Switching those two to Haiku 4.5 cuts per-file cost ~25% with little quality
      loss on a yes/no safety check.
- [x] **Resolve the placeholder guardrail.** Live config (`agents.local.yaml` / generated
      `agents.yaml`) carries a real guardrail id; the `REPLACE_WITH_GUARDRAIL_ID` placeholder only
      remains in the committed template `agents.yaml`.
- [x] **Externalize the remaining prompts.** `java_reviewer`, `guardrails_pre`, `guardrails_post`
      now load from `prompts/*.md` via [forge/utils/prompts.py](forge/utils/prompts.py).

## P3 — Roadmap (beyond Phase 0, from the deck)

- [ ] Build the next transform/review agent pairs (Spring X→Y, Struts2→MVC, Discovery, Risk-Scorer,
      Containerize, Test-Gen) — currently only `java21` exists.
- [ ] **Test-Gen agent** + run generated JUnit 5 tests as a second verification gate (complements the
      compile gate above).
- [x] **Review portal** — [review_portal.py](review_portal.py) (Streamlit): lists MANUAL_REVIEW
      files from DynamoDB, renders the transform diff, Approve (write + DONE) / Reject (re-enqueue).
- [x] Fix the stale line in [../CLAUDE.md](../CLAUDE.md) — done; it no longer says "not yet built".
