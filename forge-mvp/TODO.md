# FORGE MVP — Next-cycle backlog

Actionable backlog for the next development cycle. Context for each item lives in
[ARCHITECTURE.md](ARCHITECTURE.md) §11 (Known gaps). Ordered by priority.

## P0 — Done

- [x] Build / compile gate — `verify_build` node, `javac`/`maven`/`command` modes, compiler
      errors fed back through the retry loop (`forge/verify/build_verifier.py`).
- [x] Pack library and loader — `prompts/packs/`, `forge/packs/`, `--list-packs`.
- [x] `web_bootstrap` context extractor — `web.xml`, vendor descriptors, Liberty `server.xml`
      with full context in the transform and review prompts; generated units.
- [x] Acceptance runner — `--acceptance` / `--acceptance-only`; `no_match`, `count_unchanged`,
      `test_parity`, `authz_parity`, `build`; verdict INCOMPLETE while anything is skipped.
- [x] Discovery — `--discover`; stack profile, BOM-aware version resolution, pack activation
      with evidence, `forge-profile.yaml`.

## P1 — Verification quality

- [ ] **More context extractors.** `web_bootstrap` is built. Packs still waiting on one:
      `struts_routing_table` (unblocks `struts1/2-to-springmvc6`), `spring_bean_graph`,
      `view_bindings`, `reactor`, `test_subject`. Each is a deterministic parser under
      `forge/extract/` registered like `web_bootstrap`; the ratchet test in `tests/test_packs.py`
      lists them.
- [ ] **`routing_parity` check.** Declared by the Struts packs, skipped by the runner until the
      `struts_routing_table` extractor exists to diff pre/post action tables.
- [ ] **Run from the profile.** `--discover` writes `forge-profile.yaml`; nothing reads it yet. A
      `--profile` run should iterate the activated packs in order, applying each pack's checks.
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
- [ ] Module-level build gate — `mvn -pl <module> -am compile` after a batch, in addition to the per-file gate.
