# Using FORGE

**You talk to FORGE in a chat window. That's the whole interface.**

You say what you want in plain English. A leader agent works out which technology packs your
repository needs, runs them in the right order, shows you anything that needs a human decision, and
puts the result on a git branch. You never edit a config file or run a command.

```bash
cd forge-mvp
python migrate.py --ui
```

That opens http://127.0.0.1:8765. Everything below happens in that window.

---

## A whole migration, as a conversation

> **you** — migrate the app at ~/work/ams

> **FORGE** — Found it: Maven, 10 modules, Java 8. Struts 6.8.0, Spring 5.3.39, Spring Security
> 5.3.13, JUnit 4, 376 Java files and 61 JSPs.
>
> Ten packs apply, in dependency order: build-maven-modernize, java8-to-java21, javax-to-jakarta,
> spring-to-spring6, springsec-to-springsec6, struts2-modernize, jsp-jstl-modernize,
> junit4-to-junit5, webapp-bootstrap-jakarta10, liberty-server-config.
>
> Nothing has run and nothing has been spent. Want me to start?

> **you** — yes

> **FORGE** — javax-to-jakarta covers 336 files, about **$10.06**. That's over the confirmation
> limit, so it needs your click. *[ Run ] [ Not now ]*

You click **Run**. It streams progress, then:

> **FORGE** — 331 passed, 4 held for review, 1 needs manual attention. $9.84 spent.
>
> Four files are HIGH risk and staged rather than written — they touch security configuration.
> Here they are. *[review cards with diffs, and Approve / Reject / Retry on each]*

You review them in the chat, click **Apply**, and say:

> **you** — carry on

FORGE runs the next pack. It **builds on what the last pack produced**, so the Java 21 pack sees
`jakarta.servlet`, not the original `javax.servlet`. When the plan is done:

> **you** — put it on a branch

> **FORGE** — Landed 372 files on `forge/jakarta-migration` (from `h2-native`), commit `a1b2c3d`.
> Nothing was pushed. Want a pull request? *[confirmation card]*

> **you** — *[clicks Confirm]*

> **FORGE** — Pull request opened: https://github.com/you/ams/pull/42

---

## What it costs

FORGE tells you before it spends anything over your limit, and anything over **$1** stops for a
click. The estimate is units × **$0.07**, where a unit is one file through one pack. That $0.07 is
`leader.unit_cost_usd`: a planning figure for three model calls with Claude Opus 4.8 as the
transform model, not a measurement. What a run actually spent is added up from each real call, and
that is the number it reports afterwards.

| Planning estimate, Opus 4.8 | |
|---|---|
| One file | **$0.07** — three model calls |
| All ten packs across AMS (420 units) | **~$29**, before retries |

The measured figures are older. They come from **Sonnet 4.5**, the transform model before Opus 4.8,
calibrated on two real runs at $0.0080 fixed plus $0.0000066 per byte:

| Measured, Sonnet 4.5 | |
|---|---|
| One file | **~$0.024** — three model calls |
| All ten packs across AMS (1,480 units then) | **~$45** |
| With a realistic retry rate | **$48 – $57** |

Don't read these as today's prices: Opus 4.8 costs more per token than Sonnet 4.5, and the four
packs that took every Java file now select by content, which took AMS from 1,480 units to 420.

By default a file the reviewer scores 50–79 is transformed again with the reviewer's feedback, up to
twice (`max_retries`); each retry adds two calls. The Sonnet 4.5 range assumed 10–40% of files
retry once.

> **Nothing costs money until you click.** Profiling your repository, listing packs and planning the
> work are all free. Only the actual migration spends.

---

## What you can say

You don't need special phrasing. These all work:

- *migrate the app at ~/work/ams*
- *what would it take to get to Java 21?*
- *ignore the db folder, those are just SQL scripts*
- *what needs my review?*
- *how much have we spent?*
- *put it on a branch called forge/jakarta*
- *stop*

You can also say nothing much at all — *"migrate my app"* is enough. The leader profiles the
repository, picks the packs from the evidence it finds, and asks you before anything costs money.

**What you say can narrow the plan, never widen it.** If you ask for something your repository shows
no evidence of, FORGE says so rather than doing it. Pack selection comes from what is actually in
your code — a dependency, an import, a file — never from a guess.

---

## Reviewing files

Some files stop for you. Each one appears in the chat as a card with the diff, the risk level and
the reviewer's score.

| Why it stopped | What you do |
|---|---|
| **Held** — HIGH risk (security config, complex code) | Read the diff, then Approve / Reject / Retry |
| **Manual review** — the reviewer scored it low | Same, or Retry with a note saying what to fix |
| **Blocked** — a credential was found in the file, or it is too large | Cannot be approved: there is no transform. Fix the file |

