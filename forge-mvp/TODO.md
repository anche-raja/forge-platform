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
- [x] Human in the loop — risk scorer, `risk_ceiling` hold gate, review queue v2 + static review
      page, `--apply-decisions`, `--feedback-report`.
- [x] Local web UI — `migrate.py --ui`; `forge/service.py` shared by CLI and UI, FastAPI routes,
      one-at-a-time job registry with SSE progress, live review with Apply, acceptance, feedback
      and artifact views (`forge/ui/`).
- [x] **Test-Gen agent** — `forge/testgen/`, `--generate-tests` / `--generate-tests-only` /
      `--run-tests`, the Tests step in the UI. Deterministic target selection and destination,
      mechanical checks ahead of the review, a failing test staged rather than written
      (ARCHITECTURE.md §14).
- [x] **Intent** — `--discover --intent "..."`; one model call maps a sentence onto decisions,
      scope and a narrowed pack set. It can only narrow, never activate (INTENT.md).
- [x] **The chat leader** — `forge/leader/`, twelve tools, the admission gate, the trust boundary,
      spend confirmation and `land_on_branch`. The wizard is gone; chat is the only surface
      (ARCHITECTURE.md §16).
- [x] **Packs no longer clobber each other.** `run_manifest` records who wrote what and
      `PackOverlap` refuses before any spend; the leader chains instead, reading each pack's units
      from the merged source ⊕ output view.
- [x] **A degraded run says so.** A pack whose declared context extractor is unbuilt still runs,
      but `--list-packs` labels it, the run warns, the report carries a caveat and every affected
      unit is stamped `context_missing`.
- [x] **Struts → Spring MVC removed.** Both packs and the `struts-spring6` built-in phase, plus the
      `migrate-to-spring` decision value. They needed an extractor that was never built, so they
      could be selected and never run.

## P1 — The one that matters

- [ ] **Measure whether the output is any good.** Every test mocks the model, so the suite proves
      orchestration and says nothing about migration quality. No pass rate from a real multi-file
      run exists anywhere. Run one AMS module end to end and publish the numbers — pass / manual /
      blocked counts, the score distribution against the threshold of 80, real cost per file, and
      how long human review actually took. Publish them **including if they are bad**; a pilot
      written up only on success is not a measurement. At ~$0.024/file this is a $2-3 experiment.
      Two signals say do it before committing to a full run: files have scored *exactly* 80 against
      a `pass_threshold` of 80 three times, and `java8-to-java21` failed to return valid JSON twice
      out of two attempts on Sonnet 4.5.

## P2 — Verification quality

- [ ] **More context extractors.** `web_bootstrap` is built. Four *runnable* packs declare one that
      is not, and so migrate each file on its own contents alone with the reviewer's cross-check
      gone: `reactor` (`build-maven-modernize`), `spring_bean_graph` (`spring-to-spring6`),
      `view_bindings` (`jsp-jstl-modernize`), `test_subject` (`junit4-to-junit5`).
      `spring-to-spring6` is the one to build first — cross-file wiring is most of its difficulty.
      Each is a deterministic parser under `forge/extract/` registered like `web_bootstrap`; the
      ratchet test in `tests/test_packs.py` lists them.
- [ ] **`routing_parity` check.** Declared by `struts2-modernize`, skipped by the runner until the
      `struts_routing_table` extractor exists to diff pre/post action tables.
- [ ] **Run from the profile.** `--discover` writes `forge-profile.yaml`; nothing reads it yet. The
      chat leader now sequences a plan itself (ARCHITECTURE §16), so this is the CLI's half: a
      `--profile` run that iterates the activated packs in order, chaining each into the next.

## P3 — Cost & config hardening

- [ ] **Add `--estimate-cost`** to [migrate.py](migrate.py). It already counts `bedrock_calls`, and
      the per-unit cost is now measured rather than assumed: **$0.0080 fixed + $0.0000066/byte** on
      Sonnet 4.5, calibrated on two real runs. That puts all ten packs across AMS at ~$45.
- [ ] **Cheaper guardrail checks.** `guardrails_pre` / `guardrails_post` use the transform model for
      their LLM pass. Switching those two to Haiku 4.5 cuts per-file cost with little quality loss
      on a yes/no safety check.
- [ ] **Model access is account-gated and fails late.** Opus 4.8 and Opus 5 both return
      `AccessDeniedException` on the deployment account, and the error arrives on the first call of
      a run rather than at config load. A one-token `converse` probe at startup would turn a
      mid-run failure into a startup message.

## P4 — Roadmap (beyond Phase 0, from the deck)

- [ ] Build the next transform/review agent pairs (Spring X→Y, Containerize) — Discovery,
      Risk-Scorer and Test-Gen are done, the first two deterministically. Struts → Spring MVC was
      removed deliberately; Struts is modernised in place.
- [ ] **Integration tests, and coverage.** Test-Gen writes unit tests with collaborators mocked. A
      Spring-context slice test (`@WebMvcTest`, `@DataJpaTest`) and a JaCoCo coverage gate are the
      next rungs; neither is built.
- [ ] Module-level build gate — `mvn -pl <module> -am compile` after a batch, in addition to the per-file gate.
