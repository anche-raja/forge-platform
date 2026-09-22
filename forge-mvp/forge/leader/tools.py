"""The closed catalogue: the only twelve things the leader can do.

Everything here is a wrapper over :mod:`forge.service`. No behaviour lives in
this file — "add behaviour to the service, never to a route or a CLI branch"
applies to a tool too. What the file does add is the four gates that make a
model-driven conductor safe to point at a real repository:

**R1** the catalogue is closed. An unknown name is an observation, not a crash.

**R2** a pack must be one discovery *selected*. ``activations`` is the wrong
bound — it still lists packs an intent plan deliberately excluded — so the
bound is ``convo.selected_packs``, plus ``runnable_phases()``. With no
discovery yet the toolbox profiles the project itself, free, and re-checks.

**R4/R5** money and approval. A call whose estimate exceeds
``leader.confirm_above_usd`` parks a pending confirmation instead of running;
applying review decisions parks whatever it costs, because an approval is a
human's signature on someone else's code and no estimate can stand in for it.

**R6** nothing here raises. Every failure is an ``ok: false`` observation, so a
broken tool costs a turn a sentence rather than the whole job. The one case
worth spelling out is a run that succeeded and whose *cards* then failed to
render: the run result is still returned. Paid work is never discarded by a
rendering bug.

Two of the twelve are new in increment 2 and invert an assumption the first one
made. A chat no longer arrives with a project attached: the owner's objection to
the wizard was *"I requested to change with prompt instead of this project
setup"*, so ``set_project`` exists and every other tool refuses with
:data:`NO_PROJECT_ERROR` until it has run. That sentence is the mechanism — a
tool that answered "nothing found" would have the leader narrate an empty
repository instead of asking which folder the user means. And ``land_on_branch``
is the only tool that writes outside ``output_dir``; like
``apply_review_decisions`` it is gated whatever it costs, because the click is
the signature and no estimate can stand in for one.

The observations are built by :mod:`forge.leader.cards`, which is the only
place a queue entry, a guardrail finding or an acceptance result becomes
something a model may read.
"""

import json
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forge.config import ForgeConfig
from forge.leader import cards
from forge.leader.settings import LeaderSettings

# One screenful of review cards. Past this the browser is the wrong place to
# read them, and the transcript is the wrong place to keep them.
CARD_CAP = 25
DRY_RUN_NOTE = "a dry run costs the same — it still calls the models"
# The same default the route and the CLI use. A leader that had to invent one
# would write a migration somewhere the user never looked.
DEFAULT_OUTPUT_DIR = "./migrated"
# Read this as an instruction, because that is what it is for. Every tool that
# needs a repository returns exactly this string until `set_project` has run:
# it names the one thing the leader can do about it, which is ask.
NO_PROJECT_ERROR = "no project set yet — ask the user which folder the repository is in"
# A landing refusal is FORGE's own sentence and the useful half of it is the fix
# — "run `git init`", "commit or stash them first". The 200-char cap an
# untrusted `error` gets would cut exactly that off as soon as a temp path is
# long. Every part of this string is already bounded: a fixed template, at most
# five paths, and git's own stderr trimmed to 200 before it gets here.
LANDING_ERROR_CAP = 600


# ─── the catalogue ────────────────────────────────────────────────────────────

# Raw OpenAI-function dicts. `bind_tools` converts these verbatim, and a
# zero-argument tool still needs `{"type": "object", "properties": {}}`: a bare
# `{}` reaches Bedrock as an inputSchema with no type and the Converse call is
# rejected — on every turn, since the whole catalogue is bound to every call.
_EMPTY_SCHEMA = {"type": "object", "properties": {}}