Three buttons on every card:

- **Approve** — the file is written. This is final; if it later fails to compile, FORGE reports it
  rather than undoing your decision.
- **Reject** — the change is discarded.
- **Retry** — FORGE tries again with your note as the instruction. *"You dropped the null check"*
  is enough.

Pick your decisions, then click **Apply**. Your notes are kept, and a repeated correction becomes a
signal that the pack itself should change.

> **Your code never leaves without passing a local secret scan first.** A file containing a
> credential is refused before any network call — FORGE does not send it and then filter. And what
> the leader agent sees is deliberately narrower than what you see: it gets file paths, scores and
> counts; the diffs, the reviewer's prose and any matched secret stay in your browser.

---

## Landing on a branch

**Broken output is caught file by file.** Every migrated Java file is parsed by the Java compiler before it is reviewed. A file the model damaged, with a stray brace for example, is sent back to the model with the compiler's error and fixed automatically.

Damage an *earlier* run left behind is caught too. Before each pack that builds on the last one,
FORGE parses every file it has already written. One that no longer parses is moved aside to
`.forge-staging/.damaged/` (never deleted — even one you approved), the file reads as your original
again, and `migration-summary.md` names the pack that wrote it so you can run that pack again.

**First, FORGE builds the project.** After the last pack, the chat compiles your source with the
migrated files laid over it, using the project's own build: each Maven reactor in dependency order,
on the JDK your target Java version names. A compile catches what no reviewer can — a file a pack
missed, a stray character in a migrated file. You get a card with the result and, on a failure, the
compiler's own lines. You can also ask for it any time: *"does it build?"*

The landing confirmation shows that verdict beside the button: **passed**, **failed**, **not built
yet**, or **stale** — the migrated files changed after the build, so ask for a new one. A failed
build does not block landing; the decision is yours.

Say *"put it on a branch"*. FORGE creates the branch, copies the migrated files in, and makes one
commit. Landing itself **never pushes**.

**Opening a pull request.** After a landing, FORGE offers to open a pull request. It pushes **only
that branch**, only when you press **Confirm**: `git push -u origin <branch>` (never forced), then
`gh pr create` into the branch you were on when you landed (`h2-native` on AMS) — say another base
if you want one. This is the only way FORGE ever pushes anything. The description is written by
FORGE from its own records — the packs that ran, per-pack totals, how many files still wait on a
human, and the project build verdict, said plainly when it **failed**, was **not run**, or is
**stale** — and never contains your source, a diff or compiler output. You see it on the
confirmation card before you click. It refuses, with nothing pushed, when the repository has no
`origin` remote, when the GitHub CLI (`gh`) is missing or not signed in (`gh auth login`), or when
the branch has nothing beyond its base; if a pull request for the branch is already open, you get
its link.

It refuses rather than forcing its way past a problem:

| | |
|---|---|
| Not a git repository | Refused — it suggests `git init`, it does not run it |
| Uncommitted changes | Refused — it never stashes |
| Branch already exists | Refused — it never overwrites |

**The output lives inside your repository.** In the chat, FORGE writes the migration to a
`.migrated/` folder in the repository it is migrating (for `~/forge/ams`, `~/forge/ams/.migrated`)
unless you name another folder. FORGE never reads that folder back as source — no pack, no
discovery, no build treats it as your code. Landing adds `/.migrated/` to your clone's
`.git/info/exclude` before it checks the work tree, so FORGE's own folder never counts as an
uncommitted change and never reaches a commit. That file is local to your clone and never committed;
your `.gitignore` is not touched. The landing card says when it added the line.

FORGE's own working files — reports, the review queue — are never committed. The review queue holds
copies of your source, so it must not end up in a commit.

Only files a FORGE run wrote (recorded in `.forge-writes.json`) or a reviewer approved are
committed. Anything else sitting in the output directory — a file copied in by hand, a leftover
test fixture — is left where it is and listed in the landing result, so you can decide what it is.
`.DS_Store` and similar operating-system files are never landed. A migrated file your repository's
`.gitignore` excludes is left out and named too; all of this is decided before the branch is
created, so a refused landing leaves your repository exactly as it was.

---

## What FORGE can migrate

Ten packs run today. Each is one technology transition.

| Pack | What it does |
|---|---|
| `build-maven-modernize` | Maven reactor → Java 21 / Spring 6 WAR / Jakarta EE 10 |
| `java8-to-java21` | Java 8 → Java 21 LTS |
| `javax-to-jakarta` | `javax.*` → `jakarta.*` |
| `spring-to-spring6` | Spring Framework 4/5 → 6.2 |
| `springsec-to-springsec6` | Spring Security 4/5 → 6.3 |
| `struts2-modernize` | Struts 2 → Struts 7 |
| `jsp-jstl-modernize` | JSP + JSTL 1.x → Jakarta JSTL 3.0 |
| `junit4-to-junit5` | JUnit 4 + old Mockito → JUnit 5 + Mockito 5 |
| `webapp-bootstrap-jakarta10` | `web.xml` and vendor descriptors → Jakarta EE 10 |
| `liberty-server-config` | App-server config → Liberty `server.xml` |

