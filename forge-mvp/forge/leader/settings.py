"""The ``leader`` block of agents.yaml, resolved once.

Defaults live here rather than at every call site, so a missing block behaves
exactly like the documented one — the same reasoning as
:mod:`forge.testgen.settings`.

``confirm_above_usd`` is the dial that decides how much authority the leader
actually has. A tool whose estimated spend exceeds it does not run: it registers
a pending confirmation and comes back to the user as a card. Set it to ``0`` and
the leader spends without asking, which is a deliberate choice and not the
default — one ambiguous sentence should not be able to start a $100 run.
Applying review decisions is confirmed whatever this says, because an approval
is the human's signature on someone else's code.

``auto_publish`` is the same kind of dial for the end of a plan. Off, landing
and the pull request each wait for a click. On, a plan whose build passed is
landed on ``<branch_prefix>-<timestamp>`` and its pull request opened with no
click -- through the same handlers and the same refusals.
"""

from dataclasses import dataclass

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOKENS = 2048
DEFAULT_CONFIRM_ABOVE_USD = 1.0
DEFAULT_HISTORY_MESSAGES = 40
# The platform's own documented average, from GUARDRAILS.md §8 and TODO.md:
# three model calls per unit, about $0.07 for a file of average size. Used only
# to estimate before a run, never to report what one actually cost — that
# number comes from `estimated_cost_usd`, accrued per real call.
DEFAULT_UNIT_COST_USD = 0.07
DEFAULT_BRANCH_PREFIX = "forge/migration"


def _bool(value) -> bool:
    """YAML ``true``, or a string that plainly means it -- never "false" read as truthy."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return value is True


def _float(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out >= 0 else default


def _int(value, default: int, *, minimum: int = 1) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out >= minimum else default


@dataclass(frozen=True)
class LeaderSettings:
    model: str = ""
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = DEFAULT_MAX_TOKENS
    confirm_above_usd: float = DEFAULT_CONFIRM_ABOVE_USD
    unit_cost_usd: float = DEFAULT_UNIT_COST_USD
    history_messages: int = DEFAULT_HISTORY_MESSAGES
    auto_publish: bool = False
    branch_prefix: str = DEFAULT_BRANCH_PREFIX

    @classmethod
    def from_config(cls, config) -> "LeaderSettings":
        block = (config.get("leader") if config is not None else None) or {}
        if not isinstance(block, dict):
            block = {}
        fallback = config.get("transform_model", "") if config is not None else ""
        return cls(
            model=str(block.get("model") or fallback),
            max_steps=_int(block.get("max_steps"), DEFAULT_MAX_STEPS),
            max_tokens=_int(block.get("max_tokens"), DEFAULT_MAX_TOKENS, minimum=256),
            # Zero is meaningful here (never confirm), so it is not coerced away.
            confirm_above_usd=_float(block.get("confirm_above_usd"), DEFAULT_CONFIRM_ABOVE_USD),
            unit_cost_usd=_float(block.get("unit_cost_usd"), DEFAULT_UNIT_COST_USD) or DEFAULT_UNIT_COST_USD,
            history_messages=_int(block.get("history_messages"), DEFAULT_HISTORY_MESSAGES, minimum=4),
            auto_publish=_bool(block.get("auto_publish")),
            branch_prefix=str(block.get("branch_prefix") or "").strip().strip("/-") or DEFAULT_BRANCH_PREFIX,
        )
