# prompts/

Prompts that are **sent to a model at runtime**, and the contract they are written against.

Build prompts used to live here too — the ones FORGE itself was constructed from. They have moved
next to what they describe: [`forge-mvp/PHASE0-SPEC.md`](../forge-mvp/PHASE0-SPEC.md) and
[`forge-terraform/SPEC.md`](../forge-terraform/SPEC.md). Nothing in this directory is history now.

## `packs/` — the runtime prompts

18 `*.pack.md` files, one technology transition each. **These are executed.** `forge/packs/loader.py`
parses them at startup, and each pack's `## transform` and `## review` sections are sent verbatim to
Bedrock as system prompts during a migration. Editing one changes what the next run does.

They sit at the repository root rather than under `forge-mvp/` because `loader.py` resolves this
directory by walking up from `forge/packs/`. Override with `FORGE_PACKS_DIR`.

Writing one: [forge-mvp/EXTENDING.md](../forge-mvp/EXTENDING.md).

## `FORGE-Platform-Requirements.md` — the contract behind them

Current, and cited by the code as its authority: `forge/packs/spec.py`, `forge/packs/__init__.py`,
`forge/intent/vocabulary.py`, `forge/discover/emit.py`, `forge/ui/app.py` and
`forge-terraform/scripts/generate-agents-yaml.sh` all point here. §1 is the pack contract, §2 the
library, §4 the decision vocabulary.

It stays beside `packs/` because that is what it governs — the loader and the intent layer are
implementations of it. Change this and the code changes with it.

---

For what FORGE does today: [forge-mvp/USING-FORGE.md](../forge-mvp/USING-FORGE.md) to use it,
[forge-mvp/ARCHITECTURE.md](../forge-mvp/ARCHITECTURE.md) for how it works.
