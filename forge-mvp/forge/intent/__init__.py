"""Intent — turning a sentence into decisions, scope and a pack subset.

Discovery answers *what is in this repository*, mechanically, from evidence.
This package answers the question evidence cannot: *which of the routes the
evidence allows did you actually want?* Today that question is answered by a
human editing the ``decisions`` block of ``forge-profile.yaml`` by hand; here it
is answered from a sentence.

Two boundaries make that safe, and they are the whole design:

**The model may narrow, never invent.** ``resolve_packs`` stays the only thing
that decides what a repository contains. A pack with no ``detect`` match cannot
be activated by any prompt — a request for one is recorded as unsupported and
reported. Every safety property lives in :mod:`forge.intent.resolve`, which is
pure Python and holds the bulk of the tests.

**No source code reaches the model.** It is given the profile — build system,
Java level, resolved dependency coordinates, import *prefixes* with counts,
descriptor filenames, file counts — and never a file's contents. This is the
same rule ``GUARDRAILS.md`` §7 states for the pipeline: a model is never the
control that decides what a model may see.

The order of a plan is never the model's: it comes from
``PackRegistry.resolve_order``, the same topological sort discovery uses, and
the result is persisted with provenance so a run replays with no model call.
"""

from forge.intent.plan import IntentPlan
from forge.intent.resolve import reconcile
from forge.intent.vocabulary import DECISION_OPTIONS, render_vocabulary

__all__ = ["IntentPlan", "reconcile", "DECISION_OPTIONS", "render_vocabulary"]
