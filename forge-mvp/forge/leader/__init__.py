"""The Leader Agent — a model-driven conductor for the conversation layer only.

``CLAUDE.md`` says *do not add a model-driven leader*, and that rule still
stands for the thing it is about: the per-file control plane in
``forge/graph.py``. The owner has reversed it for one layer and one layer only,
and the scope of the reversal IS the design:

| The leader decides                      | Code still decides — unchanged                       |
|-----------------------------------------|------------------------------------------------------|
| which tool to call next, when to ask    | which packs exist: ``resolve_packs`` over ``detect``  |
| which pack to run next                  | which files a pack takes: ``scan_java_files``, scope  |
| how to explain a result                 | everything inside a run: ``route_reviewer``, the gate |
| which review decisions to *propose*     | what a decision may be; every approval is a click     |

So the objections the rule raises are answered rather than ignored. There is no
fifth model call per file — the leader sits above ``forge/service.py`` and never
inside the graph. The order of a run is still ``resolve_order``'s. And a wrong
answer costs a wasted turn, because nothing here can activate a pack the
evidence does not show, lower the review gate, or approve a file.

The modules, and what each one exists to hold:

- ``settings``  the ``leader:`` block of agents.yaml, including
  ``confirm_above_usd`` — the dial that decides how much authority the leader
  actually has.
- ``convo``     conversation state, split in two: ``history`` is what the model
  sees, ``transcript`` is what the browser renders. A card may carry a diff; an
  observation never does.
- ``cards``     the only place a queue entry, a guardrail finding or an
  acceptance result becomes something a model may read. GUARDRAILS.md §7 one
  layer up.
- ``tools``     the closed catalogue of fourteen wrappers, plus the evidence,
  spend and approval gates. Three of them exist because the wizard was
  deleted: ``set_project`` (the leader asks for the folder instead of a form),
  ``list_artifacts`` (what step 9 showed) and ``land_on_branch``; a fourth,
  ``open_pull_request``, publishes what landing committed.
- ``landing``   the only code in FORGE that writes into the user's own
  repository, and it does so once, on a branch, after a click.
- ``pull_request`` the only code in FORGE that pushes: the landed branch to
  ``origin`` and a pull request through ``gh``, after its own click.
- ``agent``     the turn: stream, admit tool calls, execute, answer, repeat.
"""

from forge.leader.convo import Conversation, ConversationStore
from forge.leader.settings import LeaderSettings
from forge.leader.tools import TOOL_DEFS, TOOL_NAMES, ProjectContext, Toolbox, ToolOutcome

# Every event a chat turn can emit. app.js only forwards the SSE event types it
# has listed, so a name missing from its EVENT_TYPES array is silently dropped
# by EventSource rather than failing anywhere a test would see.
CHAT_EVENT_TYPES = (
    "turn_start", "assistant_delta", "assistant_message",
    "tool_start", "card", "tool_result", "usage",
)

__all__ = [
    "CHAT_EVENT_TYPES", "Conversation", "ConversationStore", "LeaderSettings",
    "ProjectContext", "TOOL_DEFS", "TOOL_NAMES", "Toolbox", "ToolOutcome",
]
