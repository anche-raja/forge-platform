# FORGE — Intent

> **Scope note.** How a sentence becomes a pack selection: what the model is allowed to decide,
> what it is structurally prevented from deciding, and where each answer is checked. The
> deterministic half — profiling a repository and firing `detect` rules — is
> [ARCHITECTURE.md](ARCHITECTURE.md) §12; the decision vocabulary is specified in
> [prompts/FORGE-Platform-Requirements.md](../prompts/FORGE-Platform-Requirements.md) §4. This
> document is the layer between them.

---

## 1. The gap this fills

Discovery answers *what is in this repository*, mechanically, from evidence. It cannot answer the
question that is left over: **which of the routes the evidence allows did you actually want?**

That question is real. `struts2-modernize` and `struts2-to-springmvc6` fire on identical evidence —
`struts2-core`, `com.opensymphony.xwork2`, `struts*.xml` — because both are genuine options for the
same repository. Ten `decisions` keys arbitrate this and eight more like it, and until now every one
of them was set by a human editing `forge-profile.yaml` by hand. `emit.py` says so in a comment:
*"the generated profile spells them out so a reader edits values, not absences."*

So a request like

> migrate to the latest Java and Spring, stay on Struts, ignore the db folder as these are sqls

contained three usable facts — a target, a route, a scope exclusion — and the platform discarded all
three. This layer reads them.

---

## 2. Two boundaries, and why they are the design

### The model may narrow, never invent

`resolve_packs` remains the only thing that decides what a repository contains. A pack with no
`detect` match cannot be activated by any prompt, in any phrasing. Ask for Hibernate in a project
with no `hibernate-core` and the answer is a line in `unsupported`, not an activation.

This is what keeps the feature on the right side of the standing rule in
[CLAUDE.md](../CLAUDE.md) — *"do not add a model-driven leader… a mechanical question handed to a
model."* Pack activation **is** mechanical and stays mechanical. Intent→decisions is not: it is the
one genuinely linguistic step in the system, and the thing it replaces is a human editing YAML, not
the evidence engine.

The rule's other objection — *"it would make the order of a run non-reproducible"* — is answered
structurally. The order comes from `PackRegistry.resolve_order`, the same topological sort discovery
uses. The model never orders anything, and the resolved plan is written to `forge-profile.yaml` with
full provenance, so a second run replays it with **zero** model calls.

### No source code reaches the model

The intent agent is given `Profile.to_json()` — build system, Java level, resolved dependency
coordinates, import *prefixes* with counts, descriptor *filenames*, file counts. Never a file's
contents. `_profile_block` in [forge/intent/agent.py](forge/intent/agent.py) assembles those fields
one by one rather than dumping the profile, and `test_the_agent_is_never_given_source_code` asserts
it.

This is [GUARDRAILS.md](GUARDRAILS.md) §7 one layer up: a model is never the control that decides
what a model may see. An intent layer that read the tree to decide what to migrate would be the
disclosure the secret gate exists to prevent, moved somewhere nobody was looking for it.

---

## 3. The flow

```
prompt ───┐
          ├──▶ intent agent ──▶ proposal ──┐
profile ──┘    (1 model call,              │
               metadata only)              ├──▶ reconcile() ──▶ IntentPlan
                                           │    (pure, deterministic)
resolve_packs(profile, packs) ─▶ activations┘         │
    (unchanged, evidence)                              ▼
                                            forge-profile.yaml + intent-plan.json
```

The model proposes; `reconcile` disposes. Every safety property lives in the second box, which is
pure Python — so the adversarial cases are ordinary unit tests with no AWS anywhere.

---

## 4. The model's output contract

A prompt-declared JSON skeleton, parsed with the shared `extract_json`. There is no `bind_tools`
and no schema validation anywhere in this codebase; the caller coerces defensively.

```json
{
  "decisions":   {"web_framework": "modernize-in-place"},
  "include":     ["javax-to-jakarta", "spring-to-spring6"],
  "exclude":     [{"pack": "junit4-to-junit5", "reason": "leave the tests alone"}],
  "scope":       {"exclude_globs": ["db/**"], "package_prefix": "org.example.am"},
  "unsupported": [{"asked": "hibernate", "reason": "no hibernate-core in the profile"}],
  "assumptions": ["'latest' read as the platform target"],
  "questions":   ["Should the JSPs stay as JSP, or move to Thymeleaf?"]
}
```

