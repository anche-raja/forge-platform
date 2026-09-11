"""Bedrock token-cost estimation.

`estimated_cost_usd` was declared in ForgeState and never written to, leaving
the FORGE-CostSpike alarm permanently blind. Prices come from agents.yaml
(model_pricing) so they can be corrected without a code change.
"""

from typing import Any, Tuple

from forge.utils.telemetry import get_logger

_log = get_logger(__name__)


def usage_from_response(response: Any) -> Tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a LangChain AIMessage.

    Returns (0, 0) when usage metadata is absent or not numeric — which is the
    case under MagicMock in tests and for providers that omit it.
    """
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return 0, 0
    raw_in = usage.get("input_tokens", 0)
    raw_out = usage.get("output_tokens", 0)
    if not isinstance(raw_in, int) or not isinstance(raw_out, int):
        return 0, 0
    return raw_in, raw_out


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int, pricing: dict) -> float:
    """USD cost for one call, from per-1k-token prices keyed by model id."""
    if not pricing or not model_id:
        return 0.0
    rates = pricing.get(model_id)
    if not isinstance(rates, dict):
        return 0.0
    try:
        in_rate = float(rates.get("input_per_1k", 0.0))
        out_rate = float(rates.get("output_per_1k", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return (input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate


def accrue(state: dict, response: Any, model_id: str, pricing: dict) -> float:
    """Return the running estimated_cost_usd after charging one model call."""
    tokens_in, tokens_out = usage_from_response(response)
    delta = estimate_cost(model_id, tokens_in, tokens_out, pricing)
    if delta:
        _log.debug("%s cost +$%.6f (%d in / %d out)", model_id, delta, tokens_in, tokens_out)
    return round(float(state.get("estimated_cost_usd", 0.0) or 0.0) + delta, 6)