TOOL_DEFS: List[dict] = [
    {
        "name": "set_project",
        "description": (
            "Point this chat at the repository to migrate, and profile it in the same call. Free: "
            "no model call, no AWS. Ask the user which folder it is in and pass what they say — "
            "never guess a path, and never invent one from the conversation. A chat works on one "
            "repository; a second, different folder is refused."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_dir": {
                    "type": "string",
                    "description": "The folder the repository is in, as the user gave it. "
                                   "An absolute path, or one starting with ~.",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Where migrated files are written. Defaults to ./migrated — "
                                   "only pass one if the user names it.",
                },
            },
            "required": ["source_dir"],
        },
    },
    {
        "name": "profile_project",
        "description": (
            "Profile the repository and list the packs its evidence activates, in dependency "
            "order, with the decisions a run would use. Free: no model call, no AWS. Call this "
            "first when the state block says the project has not been profiled."
        ),
        "parameters": dict(_EMPTY_SCHEMA),
    },
    {
        "name": "resolve_intent",
        "description": (
            "Profile the repository AND narrow the plan to what the user asked for, in their own "
            "words. Costs one small model call. It can only narrow the packs evidence already "
            "activated — it can never add one — and it cannot change the review gate. Use it when "
            "the user's request names a target, a framework or a part of the tree to leave alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The user's request, in their words. Do not invent one.",
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "estimate_pack",
        "description": (
            "Count the files one pack would touch and what it would cost, without running "
            "anything. Free. Use it before proposing a run."
        ),
        "parameters": {
            "type": "object",
            "properties": {"pack": {"type": "string", "description": "A pack id from the plan."}},
            "required": ["pack"],
        },
    },
    {
        "name": "run_pack",
        "description": (
            "Run one migration pack over the project. This SPENDS MONEY and writes files. "
            "A dry run costs exactly the same as a real one — it still calls the models — so it "
            "is a preview, never a cheaper option. One pack at a time, in dependency order."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pack": {"type": "string", "description": "A pack id from the plan."},
                "dry_run": {
                    "type": "boolean",
                    "description": "Transform and review but write nothing. Costs the same as a real run.",
                },
                "acceptance": {
                    "type": "boolean",
                    "description": "Run the pack's acceptance checks over the merged tree afterwards.",
                },
            },
            "required": ["pack"],
        },
    },
    {
        "name": "check_acceptance",
        "description": (
            "Run one pack's acceptance checks over the migrated tree. Mechanical, no model call. "
            "Needs a pack to have run first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pack": {"type": "string", "description": "A pack id from the plan."},
                "build": {
                    "type": "boolean",
                    "description": "Also compile the tree. Slow — it shells out to the build tool.",
                },
            },
            "required": ["pack"],
        },
    },
    {
        "name": "list_held_files",
        "description": (
            "The files the last run could not settle on its own: held, sent to manual review, or "
            "blocked. Free. Each one becomes a review card the user can decide on."
        ),
        "parameters": dict(_EMPTY_SCHEMA),
    },
    {
        "name": "apply_review_decisions",
        "description": (
            "Propose approve / reject / retry for held files. This ALWAYS needs the user to press "
            "Confirm, whatever it costs: approving is their signature on code you have not seen. "
            "A retry re-runs that one file with your note in the prompt, and costs money."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run": {
                    "type": "string",
                    "description": "The review queue's run id, exactly as list_held_files reported it.",
                },
                "decisions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "The file's path exactly as it was listed."},
                            "pack": {"type": "string", "description": "The pack that holds it."},
                            "decision": {"type": "string", "enum": ["approve", "reject", "retry"]},
                            "note": {"type": "string", "description": "For a retry: what to change. The user signs this."},
                            "rule": {"type": "string", "description": "Optional pack rule the note refers to."},
                        },
                        "required": ["file", "pack", "decision"],
                    },
                },
            },
            "required": ["run", "decisions"],
        },
    },
    {
        "name": "generate_tests",
        "description": (
            "Write JUnit 5 tests for the migrated classes in the output tree. SPENDS MONEY: two "
            "model calls per class. Only useful after a pack has actually written files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_tests": {"type": "boolean", "description": "Also execute the generated tests."},
            },
        },
    },
    {
        "name": "pack_feedback",
        "description": (
            "Group the reviewers' notes by pack and rule, so recurring corrections can be folded "
            "back into the packs. Free."
        ),
        "parameters": dict(_EMPTY_SCHEMA),
    },
    {
        "name": "list_artifacts",
        "description": (
            "The files a run left in the output directory — report, review page, profile, "
            "acceptance record — with their sizes and a download link each. Free. Names and sizes "
            "only: you do not see what is in them."
        ),
        "parameters": dict(_EMPTY_SCHEMA),
    },
    {
        "name": "land_on_branch",
        "description": (
            "Copy the migrated files into the user's own repository on a NEW git branch and commit "
            "them. This is the only thing FORGE does that writes outside the output directory, so "
            "it ALWAYS needs the user to press Confirm. It refuses rather than repairs: the work "
            "tree must be clean and the branch must not exist, and it never stashes, never forces "
            "and never pushes. Propose it once a pack has run and the held files are settled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "A branch name that does not exist yet, e.g. forge/jakarta-migration.",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message. Leave it out for a generated one naming the packs.",
                },
            },
            "required": ["branch"],
        },
    },
]

TOOL_NAMES = tuple(d["name"] for d in TOOL_DEFS)
_BY_NAME = {d["name"]: d for d in TOOL_DEFS}
# The two that can spend real money on their own. apply_review_decisions spends
# too, on a retry, but it is gated by R5 whatever the number says.
_SPENDING_TOOLS = ("run_pack", "generate_tests")

_JSON_TYPES = {
    "string": str, "boolean": bool, "integer": int, "number": (int, float),
    "array": list, "object": dict,
}


def _type_error(key: str, value: Any, expected: str) -> Optional[str]:
    wanted = _JSON_TYPES.get(expected)
    if wanted is None:
        return None
    # bool is an int in Python; a boolean where a number is wanted is still wrong.
    if expected in ("integer", "number") and isinstance(value, bool):
        return f"'{key}' must be a {expected}"
    return None if isinstance(value, wanted) else f"'{key}' must be a {expected}"


def _check_object(schema: dict, value: Any, where: str) -> Optional[str]:
    if not isinstance(value, dict):
        return f"{where} must be an object"
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if value.get(key) in (None, ""):
            return f"{where} is missing '{key}'"
    for key, raw in value.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        problem = _type_error(key, raw, str(spec.get("type") or ""))
        if problem:
            return f"{where}: {problem}"
        enum = spec.get("enum")
        if enum and raw not in enum:
            return f"{where}: '{key}' must be one of {', '.join(str(e) for e in enum)}"
        if spec.get("type") == "array" and isinstance(spec.get("items"), dict):
            items = spec["items"]
            if items.get("type") == "object":
                for i, row in enumerate(raw):
                    problem = _check_object(items, row, f"{key}[{i}]")
                    if problem:
                        return problem
    return None


