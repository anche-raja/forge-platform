"""Discovery — what is actually in a repository, and which packs apply.

Point it at any J2EE project and it answers, without a model call: the build
system and modules, the Java level, every framework and its version, the
descriptors present, the scale — and, from the pack library's own ``detect``
rules, which packs fire and on what evidence. The output is the project
profile the rest of the platform consumes.
"""

from forge.discover.emit import render_summary, write_outputs
from forge.discover.profile import build_profile
from forge.discover.resolve import Activation, resolve_packs

__all__ = ["Activation", "build_profile", "render_summary", "resolve_packs", "write_outputs"]
