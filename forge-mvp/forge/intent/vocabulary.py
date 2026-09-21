"""The closed world an intent proposal may draw from.

A model that can only answer in a fixed vocabulary is a model whose answer can
be checked. Everything here is deterministic: the decision enums, what each one
means in one line, the mutually exclusive route families, and a rendering of the
activated packs for the prompt. No model, no AWS.
"""

from typing import Dict, List, Mapping, Sequence

# What each decision may be, from prompts/FORGE-Platform-Requirements.md §4.
# This lived in forge/ui/app.py, which meant the CLI validated nothing at all.
# It is the decision vocabulary, not a UI concern, so it lives here and the UI
# imports it.
DECISION_OPTIONS: Dict[str, List[str]] = {
    "web_framework": ["modernize-in-place", "migrate-to-spring"],
    "runtime": ["war-xml-bootstrap", "war-programmatic-bootstrap"],
    "container": ["liberty", "wildfly", "tomcat", "jetty"],
    "liberty_edition": ["open", "websphere"],
    "liberty_features": ["jakartaee-10.0", "webProfile-10.0", "granular"],
    "views": ["in-place", "thymeleaf", "defer"],
    "url_compat": ["preserve-with-redirect", "preserve-exact", "clean-only"],
    "persistence": ["keep-orm", "to-spring-data"],
    "idiom_aggressiveness": ["conservative", "moderate"],
    "risk_ceiling": ["auto", "review-high", "review-all"],
}

# One line each, for the prompt. A model choosing between enum values needs to
# know what they mean; without this it matches on the string alone.
DECISION_HELP: Mapping[str, str] = {
    "web_framework": "modernize-in-place upgrades the framework in place (Struts 2 -> Struts 7). "
                     "migrate-to-spring replaces it with Spring MVC 6. Mutually exclusive routes.",
    "runtime": "How the WAR bootstraps: keep web.xml, or move to a programmatic initializer.",
    "container": "Target servlet container. liberty is the platform standard.",
    "liberty_edition": "Open Liberty or WebSphere Liberty.",
    "liberty_features": "Whole-platform feature set, web profile, or a granular feature list.",
    "views": "in-place keeps JSP as JSP; thymeleaf converts them; defer leaves the view tier alone.",
    "url_compat": "Whether existing URLs must keep working, exactly or via redirects, or may change.",
    "persistence": "Keep the existing ORM mapping, or move repositories to Spring Data.",
    "idiom_aggressiveness": "How freely to modernise Java idioms. conservative rewrites only what is "
                            "locally provable.",
    "risk_ceiling": "auto writes everything; review-high stages high-risk units for a human; "
                    "review-all stages every unit.",
}

# The route families `web_framework` chooses between. Explicit rather than
# inferred from the pack id: `jsp-jstl-modernize` also declares `web_framework`
# and ends in "-modernize", but it is a view pack that runs on either route, so
# a name-suffix rule would silently drop the JSPs on the Spring route.
# `test_intent_vocabulary.py` asserts every framework-tier pack that reads
# `web_framework` appears here, so a new route pack cannot be added silently.
WEB_FRAMEWORK_ROUTES: Mapping[str, frozenset] = {
    "modernize-in-place": frozenset({"struts2-modernize"}),
    "migrate-to-spring": frozenset({"struts1-to-springmvc6", "struts2-to-springmvc6"}),
}


def route_governed_packs() -> frozenset:
    """Every pack id that the `web_framework` decision arbitrates between."""
    return frozenset().union(*WEB_FRAMEWORK_ROUTES.values())


def render_vocabulary(activations: Sequence[dict], decisions: Mapping[str, str]) -> str:
    """The closed world, as the text block the model is given.

    `activations` are discovery's, already narrowed to what fired — the model is
    never shown a pack the repository has no evidence for, so it cannot ask for
    one by accident.
    """
    lines = ["PACKS THAT APPLY TO THIS REPOSITORY (evidence-based; you may only choose from these):"]
    for a in activations:
        state = "runnable" if a.get("runnable") else ("blocked" if a.get("complete") else "detect-only")
        lines.append(f"  {a['pack']}  [{state}]")
        for e in (a.get("evidence") or [])[:3]:
            lines.append(f"      evidence: {e}")

    lines += ["", "DECISIONS you may set (choose exactly one value from each list, or omit the key):"]
    for key, options in DECISION_OPTIONS.items():
        current = decisions.get(key)
        default = f"  (platform default: {current})" if current else ""
        lines.append(f"  {key}: {' | '.join(options)}{default}")
        lines.append(f"      {DECISION_HELP[key]}")
    return "\n".join(lines)