Eight more technologies are **recognised but not migrated** — FORGE will tell you they are there and
that it cannot do them: Ant, Hibernate, JAX-RS, iBATIS, EJB 2, EJB 3, JMS and JSF.

**Struts is modernised in place** (Struts 2 → Struts 7), not replaced with Spring MVC. If you ask to
move off Struts, FORGE says it cannot.

### Four packs run with less information than they should

`build-maven-modernize`, `spring-to-spring6`, `jsp-jstl-modernize` and `junit4-to-junit5` each need
cross-file facts — a bean graph, a module map — that FORGE cannot yet extract. They still run, but
each file is migrated on its own contents alone, and the reviewer loses part of its cross-check.

FORGE says so in the run and marks every affected file. **Review those packs' output more closely**,
`spring-to-spring6` above all, where the cross-file wiring is most of the difficulty.

---

## Setup, once

You need AWS credentials and a generated config:

```bash
./forge-terraform/scripts/generate-agents-yaml.sh dev --out forge-mvp/agents.yaml
```

That reads your Terraform outputs. Re-run it after any `terraform apply`.

`dev` is a label, not a switch — the environment comes from the backend key you gave
`terraform init`. The script prints the state it actually read, and stops if the label disagrees
with it.

**`agents.yaml.example` will not run** — it is a reference for the keys and ships a placeholder
guardrail ID. FORGE stops with a message telling you this rather than failing mid-run.

**Check your model access.** FORGE needs Bedrock models enabled in your account. If one is not,
every run fails on the first call with `AccessDeniedException`. Whatever model you use must also
appear under `model_pricing`, or costs silently report as **$0.00**.

The settings most worth knowing:

| Setting | Default | What it does |
|---|---|---|
| `leader.confirm_above_usd` | `1.0` | Anything estimated above this stops for your click. `0` never asks |
| `decisions.risk_ceiling` | `review-high` | `review-all` holds every file; `auto` holds nothing |
| `pass_threshold` | `80` | Score at or above this is written; below it retries or stops for you |
| `scope_exclude_globs` | `[]` | Directories to leave alone. Saying *"ignore the db folder"* fills this in |

---

## If something goes wrong

| | |
|---|---|
| **"config not found: agents.yaml"** | Run the generator above |
| **`AccessDeniedException`** | Enable that model in the Bedrock console for your account |
| **"FORGE runs one job at a time"** | A run is in progress. Wait, or press Stop |
| **A run stopped part-way** | Say *"carry on"* — it picks up the files it had not finished |
| **Landing refuses: work tree not clean** | Commit or stash your own changes (FORGE's `.migrated/` is excluded for you) |

Everything FORGE produces lands in the output directory — `.migrated/` inside your repository in
the chat, `./migrated` on the command line: the migrated
tree, a report of what happened and what it cost, and the review queue. Ask for *"the artifacts"* in
chat and it lists them with download links.

A plan runs several packs into that one directory, and nothing a pack writes is overwritten by the
next one:

| File | What it holds |
|---|---|
| `migration-summary.md` | One row per pack — files, passed, manual, blocked, held, still awaiting review, cost, acceptance — and the project build. Rewritten after every run, build and decision. |
| `migration-report-<pack>.md` | That pack's own report, from its latest run |
| `migration-acceptance-<pack>.json` | That pack's acceptance record |
| `migration-report.md`, `migration-acceptance.json` | Whichever pack ran last |
| `manual-review-queue.json`, `migration-review.html` | Every file still waiting on you, from every pack. Running a pack again replaces only that pack's entries. |

---

## For engineers extending FORGE

The chat is the product surface. These are for working on FORGE itself:

- **[COMPONENTS.md](COMPONENTS.md)** — what each module does, and where the Java assumptions live
- **[EXTENDING.md](EXTENDING.md)** — adding a technology transition: one markdown file, no Python
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the pipeline, node by node
- **[GUARDRAILS.md](GUARDRAILS.md)** — the six checks every file passes, and what each costs
- **[INTENT.md](INTENT.md)** — how plain English becomes a pack selection, and the limits on it

There is also a CLI (`migrate.py --discover`, `--phase`, `--acceptance`) used by CI and the test
suite. It does the same things through the same service layer, minus the conversation — see
[ARCHITECTURE.md §8](ARCHITECTURE.md). It cannot land on a branch; that is chat only.
