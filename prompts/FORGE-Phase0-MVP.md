# FORGE — Phase 0: MVP
# Java Upgrade Agent — Prove the full pipeline works end to end

## Goal
Build the minimum working FORGE system. One transform agent (Java X→Y). One review agent (Java reviewer). Full pipeline from CLI to DynamoDB to LangSmith. Nothing else. Every architectural decision made here becomes the foundation all future phases build on.

## What "done" looks like for this phase
Run this command against a real Java 8 codebase:

  python migrate.py ./myapp --phase java21 --file src/main/java/com/corp/UserAction.java

And see:
- Bedrock Guardrails evaluated the file (INPUT check)
- Claude Sonnet 4.5 replaced javax.* with jakarta.* and modernised Date/Time/deprecated APIs
- Amazon Nova Pro reviewed the output and returned a score 0-100
- If score >= 80: file written to ./migrated/myapp/ with correct package path
- If score 50-79: Claude retried with Nova Pro feedback injected (max 2 retries)
- If score < 50: file pushed to manual-review-queue.json with full context
- DynamoDB record shows final status, score, retry count, model pair used
- LangSmith shows the full trace: every prompt sent, every tool called, every state transition
- migration-report.md generated showing the outcome

## Tech stack (do not add anything not listed here)
- Python 3.11+
- LangGraph for the pipeline graph
- LangChain (ChatBedrockConverse for Claude Sonnet 4.5, ChatBedrockConverse for Nova Pro)
- AWS Bedrock — Claude Sonnet 4.5 (transform), Amazon Nova Pro (review)
- AWS Bedrock Guardrails — standalone ApplyGuardrail API (not inline)
- AWS DynamoDB — one table: forge-migration-state
- LangSmith — enabled via environment variables only, no code change needed
- PyYAML for agents.yaml config
- python-dotenv for .env

No LiteLLM yet. No SQS yet. No OpenHands yet. No RAG yet. No Discovery agent yet.

## Folder structure to build

forge-mvp/
  migrate.py
  agents.yaml
  .env.example
  requirements.txt
  forge/
    __init__.py
    config.py
    state.py
    graph.py
    agents/
      __init__.py
      base.py
      guardrails_pre.py
      guardrails_post.py
      java_upgrade.py
    review/
      __init__.py
      base_reviewer.py
      java_reviewer.py
    guardrails/
      __init__.py
      bedrock_guardrails.py
    state_store/
      __init__.py
      dynamodb.py
    utils/
      __init__.py
      file_scanner.py
      file_writer.py
      report.py
  tests/
    test_graph.py
    test_java_upgrade.py
    test_guardrails.py
  infrastructure/
    create_dynamodb.py

## State schema — forge/state.py
Two TypedDicts. FileStatus tracks one file. ForgeState is the full graph state.

FileStatus fields: file_path, status (PENDING/TRANSFORMING/REVIEWING/RETRY_1/RETRY_2/DONE/MANUAL_REVIEW/BLOCKED), phase, risk_tier (hardcode UNSCORED for MVP), risk_score (hardcode 0), transform_output, review_score, review_verdict (PASS/RETRY/MANUAL), review_feedback, guardrail_pre_verdict, guardrail_post_verdict, guardrail_findings (list), retry_count, transform_model, review_model, error.

ForgeState fields: current_file (FileStatus), phase, dry_run, source_dir, output_dir, target_java_version, target_spring_version, files_processed, files_passed, files_retried, files_manual, files_blocked, bedrock_calls, estimated_cost_usd, messages (list).

## LangGraph graph — forge/graph.py
Nodes: guardrails_pre, java_upgrade, java_reviewer, guardrails_post, write_file, manual_queue, blocked, update_state.

Entry: guardrails_pre.

Routing:
- After guardrails_pre: BLOCK → blocked, else → java_upgrade
- After java_reviewer: score>=80 → guardrails_post, score 50-79 and retry_count<2 → java_upgrade (with feedback in state), else → manual_queue
- After guardrails_post: BLOCK → manual_queue, else → write_file
- write_file → update_state → END
- manual_queue → update_state → END
- blocked → update_state → END

Use DynamoDBSaver as the checkpointer. thread_id = file_path.

## Bedrock Guardrails — forge/guardrails/bedrock_guardrails.py
Wrap the standalone ApplyGuardrail API. Method: evaluate(text, source). source is "INPUT" or "OUTPUT". Returns dict with action ("NONE" or "GUARDRAIL_INTERVENED"), findings list, intervened bool. This is NOT inline with model invocation. It is a separate boto3 call to bedrock-runtime.apply_guardrail. It uses ML classifiers, not a generative LLM — works regardless of which LLM generated the text.

## Guardrails Pre Agent — forge/agents/guardrails_pre.py
Two steps in sequence:
Step 1 — Call Bedrock Guardrails (ApplyGuardrail, source=INPUT). If GUARDRAIL_INTERVENED: set status=BLOCKED, return state immediately.
Step 2 — Call Claude Sonnet 4.5 with a short prompt. Ask it to check: secrets/credentials in code, file within scope (correct package prefix from agents.yaml), PII in comments/strings, file size vs complexity threshold. Return JSON verdict (PASS/WARN/BLOCK) and findings list. If BLOCK: set status=BLOCKED. If WARN: add to findings but continue.

