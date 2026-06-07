"""Context retrieval for the migration agents.

Central entry point that all agents call. Branches on `rag_mode`:
  - off            -> no context (today's behavior)
  - prompt_stuff   -> select relevant standards docs and inject them (cheap, ~$0)
  - knowledge_base -> (future) Bedrock KB retrieval; falls back to prompt_stuff
"""

import logging

from forge.rag.corpus import load_corpus, render_context, select_docs

log = logging.getLogger(__name__)


def retrieve_context(state, config, *, role: str) -> str:
    """Return a standards-context string to prepend to an agent prompt.

    `role` is "transform" or "review" — kept so the two callers can diverge
    later; identical output for now. Returns "" when RAG is off or empty.
    """
    mode = (config.get("rag_mode", "off") if config else "off") or "off"
    if mode == "off":
        return ""

    if mode == "knowledge_base":
        ctx = _kb_retrieve(state, config, role=role)
        if ctx:
            return ctx
        log.warning("rag_mode=knowledge_base unavailable; falling back to prompt_stuff")
        # fall through to prompt_stuff

    # prompt_stuff (and knowledge_base fallback)
    fs = state.get("current_file", {})
    file_path = fs.get("file_path", "")
    phase = state.get("phase", "")
    scope_prefix = config.get("scope_package_prefix", "") if config else ""
    max_chars = int(config.get("rag_max_chars", 6000)) if config else 6000

    names = select_docs(phase, file_path, scope_prefix, config)
    if not names:
        return ""
    return render_context(names, load_corpus(config), max_chars=max_chars)


def _kb_retrieve(state, config, *, role: str) -> str:
    """Future: Bedrock Knowledge Base retrieval. Stubbed — returns "" for now.

    When funded: boto3.client("bedrock-agent-runtime").retrieve(
        knowledgeBaseId=config.knowledge_base_id, retrievalQuery={"text": ...})
    The execution role already grants bedrock:Retrieve.
    """
    return ""