**There is no retry.** A response that will not parse becomes `None`, and `reconcile(None, …)`
returns the plan discovery would have produced on its own. A guess is worse than a default.

---

## 5. The eight rules

`reconcile` in [forge/intent/resolve.py](forge/intent/resolve.py). Each has a test in
`tests/test_intent.py`.

| # | Rule | What it prevents |
|---|---|---|
| 1 | `include ⊆ activated` | A prompt inventing work the codebase does not contain. Anything else becomes `unsupported`. |
| 2 | Nothing silently dropped | Every set-aside candidate keeps its reason **and its evidence**. Requirement 7 of the platform spec. |
| 3 | Closed vocabulary | An unknown decision key or an out-of-enum value is rejected, not merged. This also closes a hole where the UI let unknown *keys* through into the config. |
| 4 | Mutual exclusion | Both Struts routes active at once — *"a configuration error; they edit the same files toward different targets."* |
| 5 | State labels survive | A `detect-only` or `blocked` pack being silently promoted to runnable. |
| 6 | Coherence check | A narrowed set that drops a dependency it still needs. Uses `registry.missing_dependencies`, which existed and was never called. |
| 7 | Order from `resolve_order` | A model-chosen run order. |
| 8 | Provenance on every decision | An unexplainable plan. Each value is `prompt`, `config` or `default`. |

Rule 6 is narrowed to dependencies that *were* available and got dropped anyway. An edge pointing at
a pack the repository never had is the benign either/or case, and so is the losing half of rule 4 —
`jsp-jstl-modernize` names both Struts packs precisely because it must follow whichever one runs.

### Assumptions are computed, not just relayed

Beyond whatever the model reports, `reconcile` walks the selected packs' declared `decisions` and
flags every one still sitting on a platform default. That is the useful subset: not every default
matters, only the ones a pack in this plan will actually read.

It also surfaces a decision with **no value anywhere**. `DEFAULT_DECISIONS` is missing
`liberty_edition` and `liberty_features`, both of which the spec table lists and
`liberty-server-config` reads — an absence a reader would otherwise have to notice for themselves.

---

## 6. Scope

`scope.exclude_globs` and `scope.package_prefix` were declared in the generated profile and read by
nothing. They are wired now: `scan_java_files` takes `exclude_globs` and matches with the same
`glob_match` that `file_glob` detect rules use, and an excluded path the phase would otherwise have
taken is recorded as a `SkippedFile`, never dropped silently.

Exclusion needs no ceiling because it can only ever shrink the unit set — so it can only reduce cost
and blast radius. "Ignore the db folder" is therefore always safe to honour.

---

## 7. A decision can re-gate discovery

`liberty-server-config` is gated on `decision_equals: {key: container, value: liberty}`. If the
prompt asks for Tomcat, that pack must stop firing — but the activations were computed before the
prompt was read.

`service.discover` handles it by re-deriving: `resolve_packs` is pure over `profile.decisions`, so
when the resolved decisions differ from the ones the profile was built with, it re-activates and
re-reconciles **the same proposal**. No second model call, and the result is still deterministic.

---

## 8. Cost

One call. The profile is a few KB — roughly 2k tokens in, 600 out. On Haiku 4.5 that is about
**half a cent**; on Opus, about two and a half. This is classification over a closed vocabulary, not
reasoning over code, so `agents.yaml` defaults `intent.model` to Haiku.

Whatever model is named there **must** also appear in `model_pricing`, or `estimate_cost` returns
`0.0` and the cost accrues silently as zero.

---

## 9. Using it

```bash
# CLI — one flag on --discover
python migrate.py /path/to/app --discover \
  --intent "migrate to the latest Java and Spring, stay on Struts, ignore the db folder"

# UI — step 2
python migrate.py --ui
```

`--discover` without `--intent` is unchanged: no model, no AWS, free.
`tests/test_intent.py::test_discover_without_intent_makes_no_model_call` pins that, and it is the
test to keep green above all the others here — it is the contract the whole deterministic path
rests on.

Artifacts: the plan is written to `intent-plan.json`, and `forge-profile.yaml` gains the provenance
comments, the set-aside packs and the resolved scope.
