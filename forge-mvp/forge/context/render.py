"""Render an extracted context into the text block a prompt carries.

The block is deterministic for identical input — a retry must see exactly what
the first attempt saw — and bounded: sections are emitted in an order that
depends on what is being transformed, and the first section that would exceed
the budget, plus everything after it, is replaced by a one-line notice. The
full context is always on disk in ``migration-context.json``.
"""

import hashlib
from pathlib import Path
from typing import Dict, Tuple

import yaml

DEFAULT_MAX_CHARS = 60_000
BEGIN = "=== CONTEXT: {name} ==="
END = "=== END CONTEXT ==="

# What matters most depends on the target. Migrating web.xml, the chain and
# the descriptor itself lead; generating server.xml, the resources and any
# existing Liberty config lead so pool sizes and JNDI names are never the
# part that gets cut.
_PRIORITY_EDIT = (
    "web_xml", "filter_chain", "vendors", "servlet_components",
    "unresolved_classes", "datasources", "ear", "existing_server_xml",
)
_PRIORITY_GENERATE = (
    "datasources", "existing_server_xml", "vendors", "web_xml",
    "filter_chain", "ear", "servlet_components", "unresolved_classes",
)
# Internal or duplicated: `authz` restates web_xml sections, `ids` only serves
# .xmi href resolution. Never worth prompt space.
_NEVER_RENDER = frozenset({"authz", "ids", "extractor", "version", "module_dir", "module_rel", "summary"})


def section_priority(for_target: str) -> Tuple[str, ...]:
    return _PRIORITY_GENERATE if Path(for_target).name == "server.xml" else _PRIORITY_EDIT


def _dump(value) -> str:
    return yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None).rstrip("\n")


def _section_text(name: str, value) -> str:
    if name == "web_xml" and isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "ids"}
    return f"## {name}\n{_dump(value)}"


def render_context(name: str, ctx: Dict, *, for_target: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The prompt block for one extracted context, bounded to ``max_chars``.

    ``summary`` is always rendered last and never omitted: it is small and it
    carries the extractor's notes, which are the findings a reviewer must see.
    """
    head = [
        BEGIN.format(name=name),
        f"target: {for_target}",
        f"module: {ctx.get('module_rel', '.')}",
        "",
    ]
    summary = _section_text("summary", ctx.get("summary", {}))
    priority = [s for s in section_priority(for_target) if s in ctx and s not in _NEVER_RENDER]
    extra = sorted(s for s in ctx if s not in priority and s not in _NEVER_RENDER)
    sections = priority + extra

    # Everything that is emitted regardless of content is reserved up front,
    # including one notice line per section and the footer, so the cap is a
    # guarantee rather than a target.
    footer = "(omitted sections are in migration-context.json)"
    fixed = sum(len(h) + 1 for h in head) + len(summary) + len(END) + len(footer) + 2
    notices = sum(len(_notice(s, 10 ** 7)) + 1 for s in sections)
    budget = max_chars - fixed - notices

    body = []
    omitting = False
    for i, section in enumerate(sections):
        text = _section_text(section, ctx[section])
        if not omitting and len(text) + 1 <= budget:
            body.append(text)
            budget -= len(text) + 1
            continue
        if i == 0 and not omitting and budget > 260:
            # Even the most important section does not fit: hard-cut it so the
            # prompt still carries its head rather than nothing.
            cut = budget - 60
            body.append(text[:cut] + f"\n... [TRUNCATED at {cut} chars]")
            budget = 0
        else:
            body.append(_notice(section, len(text)))
        omitting = True

    tail = [footer] if omitting else []
    return "\n".join(head + body + tail + [summary, END])


def _notice(section: str, chars: int) -> str:
    return f"## {section}: OMITTED ({chars} chars)"


def context_digest(rendered: str) -> str:
    """sha256 of the rendered block — what the audit trail records, not the block."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
