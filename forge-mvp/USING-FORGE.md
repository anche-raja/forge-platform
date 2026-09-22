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

> **FORGE** — Found it: Maven, 10 modules, Java 8. Struts 2.6.8, Spring 5.3.39, Spring Security
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

> **FORGE** — Landed 372 files on `forge/jakarta-migration`, commit `a1b2c3d`. Nothing was pushed —
> run `git push -u origin forge/jakarta-migration` when you're ready.

---

## What it costs

FORGE tells you before it spends anything over your limit, and anything over **$1** stops for a
click. Measured on a real AMS file with the current models:

| | |
|---|---|
| One file | **~$0.024** — three model calls |
| All ten packs across AMS (1,480 units) | **~$45** |
| With a realistic retry rate | **$48 – $57** |

A file that scores below the pass threshold retries once with the reviewer's feedback, which adds
two more calls for that file. The range above covers a 10–40% retry rate.

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

Say *"put it on a branch"*. FORGE creates the branch, copies the migrated files in, and makes one
commit. It **never pushes** — it hands you the push command.

It refuses rather than forcing its way past a problem:

| | |
|---|---|
| Not a git repository | Refused — it suggests `git init`, it does not run it |
| Uncommitted changes | Refused — it never stashes |
| Branch already exists | Refused — it never overwrites |

**One sharp edge:** if the output directory sits inside your repository, landing refuses every time,
because the work tree is never clean. Add `migrated/` to your `.gitignore`.

FORGE's own working files — reports, the review queue — are never committed. The review queue holds
copies of your source, so it must not end up in a commit.

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
./forge-terraform/scripts/generate-agents-yaml.sh dev > forge-mvp/agents.yaml
```

That reads your Terraform outputs. Re-run it after any `terraform apply`.

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
| **Landing refuses: work tree not clean** | Commit or stash your own changes; gitignore `migrated/` |

Everything FORGE produces lands in the output directory (`./migrated` by default): the migrated
tree, a report of what happened and what it cost, and the review queue. Ask for *"the artifacts"* in
chat and it lists them with download links.

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