def validate_call(name: str, args: Any) -> Optional[str]:
    """Why this call cannot run, or None.

    A streamed tool call is re-parsed from partial JSON on every accumulation,
    so a truncated one arrives looking perfectly valid with arguments quietly
    missing. Checking the declared schema here is what turns that into an
    answerable error instead of a run nobody asked for.
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        return f"unknown tool '{name}'. The tools are: {', '.join(TOOL_NAMES)}"
    if not isinstance(args, dict):
        return f"{name}: arguments must be an object"
    return _check_object(spec.get("parameters") or {}, args, name)


# ─── what a tool runs against, and what it gives back ─────────────────────────

@dataclass
class ProjectContext:
    """The project a turn acts on, resolved by the route.

    Two configs, not one. ``config`` carries the browser's per-request decision
    overlay and is what ``profile_project`` reports against. ``base_config`` is
    ``agents.yaml`` with nothing overlaid, and is what ``resolve_intent`` is
    given: feeding the previous plan's answers back in as "config" makes the
    intent layer attribute them to agents.yaml and drop them from its
    assumptions — the misattribution the wizard already avoids by deleting
    ``decisions`` from its /api/intent body (app.js:120-123).

    ``bound`` is False while nobody has said where the repository is, and both
    directories are then ``""``. It is derived from ``source_dir`` rather than
    required, so every existing construction keeps meaning what it meant: a
    context built with a directory is bound, and only the chat route's
    "no project yet" case is not. ``set_project`` flips it mid-turn, which is
    why the toolbox holds this object rather than copies of its two strings.
    """

    source_dir: str
    output_dir: str
    config: ForgeConfig
    base_config: Optional[ForgeConfig] = None
    bound: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.base_config is None:
            self.base_config = self.config
        if self.bound is None:
            self.bound = bool(self.source_dir)


@dataclass
class ToolOutcome:
    ok: bool
    observation: dict                       # JSON-safe, R3-reduced
    cards: List[dict] = field(default_factory=list)
    summary: str = ""
    needs_confirmation: bool = False
    pending_id: Optional[str] = None


class Toolbox:
    """One turn's access to the service layer."""

    def __init__(self, ctx: ProjectContext, convo, settings: LeaderSettings,
                 emit: Callable[[dict], None], cancel: Optional[threading.Event] = None):
        self.ctx = ctx
        self.convo = convo
        self.settings = settings
        self._emit = emit if callable(emit) else (lambda event: None)
        self.cancel = cancel

    # ── config ───────────────────────────────────────────────────────────────

    def effective_config(self) -> ForgeConfig:
        """The config every *acting* tool runs under.

        Scope and decisions from an intent plan are written to files and read by
        nothing — ``run_migration`` and ``scan_java_files`` take both from the
        config only. Without this, "migrate to Tomcat, skip com.legacy" would
        plan one thing and run another in the same turn.

        ``risk_ceiling`` is the exception and it is R9: it always comes from the
        config, never from the plan. The intent sentence here is written by a
        model, and a model that could set ``risk_ceiling: auto`` could switch
        the hold gate off and write every HIGH-risk file straight to the output
        tree with no human click. What the leader may narrow is which packs run.
        What it may never do is decide what needs a human.
        """
        base = self.ctx.config
        ceiling = (base.get("decisions") or {}).get("risk_ceiling", "review-high")
        discovery = self.convo.discovery
        if not isinstance(discovery, dict):
            return base

        decisions = dict(discovery.get("decisions") or {})
        decisions["risk_ceiling"] = ceiling
        overrides: Dict[str, Any] = {"decisions": decisions}

        plan = discovery.get("intent")
        if isinstance(plan, dict):
            scope = plan.get("scope") if isinstance(plan.get("scope"), dict) else {}
            prefix = str(scope.get("package_prefix") or "") or str(base.get("scope_package_prefix", "") or "")
            globs = [str(g) for g in (base.get("scope_exclude_globs") or [])]
            for glob in scope.get("exclude_globs") or []:
                if str(glob) not in globs:
                    globs.append(str(glob))
            overrides["scope_package_prefix"] = prefix
            overrides["scope_exclude_globs"] = globs
        return base.with_overrides(overrides)

    # ── estimates (free, and never raising: the gate calls this) ─────────────

    def estimate(self, name: str, args: Any) -> float:
        args = args if isinstance(args, dict) else {}
        try:
            if name == "run_pack":
                units = self._pack_units(str(args.get("pack") or ""))
                return round((units[0] if units else 0) * self.settings.unit_cost_usd, 4)
            if name == "generate_tests":
                return round(self._test_targets() * self.settings.unit_cost_usd, 4)
            if name == "apply_review_decisions":
                rows = args.get("decisions")
                retries = sum(1 for d in rows if isinstance(d, dict) and d.get("decision") == "retry") \
                    if isinstance(rows, list) else 0
                return round(retries * self.settings.unit_cost_usd, 4)
        except Exception:  # noqa: BLE001 — an estimate that fails is $0, never a failed turn
            return 0.0
        return 0.0

    def _pack_units(self, pack: str):
        """(units, generated) for a pack, or None when the scan refuses it."""
        from forge.utils.file_scanner import scan_java_files

        if not pack:
            return None
        config = self.effective_config()
        try:
            scan = scan_java_files(self.ctx.source_dir, pack,
                                   str(config.get("scope_package_prefix", "") or ""),
                                   config.get("scope_exclude_globs") or [])
        except ValueError:
            return None
        return len(scan.files) + len(scan.generated), len(scan.generated)

    def _test_targets(self) -> int:
        from forge.testgen import TestGenSettings, scan_test_targets

        settings = TestGenSettings.from_config(self.effective_config())
        scan = scan_test_targets(self.ctx.output_dir, self.ctx.source_dir, only=None,
                                 overwrite=settings.overwrite, kinds=settings.kinds)
        return len(scan.targets)

    # ── execution ────────────────────────────────────────────────────────────

    def execute(self, name: str, args: Any, *, tool_id: str, confirmed: bool = False) -> ToolOutcome:
        # Never enter service.* once Stop has been pressed. run_migration with
        # cancel already set is not a no-op: it marks every file PENDING, breaks
        # at unit one, and then overwrites manual-review-queue.json with an
        # empty queue — the previous pack's held files stop being appliable.
        if self.cancel is not None and self.cancel.is_set():
            return ToolOutcome(False, {"status": "cancelled", "error": "cancelled before this tool ran"},
                               [], "cancelled")

        handler = _HANDLERS.get(name)
        if handler is None:
            return self._fail(f"unknown tool '{name}'. The tools are: {', '.join(TOOL_NAMES)}")

        args = dict(args) if isinstance(args, dict) else {}
        problem = validate_call(name, args)
        if problem:
            return self._fail(problem)

        # Before the spend gate, deliberately. A gated tool with no project
        # would park a confirmation card for a run on a repository nobody has
        # named, and the click would then execute it against "".
        if name in NEEDS_PROJECT and not self.ctx.bound:
            return self._fail(NO_PROJECT_ERROR)

        if not confirmed:
            gated = self._gate(name, args)
            if gated is not None:
                return gated

        try:
            return handler(self, args, tool_id)
        except Exception as e:  # noqa: BLE001 — R6: a tool failure is an observation, not a failed turn
            return self._fail(f"{type(e).__name__}: {e}")

    # ── gates ────────────────────────────────────────────────────────────────

    def _gate(self, name: str, args: dict) -> Optional[ToolOutcome]:
        if name == "apply_review_decisions":
            rows = args.get("decisions") if isinstance(args.get("decisions"), list) else []
            est = self.estimate(name, args)
            return self._park(name, args, est, f"Apply {len(rows)} review decision(s)", decisions=rows)

        if name == "land_on_branch":
            # R5's reasoning, one repository further out: this one writes into
            # the user's own git history. It costs $0.00 and is confirmed
            # anyway, because the thing being authorised is not spend.
            from forge.leader import landing

            branch = str(args.get("branch") or "").strip()
            try:
                count = len(landing.landable_files(self.ctx.output_dir))
            except Exception:  # noqa: BLE001 — a title is not worth a failed turn
                count = 0
            title = (f"Commit {count} migrated file(s) onto a new branch '{branch}' "
                     f"in {self.ctx.source_dir}")
            return self._park(name, args, 0.0, title, units=count)

        if name not in _SPENDING_TOOLS:
            return None
        ceiling = float(self.settings.confirm_above_usd or 0.0)
        if ceiling <= 0:              # R4: the owner's dial, set to "never ask"
            return None
        est = self.estimate(name, args)
        if est <= ceiling:
            return None

        if name == "run_pack":
            units = self._pack_units(str(args.get("pack") or ""))
            count = units[0] if units else 0
            label = "Dry-run" if args.get("dry_run") else "Run"
            title = f"{label} {args.get('pack')} over {count} file(s) — about ${est:.2f}"
            return self._park(name, args, est, title, units=count)
        count = self._test_targets()
        return self._park(name, args, est, f"Generate tests for {count} class(es) — about ${est:.2f}",
                          units=count)

    def _park(self, name: str, args: dict, est: float, title: str, *,
              units: Optional[int] = None, decisions: Optional[list] = None) -> ToolOutcome:
        pending_id = self.convo.add_pending(name, args, est, title)
        card = cards.confirm_card(pending_id, name, title, args, est, units=units, decisions=decisions)
        observation = {
            "status": "needs_confirmation", "pending_id": pending_id, "tool": name,
            "est_usd": round(float(est), 4), "title": title,
            "note": "nothing has run. Only the user pressing Confirm on the card can run it.",
        }
        return ToolOutcome(True, observation, [card], title, needs_confirmation=True, pending_id=pending_id)

    # ── plumbing ─────────────────────────────────────────────────────────────

    def _fail(self, message: str) -> ToolOutcome:
        text = cards.cap(message) or ""
        return ToolOutcome(False, {"error": text}, [], text)

    def _relay(self, tool_id: str) -> Callable[[dict], None]:
        """Service events, stamped with the tool that caused them.

        ``done`` and ``error`` are the job registry's terminal events; relaying
        a tool's own would close the browser's EventSource halfway through the
        turn.
        """
        def on_event(event: dict) -> None:
            if not isinstance(event, dict) or event.get("type") in ("done", "error"):
                return
            self._emit({**event, "via": "tool", "tool_id": tool_id})
        return on_event

    def _store_discovery(self, result: dict) -> None:
        """Keep the plan whole, and record what it SELECTED.

        ``activations`` is every pack the evidence fires on, including the ones
        an intent plan just excluded — "don't touch the JSPs" leaves
        jsp-jstl-modernize activated and runnable. The plan's own pack list is
        the honest bound, so R2 gates on that when there is one.
        """
        plan = result.get("intent") if isinstance(result, dict) else None
        if isinstance(plan, dict) and isinstance(plan.get("packs"), list):
            selected = [str(p) for p in plan["packs"]]
        else:
            selected = [str(a.get("pack")) for a in (result.get("activations") or [])
                        if isinstance(a, dict) and a.get("pack")]
        with self.convo.lock:
            self.convo.discovery = result
            self.convo.selected_packs = selected

    def _check_pack(self, pack: str) -> Optional[str]:
        """R2. Returns why this pack may not be touched, or None."""
        from forge.utils.file_scanner import runnable_phases

        if not pack:
            return "name a pack from the plan"
        if not isinstance(self.convo.discovery, dict):
            # No evidence yet, and profiling is free — gather it rather than
            # refusing, then apply the same bound.
            problem = self._auto_profile()
            if problem:
                return problem
        selected = list(self.convo.selected_packs or [])
        if pack not in selected:
            if not selected:
                return f"'{pack}' cannot run: discovery selected no packs for this project"
            return (f"'{pack}' is not one of the packs discovery selected for this project. "
                    f"Those are: {', '.join(selected)}")
        if pack not in runnable_phases():
            return (f"'{pack}' is not runnable today — it is detect-only, or it needs a context "
                    "extractor that is not registered")
        return None

    def _auto_profile(self) -> Optional[str]:
        from forge import service

        try:
            self._store_discovery(service.discover(self.ctx.source_dir, self.ctx.output_dir, self.ctx.config))
        except Exception as e:  # noqa: BLE001 — a broken pack library is an answer, not a crash
            return f"could not profile the project: {type(e).__name__}: {e}"
        return None

    def _review_cards(self):
        """(cards, card_error). Never raises — see R6 and the run_pack handler."""
        from forge.review_queue import REVIEW_STATUSES, load_queue

        try:
            queue = load_queue(self.ctx.output_dir)
        except FileNotFoundError:
            return [], None
        except Exception as e:  # noqa: BLE001
            return [], type(e).__name__
        try:
            run = str(queue.get("run") or "")
            entries = [e for e in (queue.get("entries") or [])
                       if isinstance(e, dict) and e.get("status") in REVIEW_STATUSES]
            built = [cards.review_file_card(e, run=run) for e in entries[:CARD_CAP]]
            if len(entries) > CARD_CAP:
                built.append(cards.review_more_card(CARD_CAP, len(entries)))
            return built, None
        except Exception as e:  # noqa: BLE001
            return [], type(e).__name__

    # ── the nine ─────────────────────────────────────────────────────────────

    def _discovery_obs(self, result: dict) -> Dict[str, Any]:
        """The plan as metadata. The card carries the whole thing."""
        profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
        plan = result.get("intent") if isinstance(result.get("intent"), dict) else None
        config = self.ctx.config

        if plan is not None:
            states = plan.get("states") if isinstance(plan.get("states"), dict) else {}
            packs = [{"id": str(p), "state": str(states.get(p, "unknown"))} for p in (plan.get("packs") or [])]
            excluded = [{"pack": x.get("pack"), "reason": cards.cap(x.get("reason"))}
                        for x in (plan.get("excluded") or []) if isinstance(x, dict)]
            unsupported = [{"asked": u.get("asked"), "reason": cards.cap(u.get("reason"))}
                           for u in (plan.get("unsupported") or []) if isinstance(u, dict)]
            provenance = dict(plan.get("provenance") or {})
            scope = dict(plan.get("scope") or {})
            assumptions = [str(a) for a in (plan.get("assumptions") or [])]
            questions = [str(q) for q in (plan.get("questions") or [])]
        else:
            packs = [{"id": str(a.get("pack")),
                      "state": "runnable" if a.get("runnable") else ("blocked" if a.get("complete") else "detect-only")}
                     for a in (result.get("activations") or []) if isinstance(a, dict)]
            excluded, unsupported, assumptions, questions = [], [], [], []
            provenance = {}
            scope = {"package_prefix": str(config.get("scope_package_prefix", "") or ""),
                     "exclude_globs": [str(g) for g in (config.get("scope_exclude_globs") or [])]}

        return {
            "packs": packs,
            "order": [str(p) for p in (result.get("order") or [])],
            "excluded": excluded,
            "unsupported": unsupported,
            "decisions": dict(result.get("decisions") or {}),
            "provenance": provenance,
            "scope": scope,
            "assumptions": assumptions,
            "questions": questions,
            "java_level": profile.get("java_level"),
            "build_system": profile.get("build_system"),
            "counts": dict(profile.get("counts") or {}),
        }

    def _plan_cards(self, result: dict, *, intent: bool, request: Optional[str] = None) -> List[dict]:
        """The plan card, and beside it the table the deleted Discover step drew.

        Two readings of one ``service.discover()`` result. The evidence card is
        built here rather than behind a tool of its own because a second tool
        would be a second walk of the repository to redraw something the
        conversation is already holding.
        """
        built = [cards.plan_card(result, intent=intent, request=request)]
        try:
            built.append(cards.evidence_card(result))
        except Exception as e:  # noqa: BLE001 — R6: a table that will not render is not a failed profile
            built.append({"kind": "evidence", "packs": [], "order": [], "decisions": [],
                          "error": f"the evidence table could not be built: {type(e).__name__}"})
        return built

    def _profile_project(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        result = service.discover(self.ctx.source_dir, self.ctx.output_dir, self.ctx.config)
        self._store_discovery(result)
        observation = self._discovery_obs(result)
        selected = list(self.convo.selected_packs)
        return ToolOutcome(True, observation, self._plan_cards(result, intent=False),
                           f"profiled: {len(selected)} pack(s) activated")

    def _set_project(self, args: dict, tool_id: str) -> ToolOutcome:
        """Bind the chat to a repository, and profile it in the same call.

        The owner deleted the Project form: *"I requested to change with prompt
        instead of this project setup"*. So the folder arrives as a sentence,
        which means it arrives with a typo in it sooner or later — every
        refusal here echoes the path back, because the user cannot see the typo
        in a message that says only "not found".

        Profiling immediately is not a convenience. Discovery is free by
        contract (no model, no AWS), and a ``set_project`` that only bound
        would spend a model call on a turn that said nothing the user did not
        already know.
        """
        from forge import service

        raw = str(args.get("source_dir") or "").strip()
        path = Path(raw).expanduser() if raw else None
        if path is None or not path.is_dir():
            return self._fail(f"there is no directory at {raw or '(nothing)'} — ask the user for the "
                              "folder the repository is in, as an absolute path or one starting with ~")
        source = str(path.resolve())

        raw_out = str(args.get("output_dir") or "").strip() or DEFAULT_OUTPUT_DIR
        # Expanded but not resolved, exactly as the route normalises the
        # browser's value: the two have to agree, or the same project posted
        # from the page would look like a second one and be refused.
        output = str(Path(raw_out).expanduser())

        if not self.convo.bind(source, output):
            return self._fail(f"this chat is already working on {self.convo.source_dir} — "
                              "start a new chat for another repository")
        # The toolbox holds the context by reference, so every later tool in
        # this turn — and state_block on the next model step — sees the project.
        self.ctx.source_dir, self.ctx.output_dir, self.ctx.bound = source, output, True

        try:
            result = service.discover(source, output, self.ctx.config)
        except Exception as e:  # noqa: BLE001 — bound but unprofiled is a real state; say so
            return self._fail(f"the project is set to {source}, but it could not be profiled: "
                              f"{type(e).__name__}: {e}")
        self._store_discovery(result)

        profile = result.get("profile") if isinstance(result.get("profile"), dict) else {}
        observation = {
            "source_dir": source,
            "output_dir": output,
            "modules": len(profile.get("modules") or []),
            **self._discovery_obs(result),
        }
        selected = list(self.convo.selected_packs)
        return ToolOutcome(True, observation, self._plan_cards(result, intent=False),
                           f"project set: {source} · {len(selected)} pack(s) activated")

    def _resolve_intent(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        request = str(args.get("request") or "").strip()
        if not request:
            # service.discover ignores a blank intent and quietly returns a
            # plain profile, which would be reported as if the sentence had
            # been honoured.
            return self._fail("intent is empty — pass the user's request verbatim")

        result = service.discover(self.ctx.source_dir, self.ctx.output_dir, self.ctx.base_config,
                                  intent=request)
        plan = result.get("intent") if isinstance(result.get("intent"), dict) else None

        # R9. The sentence handed to the intent layer here was written by a
        # model, and an intent decision with provenance "prompt" beats the
        # config. risk_ceiling is the hold gate: left alone, "auto" arriving
        # from a model's own sentence would write every HIGH-risk file straight
        # to the output tree. The leader may narrow the plan; it may not decide
        # what needs a human.
        ceiling = (self.ctx.config.get("decisions") or {}).get("risk_ceiling", "review-high")
        proposed = (result.get("decisions") or {}).get("risk_ceiling")
        if isinstance(result.get("decisions"), dict):
            result["decisions"]["risk_ceiling"] = ceiling
        if plan is not None and isinstance(plan.get("decisions"), dict):
            plan["decisions"]["risk_ceiling"] = ceiling
        if proposed is not None and proposed != ceiling and plan is not None:
            plan.setdefault("assumptions", []).append(
                f"risk_ceiling stays {ceiling}: the review gate is not the leader's to change"
            )

        self._store_discovery(result)
        if plan is not None:
            self.convo.accrue_pipeline(plan.get("cost_usd") or 0.0)

        observation = self._discovery_obs(result)
        packs = ", ".join(p["id"] for p in observation["packs"]) or "none"
        return ToolOutcome(True, observation, self._plan_cards(result, intent=True, request=request),
                           f"plan: {packs}")

    def _estimate_pack(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge.utils.file_scanner import scan_java_files

        pack = str(args.get("pack") or "")
        problem = self._check_pack(pack)
        if problem:
            return self._fail(problem)

        config = self.effective_config()
        try:
            scan = scan_java_files(self.ctx.source_dir, pack,
                                   str(config.get("scope_package_prefix", "") or ""),
                                   config.get("scope_exclude_globs") or [])
        except ValueError as e:
            return self._fail(str(e))

        generated = len(scan.generated)
        units = len(scan.files) + generated
        est = round(units * self.settings.unit_cost_usd, 4)
        observation = {"pack": pack, "units": units, "generated": generated, "est_usd": est,
                       "unit_cost_usd": self.settings.unit_cost_usd, "note": DRY_RUN_NOTE}
        card = cards.estimate_card(pack, units, generated, est, self.settings.unit_cost_usd, DRY_RUN_NOTE)
        return ToolOutcome(True, observation, [card], f"{pack}: {units} file(s), about ${est:.2f}")

    def _run_pack(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        pack = str(args.get("pack") or "")
        dry_run = bool(args.get("dry_run", False))
        problem = self._check_pack(pack)
        if problem:
            return self._fail(problem)

        # Chain onto whatever an earlier pack in this conversation wrote, rather
        # than re-reading the original and replacing it. The user drives this
        # surface by saying "migrate my app" and nothing else, so the leader has
        # to sequence a multi-pack plan without being told how; refusing on the
        # second pack would end every real migration one step in.
        # Only from the manifest, never from `convo.completed`: a chat that
        # resumed against a directory an earlier session wrote must chain too.
        from forge.utils import run_manifest
        written = run_manifest.load(self.ctx.output_dir)
        chain = bool(written) and any(owner != pack for owner in written.values())

        try:
            result = service.run_migration(
                self.ctx.source_dir, pack, self.ctx.output_dir, self.effective_config(),
                dry_run=dry_run, run_acceptance=bool(args.get("acceptance", False)),
                chain=chain,
                on_event=self._relay(tool_id), cancel=self.cancel,
            )
        except service.NoEligibleFiles as e:
            return ToolOutcome(True, {"status": "nothing", "pack": pack, "message": cards.cap(e)},
                               [], f"{pack}: nothing to do")
        except service.PackOverlap as e:
            # ok:false, so the leader reports it and asks rather than retrying:
            # the fix is a combined phase or a chained output dir, and both are
            # the user's call. Capped well above TEXT_CAP because the actionable
            # half is at the end — at 200 chars the leader would see the
            # complaint and not the remedy. Every part of this string is engine
            # prose plus file paths, which are already model-visible; no file
            # bytes can reach it, so the wider cap does not touch R3.
            return ToolOutcome(False, {"error": cards.cap(e, 800), "pack": pack},
                               [], f"{pack}: refused — would overwrite another pack")

        summary = result.summary()
        totals = dict(summary.get("totals") or {})
        status = "cancelled" if summary.get("cancelled") else "done"
        acceptance = summary.get("acceptance")
        observation = {
            "status": status, "pack": pack, "dry_run": dry_run, "totals": totals,
            "bedrock_calls": totals.get("bedrock_calls", 0),
            "cost_usd": totals.get("cost_usd", 0.0),
            "queue_count": summary.get("queue_count", 0),
            "acceptance": cards.acceptance_obs(acceptance) if acceptance else None,
        }
        self.convo.accrue_pipeline(totals.get("cost_usd") or 0.0)
        if status == "done" and not dry_run:
            with self.convo.lock:
                if pack not in self.convo.completed:
                    self.convo.completed.append(pack)

        # R6, the expensive half: the run is paid for and the files are written.
        # A diff that will not render is a cosmetic failure and must not be
        # reported as a failed run — the model would propose running it again.
        built, card_error = self._review_cards()
        parts = [f"{pack}: {totals.get('passed', 0)} passed, {totals.get('manual', 0)} manual, "
                 f"{totals.get('blocked', 0)} blocked, {totals.get('held', 0)} held"]
        if dry_run:
            parts.append("dry run")
        if status == "cancelled":
            parts.append("stopped")
        parts.append(f"${float(totals.get('cost_usd') or 0.0):.4f}")
        if card_error:
            parts.append(f"card_error: {card_error}")
        return ToolOutcome(True, observation, built, " · ".join(parts))

    def _check_acceptance(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        pack = str(args.get("pack") or "")
        problem = self._check_pack(pack)
        if problem:
            return self._fail(problem)
        if not Path(self.ctx.output_dir).is_dir():
            # acceptance would create the directory and grade the unmigrated
            # source, reporting FAIL for work that was never done.
            return self._fail(f"no output directory at {self.ctx.output_dir} — run a pack first")

        outcome = service.acceptance(pack, self.ctx.source_dir, self.ctx.output_dir,
                                     self.effective_config(), run_build=bool(args.get("build", False)))
        observation = {"pack": pack, **cards.acceptance_obs(outcome)}
        verdict = observation.get("verdict") or f"skipped ({observation.get('skipped_reason')})"
        return ToolOutcome(True, observation, [cards.acceptance_card(pack, outcome)], f"{pack}: {verdict}")

    def _list_held_files(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge.review_queue import REVIEW_STATUSES, load_queue

        try:
            queue = load_queue(self.ctx.output_dir)
        except (FileNotFoundError, ValueError) as e:
            # A missing queue is the normal state before the first run, and a
            # stale one is fixable by re-running — neither is a tool failure.
            return ToolOutcome(True, {"count": 0, "message": cards.cap(e)}, [], "no review queue yet")

        run = str(queue.get("run") or "")
        entries = [e for e in (queue.get("entries") or [])
                   if isinstance(e, dict) and e.get("status") in REVIEW_STATUSES]
        rows = entries[:CARD_CAP]
        observation = {
            "count": len(entries),
            "shown": len(rows),
            "run": run,
            "by_status": dict(Counter(str(e.get("status")) for e in entries)),
            "entries": [cards.entry_obs(e) for e in rows],
        }
        built = [cards.review_file_card(e, run=run) for e in rows]
        if len(entries) > CARD_CAP:
            built.append(cards.review_more_card(CARD_CAP, len(entries)))
        return ToolOutcome(True, observation, built, f"{len(entries)} file(s) waiting on a human")

    def _apply_review_decisions(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service
        from forge.decisions import decisions_from
        from forge.review_queue import load_queue

        run = str(args.get("run") or "")
        try:
            decided = decisions_from(args.get("decisions"), "<decisions>")
        except ValueError as e:
            return self._fail(str(e))
        if not decided:
            return self._fail("no decisions to apply")

        try:
            queue = load_queue(self.ctx.output_dir)
        except (FileNotFoundError, ValueError) as e:
            return self._fail(str(e))

        # The queue is one file per output_dir, rewritten by every run. A card
        # from an earlier run still looks live in the transcript, and
        # service.apply matches on path alone — approving it would promote a
        # transform the human never saw.
        if str(queue.get("run") or "") != run:
            return self._fail("the review queue changed since those files were shown — "
                              "ask for the held files again")

        by_path: Dict[str, dict] = {}
        for entry in queue.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            for key in (entry.get("rel_path"), entry.get("file_path")):
                if key:
                    by_path.setdefault(str(key), entry)

        kept, rejected, seen = [], [], set()
        for d in decided:
            row = {"file": d.file, "pack": d.pack, "decision": d.decision}
            entry = by_path.get(d.file)
            if entry is None:
                # Deliberately no basename fallback: find_entry would approve a
                # same-named file in another directory, a diff nobody saw.
                rejected.append({**row, "reason": "not in the current review queue under that exact path"})
            elif str(entry.get("pack") or "") != d.pack:
                rejected.append({**row, "reason": f"that file is held by pack '{entry.get('pack')}'"})
            elif d.file in seen:
                rejected.append({**row, "reason": "duplicate decision for the same file"})
            else:
                seen.add(d.file)
                kept.append(d)

        if not kept:
            return ToolOutcome(False, {"error": "none of those decisions match the current review queue",
                                       "outcomes": [], "remaining": len(queue.get("entries") or []),
                                       "rejected": rejected}, [], "nothing applied")

        result = service.apply(kept, self.ctx.source_dir, self.ctx.output_dir, self.effective_config(),
                               run=run, on_event=self._relay(tool_id))
        data = result.to_json()
        observation = {
            "outcomes": [{"file": o.get("file"), "decision": o.get("decision"), "applied": o.get("applied"),
                          "status_after": o.get("status_after"), "detail": cards.cap(o.get("detail"))}
                         for o in data.get("outcomes") or []],
            "remaining": data.get("remaining", 0),
            "rejected": rejected,
        }
        applied = sum(1 for o in observation["outcomes"] if o.get("applied"))
        return ToolOutcome(True, observation, [],
                           f"{applied} of {len(decided)} applied, {observation['remaining']} left")

    def _generate_tests(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        result = service.generate_tests(self.ctx.source_dir, self.ctx.output_dir, self.effective_config(),
                                        run_tests=bool(args.get("run_tests", False)),
                                        on_event=self._relay(tool_id), cancel=self.cancel)
        data = result.to_json()
        totals = dict(data.get("totals") or {})
        observation = {
            "status": "cancelled" if data.get("cancelled") else "done",
            "dry_run": data.get("dry_run"), "style": data.get("style"),
            "totals": totals, "skipped": data.get("skipped", 0),
            "dependencies": list(data.get("dependencies") or []),
        }
        self.convo.accrue_pipeline(totals.get("cost_usd") or 0.0)
        card = cards.tests_card(totals, data.get("dependencies"),
                                cards.file_href(self.ctx.output_dir, "test-generation-report.md"))
        return ToolOutcome(True, observation, [card],
                           f"{totals.get('generated', 0)} test(s) generated, {totals.get('held', 0)} held")

    def _pack_feedback(self, args: dict, tool_id: str) -> ToolOutcome:
        from forge import service

        result = service.feedback(self.ctx.output_dir)
        observation = {"notes": result.get("notes", 0), "packs": list(result.get("packs") or []),
                       "path": result.get("path")}
        card = cards.feedback_card(observation["notes"], observation["packs"], observation["path"])
        return ToolOutcome(True, observation, [card],
                           f"{observation['notes']} note(s) across {len(observation['packs'])} pack(s)")

    def _list_artifacts(self, args: dict, tool_id: str) -> ToolOutcome:
        """What a run left behind: names, sizes, and a link each.

        The same rows ``/api/artifacts`` serves, from the same table — the UI's
        ``ARTIFACTS`` is the list of files FORGE writes, and a second copy here
        would be the one that goes stale. Imported inside the handler because
        the leader must not drag the whole HTTP layer in to answer a question
        about a directory.

        The observation carries no contents and never could: several of these
        files embed the user's source verbatim, which is the same R3 boundary
        the review cards hold. A download link is not a disclosure — the user
        clicking it is not the model reading it.
        """
        from forge.ui.app import ARTIFACTS

        out = Path(self.ctx.output_dir).expanduser()
        rows: List[Dict[str, Any]] = []
        for name, label in ARTIFACTS:
            path = out / name
            if not path.is_file():
                continue
            stat = path.stat()
            rows.append({"name": name, "label": label, "size": stat.st_size,
                         "modified": stat.st_mtime, "url": cards.file_href(str(out), name)})

        observation = {
            "output_dir": str(out),
            "exists": out.is_dir(),
            "count": len(rows),
            "artifacts": [{"name": r["name"], "label": r["label"], "size": r["size"]} for r in rows],
        }
        if not rows:
            observation["message"] = ("nothing has been written into this output directory yet — "
                                      "run a pack first")
        return ToolOutcome(True, observation, [cards.artifacts_card(str(out), rows)],
                           f"{len(rows)} artifact(s) in {out}")

    def _superseded_paths(self) -> List[str]:
        """Paths the migration retired, from the queue on disk. Never raises.

        The review queue is the only durable record of a ``deleted_files`` entry
        once the run's process is gone — ``RunResult`` is not persisted and the
        conversation does not carry it — so a file superseded by a unit that
        went through cleanly in an earlier session is not in here. Landing
        copies files in either way; the worst case is an XML config left beside
        the Java config that replaced it, which the migration report already
        lists for the user under "XML configs replaced by Java configuration".
        """
        from forge.review_queue import load_queue

        try:
            queue = load_queue(self.ctx.output_dir)
        except Exception:  # noqa: BLE001 — no queue is the normal state before a run
            return []
        found: List[str] = []
        for entry in queue.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            for path in entry.get("deleted_files") or []:
                if str(path) not in found:
                    found.append(str(path))
        return found

    def _land_on_branch(self, args: dict, tool_id: str) -> ToolOutcome:
        """The one write into the user's own repository. Confirmed, and refusing.

        Everything the preconditions catch is a state the user has to resolve
        themselves — a dirty tree, an existing branch, a folder that is not a
        repository at all. This returns their own git's words and stops. It
        does not stash, force, amend or push, and on a failure part-way through
        it says which branch the repository is on rather than guessing at an
        undo.
        """
        from forge.leader import landing

        branch = str(args.get("branch") or "").strip()
        message = args.get("message")
        message = str(message).strip() if isinstance(message, str) and message.strip() else None
        # The packs this chat actually finished, in the order it ran them. A
        # commit message is a record, so it names what was done, not what was
        # planned.
        packs = [str(p) for p in (self.convo.completed or [])]

        result = landing.land(self.ctx.source_dir, self.ctx.output_dir, branch,
                              message=message, packs=packs,
                              deleted=self._superseded_paths())
        if not result.get("ok"):
            observation = {"error": cards.cap(result.get("error"), LANDING_ERROR_CAP)
                           or "the landing was refused"}
            state = cards.cap(result.get("state"), LANDING_ERROR_CAP)
            if state:
                observation["state"] = state
            return ToolOutcome(False, observation, [], observation["error"])

        observation = {
            "branch": result["branch"],
            "files_changed": result["files_changed"],
            "deleted": result["deleted"],
            "commit": result["commit"],
            "packs": result["packs"],
            "push_command": result["push_command"],
        }
        return ToolOutcome(True, observation, [cards.land_card(result)],
                           f"{result['files_changed']} file(s) committed on {result['branch']} "
                           f"({result['commit']}) — not pushed")


_HANDLERS: Dict[str, Callable[[Toolbox, dict, str], ToolOutcome]] = {
    "set_project": Toolbox._set_project,
    "profile_project": Toolbox._profile_project,
    "resolve_intent": Toolbox._resolve_intent,
    "estimate_pack": Toolbox._estimate_pack,
    "run_pack": Toolbox._run_pack,
    "check_acceptance": Toolbox._check_acceptance,
    "list_held_files": Toolbox._list_held_files,
    "apply_review_decisions": Toolbox._apply_review_decisions,
    "generate_tests": Toolbox._generate_tests,
    "pack_feedback": Toolbox._pack_feedback,
    "list_artifacts": Toolbox._list_artifacts,
    "land_on_branch": Toolbox._land_on_branch,
}

assert set(_HANDLERS) == set(TOOL_NAMES), "every catalogue entry needs a handler"

# Every tool but one acts on a repository. `set_project` is how the chat gets
# one, so it is the only name absent here — a new tool is inside this set unless
# it is a second way to name a project.
NEEDS_PROJECT = frozenset(TOOL_NAMES) - {"set_project"}


def tool_title(name: str, args: Any) -> str:
    """A one-line label for the tool row the browser draws."""
    args = args if isinstance(args, dict) else {}
    if name == "run_pack":
        return f"{'Dry-run' if args.get('dry_run') else 'Run'} {args.get('pack') or '?'}"
    if name == "estimate_pack":
        return f"Estimate {args.get('pack') or '?'}"
    if name == "check_acceptance":
        return f"Acceptance checks for {args.get('pack') or '?'}"
    if name == "resolve_intent":
        return "Work out the plan"
    if name == "profile_project":
        return "Profile the project"
    if name == "list_held_files":
        return "List the files waiting on a human"
    if name == "apply_review_decisions":
        rows = args.get("decisions")
        return f"Apply {len(rows) if isinstance(rows, list) else 0} review decision(s)"
    if name == "generate_tests":
        return "Generate tests"
    if name == "pack_feedback":
        return "Group the review notes"
    if name == "set_project":
        return f"Set the project to {args.get('source_dir') or '?'}"
    if name == "list_artifacts":
        return "List what the run wrote"
    if name == "land_on_branch":
        return f"Land the migration on branch {args.get('branch') or '?'}"
    return name


def to_json(observation: Any) -> str:
    """Serialise an observation for a ToolMessage.

    A dict handed to ToolMessage is coerced with ``str()``, which produces a
    Python repr rather than JSON (``tool.py:103-105``). Every call site goes
    through here.
    """
    return json.dumps(observation, default=str)
