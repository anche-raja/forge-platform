"""Selector resolvers for the ``web_bootstrap`` context.

A selector answers "which files does this pack apply to?" when no path glob
can — the answer is in the descriptors, not the filenames.
"""

from pathlib import Path

from forge.extract import Selection

# Where a Tomcat context descriptor lands, relative to the module: inside the
# WAR, so Tomcat applies it at deploy with no server-side file to maintain.
TOMCAT_CONTEXT_REL = Path("src/main/webapp/META-INF/context.xml")

# The generated file each generating selector creates, by file name.
_GENERATES = {"tomcat_context": TOMCAT_CONTEXT_REL.name}


def servlet_components(ctx: dict, module_dir: str) -> Selection:
    """Every filter, listener and servlet class that resolves to a source file."""
    files = sorted({c["file"] for c in ctx.get("servlet_components", ()) if c.get("file")})
    return Selection(files=tuple(files))


def tomcat_context(ctx: dict, module_dir: str) -> Selection:
    """One synthetic unit per module: the Tomcat ``META-INF/context.xml`` to generate.

    There is no source file to read — the transform is given the extracted
    context and asked to produce this path. It supplies what a full server
    used to: every ``resource-ref`` the application looks up
    (``jdbc/amsInternalDS``) as a ``<Resource>``. An existing one is migrated
    like any other file (see file_scanner).
    """
    return Selection(generated=(str(Path(module_dir) / TOMCAT_CONTEXT_REL),))


def is_generated_target(spec, file_path: str) -> bool:
    """Whether a ``--file`` target is a file the pack *creates* rather than edits."""
    path = Path(file_path)
    names = {_GENERATES[s] for s in getattr(spec, "selectors", ()) if s in _GENERATES}
    return path.name in names and not path.exists()
