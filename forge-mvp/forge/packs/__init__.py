"""The stack pack library — one technology transition per pack.

Packs live as markdown-with-frontmatter under ``prompts/packs/`` so that adding
support for a technology is a new file rather than a code change. See
``prompts/FORGE-Platform-Requirements.md`` for the contract.
"""

from forge.packs.loader import (
    PackRegistry,
    load_packs,
    packs_dir,
    parse_pack,
    response_maxima,
    rubric_weights,
)
from forge.packs.spec import (
    ACCEPTANCE_KINDS,
    DETECT_KINDS,
    STATUSES,
    TIERS,
    AcceptanceCheck,
    DetectRule,
    PackError,
    PackSpec,
)

__all__ = [
    "ACCEPTANCE_KINDS",
    "DETECT_KINDS",
    "STATUSES",
    "TIERS",
    "AcceptanceCheck",
    "DetectRule",
    "PackError",
    "PackRegistry",
    "PackSpec",
    "load_packs",
    "packs_dir",
    "parse_pack",
    "response_maxima",
    "rubric_weights",
]