## Java Upgrade Agent — forge/agents/java_upgrade.py
System prompt instructs Claude Sonnet 4.5 to apply these rules in order:
Rule 1 — Namespace migration (highest priority): every javax.servlet.* → jakarta.servlet.*, javax.persistence.* → jakarta.persistence.*, javax.validation.* → jakarta.validation.*, javax.transaction.* → jakarta.transaction.*. Zero javax.* allowed in output.
Rule 2 — Deprecated API replacement: Thread.stop() → throw InterruptedException. finalize() → flag with comment. StringBuffer in loops → StringBuilder. System.runFinalizersOnExit() → remove.
Rule 3 — Date/Time modernisation: new Date() for current time → Instant.now(). Calendar → LocalDateTime. SimpleDateFormat → DateTimeFormatter. java.sql.Date → java.time equivalent.
Rule 4 — Local variable inference (conservative): var only where type is obvious from right-hand side. Never on parameters or fields.
Rule 5 — Flag but do not change: sun.misc.Unsafe → add MANUAL comment. Illegal reflective access → add MANUAL comment.

Return JSON with: files dict (path→content), manual_flags list (file+line+reason).

The agent must also inject review feedback into the prompt when retry_count > 0. The feedback comes from state.current_file.review_feedback.

## Java Reviewer — forge/review/java_reviewer.py
Uses Amazon Nova Pro (different model from transform — genuine cross-validation). System prompt: check these five things and score 0-100.
Check 1 (20pts): Zero javax.* imports remain.
Check 2 (20pts): No deprecated APIs (Thread.stop, finalize, Calendar, SimpleDateFormat).
Check 3 (25pts): Date/Time modernisation is correct and complete.
Check 4 (20pts): var inference applied only where safe and type is obvious.
Check 5 (15pts): No regressions — original structure, error handling, and null checks preserved.

PASS >= 80. RETRY 50-79. MANUAL < 50. Return JSON: score, verdict, feedback (specific issues for retry), checks dict.

## Guardrails Post Agent — forge/agents/guardrails_post.py
Two steps:
Step 1 — Call Bedrock Guardrails (ApplyGuardrail, source=OUTPUT). If GUARDRAIL_INTERVENED: BLOCK.
Step 2 — Call Claude Sonnet 4.5. Check: zero javax.* imports in output, no deprecated patterns remain, package naming follows enterprise convention from agents.yaml, no introduced security issues. Return JSON verdict and findings.

## DynamoDB State Manager — forge/state_store/dynamodb.py
Table name from agents.yaml. Methods: put_file_status(file_status), get_file_status(file_path), get_files_by_status(status), get_progress_summary(), mark_pending(file_paths, phase). Create a GSI on the status attribute so get_files_by_status is efficient. Use PAY_PER_REQUEST billing. Create the table in infrastructure/create_dynamodb.py.

## File Scanner — forge/utils/file_scanner.py
Walk source_dir recursively. For phase=java21 return all .java files. Filter out: generated code (contains "DO NOT EDIT"), test files (path contains src/test) unless explicitly included, binary files. Return list of relative paths.

## File Writer — forge/utils/file_writer.py
If dry_run=True: do nothing, return. Otherwise: read transform_output from state (JSON dict of path→content). For each file: create directory structure under output_dir, write content. Preserve the package path exactly.

## CLI — migrate.py
Arguments: source_dir (positional), --phase (required, choices: java21), --dry-run, --resume, --file (single file path), --output-dir (default: ./migrated).

For --resume: query DynamoDB for PENDING files, process only those.
For --file: process only that one file.
Default: scan source_dir, mark all .java files PENDING in DynamoDB, process in order.

Print progress: [N/total] filename → STATUS (score: N).
At the end: print summary and path to migration-report.md.

## Configuration — agents.yaml
Include: transform_model, review_model, aws_region, dynamodb_table, guardrail_id, guardrail_version, source_java_version, target_java_version, pass_threshold (80), retry_threshold (50), max_retries (2), scope_package_prefix (for scope validation), complexity_block_threshold (2000 LOC), langsmith_project.

## Report — forge/utils/report.py
Generate migration-report.md with: run timestamp, phase, source_dir, files scanned, files passed, files retried, files manual, files blocked, total Bedrock calls, per-file table (path, status, score, retries, guardrail findings).

## Tests — tests/
test_graph.py: test that a mock file flows through the full graph and ends at DONE with correct state fields.
test_java_upgrade.py: test that the agent prompt is assembled correctly when retry_count=0 and retry_count=1 (feedback injection).
test_guardrails.py: test that GUARDRAIL_INTERVENED from Bedrock Guardrails results in BLOCKED status and the graph terminates at blocked node.

## Acceptance criteria — phase 0 is complete when
1. python migrate.py ./myapp --phase java21 --file path/to/AnyFile.java runs without error.
2. DynamoDB shows a record for that file with status DONE, a review score, and the model pair used.
3. LangSmith shows a trace with every node that ran and every LLM call made.
4. ./migrated/myapp/path/to/AnyFile.java exists and contains jakarta.* instead of javax.*.
5. All three tests pass.
6. python migrate.py ./myapp --phase java21 --dry-run runs the full pipeline but writes no files and makes no DynamoDB changes.
