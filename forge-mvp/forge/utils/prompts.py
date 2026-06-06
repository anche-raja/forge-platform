"""Load agent prompts from external files.

Keeping prompts out of the Python source means they can be edited and tuned
without changing code. Prompt files live in ``forge-mvp/prompts/`` by default;
override the directory with the ``FORGE_PROMPTS_DIR`` environment variable.
"""

import os
from pathlib import Path

# forge/utils/prompts.py -> parents[2] == forge-mvp/
_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "prompts"


def prompts_dir() -> Path:
    return Path(os.environ.get("FORGE_PROMPTS_DIR", str(_DEFAULT_DIR)))


def load_prompt(name: str) -> str:
    """Return the contents of a prompt file (e.g. ``"java_upgrade.md"``).

    A single trailing newline is stripped so the prompt round-trips exactly as
    if it were an inline string literal.
    """
    path = prompts_dir() / name
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt file not found: {path}. "
            f"Set FORGE_PROMPTS_DIR or add the file under {prompts_dir()}/."
        )
    return path.read_text(encoding="utf-8").rstrip("\n")
