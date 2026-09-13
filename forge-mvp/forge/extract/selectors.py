"""Selector resolvers for the ``web_bootstrap`` context.

A selector answers "which files does this pack apply to?" when no path glob
can — the answer is in the descriptors, not the filenames.
"""

from pathlib import Path

from forge.extract import Selection

# Where a generated Liberty server configuration lands, relative to the module.
# This is the liberty-maven-plugin's default config directory.
SERVER_XML_REL = Path("src/main/liberty/config/server.xml")


def servlet_components(ctx: dict, module_dir: str) -> Selection:
    """Every filter, listener and servlet class that resolves to a source file."""
    files = sorted({c["file"] for c in ctx.get("servlet_components", ()) if c.get("file")})
    return Selection(files=tuple(files))


def server_config(ctx: dict, module_dir: str) -> Selection:
    """One synthetic unit per module: the ``server.xml`` to generate.

    There is no source file to read — the transform is given the extracted
    context and asked to produce this path.
    """
    return Selection(generated=(str(Path(module_dir) / SERVER_XML_REL),))


def is_generated_target(spec, file_path: str) -> bool:
    """Whether a ``--file`` target is a file the pack *creates* rather than edits."""
    selectors = getattr(spec, "selectors", ())
    if "server_config" not in selectors:
        return False
    path = Path(file_path)
    return path.name == "server.xml" and not path.exists()
