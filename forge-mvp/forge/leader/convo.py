"""Conversation state: what the model remembers, and what the browser shows.

Two histories, deliberately kept apart.

``history`` is what goes back to the model — LangChain messages, and nothing
else. ``transcript`` is what the browser renders on a reload: user turns, the
leader's replies, tool rows with their progress, and cards. A card may carry a
diff; an observation in ``history`` never does. That split is the one in
GUARDRAILS.md §7 applied a layer up, and it is why these are separate lists
rather than one list rendered two ways.

Storage is in memory for the process lifetime, mirroring
:class:`forge.ui.jobs.JobRegistry` — this is a loopback tool for one engineer on
their own machine, and a conversation that outlived the server would be a
promise the rest of the UI does not make.
"""

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.messages import BaseMessage, HumanMessage


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Conversation:
    """One chat. Mutated on a job thread, read on request threads."""

    id: str = field(default_factory=_new_id)
    history: List[BaseMessage] = field(default_factory=list)
    transcript: List[dict] = field(default_factory=list)
    # The last `service.discover()` result, kept whole. Tools read it to answer
    # "is this pack one the evidence actually activated?" without re-profiling.
    discovery: Optional[dict] = None
    # The packs that discovery SELECTED — the plan's packs when an intent plan
    # exists, every activated pack otherwise. This is the R2 bound: a pack the
    # model names that is not in here never reaches `service`. It is stored
    # rather than re-derived because `activations` still lists packs an intent
    # plan deliberately excluded, so `discovery` alone is the wrong answer.
    selected_packs: List[str] = field(default_factory=list)
    completed: List[str] = field(default_factory=list)
    # Plan packs that ran and found no eligible files. Not `completed` — a
    # commit message names what was done, and these did nothing — but settled:
    # the plan is finished without them, and waiting on them would mean the
    # project build after the last pack never runs.
    nothing_to_do: List[str] = field(default_factory=list)
    # The project this conversation is bound to, resolved. R2 evidence and a
    # parked estimate belong to ONE repository: a conversation that followed the
    # browser from project to project would gate a paid run on another repo's
    # discovery.
    source_dir: str = ""
    output_dir: str = ""
    # Branches land_on_branch created in THIS conversation: branch -> {"base_branch",
    # "commit", "source_dir"}. open_pull_request pushes only a branch named here
    # — never one the user already had — and opens the PR into its base_branch,
    # the branch landing started from. `last_landed` is its default.
    landings: Dict[str, dict] = field(default_factory=dict)
    last_landed: str = ""
    # leader.migrate_on_branch: the branch this chat created before its first
    # pack, which every pack commits onto. Also recorded in `landings`, so
    # open_pull_request publishes it like any other branch the chat made.
    work_branch: str = ""
    # branch -> pull request URL, once open_pull_request has opened (or found) one.
    pull_requests: Dict[str, str] = field(default_factory=dict)
    # The job currently writing this conversation, stamped onto every item it
    # adds. A reload mid-turn renders the finished items from the transcript and
    # lets the live stream draw the rest; without the stamp both sources draw
    # the same turn.
    job_id: Optional[str] = None
    # pending_id -> {"tool", "args", "est_usd", "title"}. A gated tool call
    # parks here until the user clicks; nothing else may execute it.
    pending: Dict[str, dict] = field(default_factory=dict)
    spend_usd: float = 0.0          # what the pipeline spent, from tool results
    leader_cost_usd: float = 0.0    # what the leader's own calls cost
    leader_calls: int = 0
    turns: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ── the turn ─────────────────────────────────────────────────────────────

    def begin_turn(self, job_id: str) -> int:
        """Claim this conversation for one job; return the turn number.

        Idempotent for the same job id. The route target claims the turn before
        the agent exists — so a credential error still lands in the transcript
        rather than vanishing into a failed job — and ``run_turn`` claims it
        again; counting that twice would renumber the turn for no reason.
        """
        with self.lock:
            if self.job_id != job_id:
                self.job_id = job_id
                self.turns += 1
            return self.turns

    def end_turn(self) -> None:
        with self.lock:
            self.job_id = None

    def bind(self, source_dir: str, output_dir: str) -> bool:
        """Tie the conversation to one project. False when it already holds another.

        Discovery, the completed packs and every parked estimate describe one
        repository. Letting the browser point the same chat at a second one
        would let evidence gathered on A authorise a paid run on B, so the
        caller turns a False into a 400 rather than silently re-binding.
        """
        with self.lock:
            if not self.source_dir and not self.output_dir:
                self.source_dir, self.output_dir = source_dir, output_dir
                return True
            return self.source_dir == source_dir and self.output_dir == output_dir

    def record_landing(self, branch: str, base_branch: str, commit: str, source_dir: str) -> None:
        with self.lock:
            self.landings[branch] = {"base_branch": base_branch, "commit": commit, "source_dir": source_dir}
            self.last_landed = branch

    # ── transcript ───────────────────────────────────────────────────────────

    def add_item(self, item: dict) -> dict:
        """Append a transcript item and stamp it with an id. Returns the item.

        The returned dict is the live object: a tool row is created when the
        call starts and completed in place when it returns.
        """
        with self.lock:
            item["id"] = len(self.transcript) + 1
            item["job_id"] = self.job_id
            self.transcript.append(item)
            return item

    def find_item(self, **match) -> Optional[dict]:
        with self.lock:
            for item in reversed(self.transcript):
                if all(item.get(k) == v for k, v in match.items()):
                    return item
        return None

    def snapshot(self) -> List[dict]:
        """A copy safe to serialise while a turn is still writing."""
        with self.lock:
            return [dict(item) for item in self.transcript]

    # ── model history ────────────────────────────────────────────────────────

    def trimmed_history(self, limit: int) -> List[BaseMessage]:
        """The last `limit` messages, cut only where it is safe to cut.

        A tool result is meaningless without the assistant turn that asked for
        it — Bedrock rejects a ``toolResult`` with no matching ``toolUse`` — so
        the window is moved back to the nearest user turn rather than slicing
        mid-exchange.
        """
        with self.lock:
            if limit <= 0 or len(self.history) <= limit:
                return list(self.history)
            start = len(self.history) - limit
            while start > 0 and not isinstance(self.history[start], HumanMessage):
                start -= 1
            return list(self.history[start:])

    def add_message(self, message: BaseMessage) -> None:
        with self.lock:
            self.history.append(message)

    # ── pending confirmations ────────────────────────────────────────────────

    def add_pending(self, tool: str, args: dict, est_usd: float, title: str) -> str:
        with self.lock:
            pending_id = "p" + _new_id()
            self.pending[pending_id] = {
                "pending_id": pending_id, "tool": tool, "args": dict(args),
                "est_usd": round(float(est_usd), 4), "title": title,
            }
            return pending_id

    def take_pending(self, pending_id: str) -> Optional[dict]:
        """Remove and return a pending call. One click, one execution."""
        with self.lock:
            return self.pending.pop(pending_id, None)

    def pending_list(self) -> List[dict]:
        with self.lock:
            return [dict(p) for p in self.pending.values()]

    # ── accounting ───────────────────────────────────────────────────────────

    def accrue_pipeline(self, usd: float) -> None:
        with self.lock:
            self.spend_usd = round(self.spend_usd + float(usd or 0.0), 6)

    def accrue_leader(self, usd: float) -> None:
        with self.lock:
            self.leader_cost_usd = round(self.leader_cost_usd + float(usd or 0.0), 6)
            self.leader_calls += 1

    def to_json(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "conversation_id": self.id,
                "transcript": [dict(i) for i in self.transcript],
                "pending": [dict(p) for p in self.pending.values()],
                "completed": list(self.completed),
                # The browser reads the output directory from here on a reload:
                # it is the repository's `.migrated` unless the user named one,
                # and a page that guessed would read another run's queue.
                "source_dir": self.source_dir,
                "output_dir": self.output_dir,
                "spend_usd": round(self.spend_usd, 6),
                "leader_cost_usd": round(self.leader_cost_usd, 6),
                "leader_calls": self.leader_calls,
                "turns": self.turns,
            }


class ConversationStore:
    """Every conversation this process has seen. Thread-safe, not persisted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: Dict[str, Conversation] = {}

    def create(self) -> Conversation:
        convo = Conversation()
        with self._lock:
            self._by_id[convo.id] = convo
        return convo

    def get(self, conversation_id: Optional[str]) -> Optional[Conversation]:
        if not conversation_id:
            return None
        with self._lock:
            return self._by_id.get(conversation_id)

    def get_or_create(self, conversation_id: Optional[str]) -> Conversation:
        """An unknown id is a fresh conversation, not an error.

        A browser holding the id of a conversation from before a restart should
        get a working chat, not a 404 it cannot clear.
        """
        return self.get(conversation_id) or self.create()

    def reset(self, conversation_id: str) -> Conversation:
        with self._lock:
            self._by_id.pop(conversation_id, None)
        return self.create()
