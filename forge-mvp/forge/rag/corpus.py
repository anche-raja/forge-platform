"""Enterprise-standards corpus for prompt-stuffing RAG.

The corpus is a handful of small markdown files (enterprise coding standards,
Spring/Struts migration patterns, etc.). It is small enough to fit in a prompt,
so the cheapest "RAG" is to select the relevant files by simple rules and inject
them directly — no vector store, no embeddings, ~$0.

The same docs directory is what the (deferred) Bedrock Knowledge Base would be
seeded from, so there is a single source of truth.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

# forge/rag/corpus.py -> parents[2] == forge-mvp/ ; the docs live alongside in
# the sibling forge-terraform/docs (same files the rag Terraform module seeds).
_DEFAULT_DOCS_DIR = (
    Path(__file__).resolve().parents[2].parent / "forge-terraform" / "docs"
)

# Always-included baseline standards.
_ALWAYS = ["coding_standards.md", "arch_decisions.md"]

# (substring-in-lowercased-path) -> doc to add. First match per doc wins; order
# below also defines append order so the rendered context is deterministic.
_RULES = [
    (("controller", "action", "struts", "/web/", "servlet"), "struts2_to_mvc_rules.md"),
    (("service", "repository", "dao", "config", "spring", "applicationcontext"), "spring_migration_patterns.md"),
    (("liberty", "server.xml", "web.xml", "ejb", "jndi"), "liberty_config_standards.md"),
]


def corpus_dir(config) -> Path:
    """Resolve the docs directory: env > config.rag_docs_dir > default sibling."""
    env = os.environ.get("FORGE_RAG_DOCS_DIR")
    if env:
        return Path(env)
    cfg = config.get("rag_docs_dir", "") if config else ""
    if cfg:
        return Path(cfg)
    return _DEFAULT_DOCS_DIR


@lru_cache(maxsize=8)
def _read_dir(dir_str: str) -> Dict[str, str]:
    d = Path(dir_str)
    out: Dict[str, str] = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        try:
            out[f.name] = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
    return out


def load_corpus(config) -> Dict[str, str]:
    """Return {filename: contents} for every markdown doc in the corpus dir.

    Cached per directory so the files are read once per process, not per file
    being migrated.
    """
    return _read_dir(str(corpus_dir(config).resolve()))


def select_docs(phase: str, file_path: str, scope_prefix: str, config) -> List[str]:
    """Pick the relevant doc filenames for one source file (rule-based, no embeddings)."""
    available = set(load_corpus(config).keys())
    selected: List[str] = []

    def _add(name: str):
        if name in available and name not in selected:
            selected.append(name)

    for name in _ALWAYS:
        _add(name)

    lowered = (file_path or "").replace("\\", "/").lower()
    for needles, doc in _RULES:
        if any(n in lowered for n in needles):
            _add(doc)

    return selected


def render_context(names: List[str], corpus: Dict[str, str], max_chars: int = 6000) -> str:
    """Render the selected docs into a single prompt-injectable block, capped at max_chars."""
    if not names:
        return ""
    parts = ["# ENTERPRISE STANDARDS (authoritative — follow these)\n"]
    for name in names:
        body = corpus.get(name, "")
        if not body:
            continue
        parts.append(f"## {name}\n{body}\n")
    rendered = "\n".join(parts).rstrip()
    if max_chars and len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip() + "\n...[standards truncated]"
    return rendered
