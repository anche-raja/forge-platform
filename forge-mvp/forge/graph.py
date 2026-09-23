from langgraph.graph import StateGraph, END

from forge.agents.guardrails_pre import GuardrailsPreAgent
from forge.agents.guardrails_post import GuardrailsPostAgent
from forge.agents.java_upgrade import JavaUpgradeAgent
from forge.config import ForgeConfig
from forge.review.java_reviewer import JavaReviewer
from forge.state import ForgeState
from forge.state_store.dynamodb import DynamoDBSaver
from forge.utils.file_writer import stage_output, write_output
from forge.utils.telemetry import get_logger
from forge.verify import syntax
from forge.verify.build_verifier import BuildVerifier

_log = get_logger(__name__)


def build_graph(config: ForgeConfig):
    pre_agent = GuardrailsPreAgent(config)
    upgrade_agent = JavaUpgradeAgent(config)
    reviewer = JavaReviewer(config)
    post_agent = GuardrailsPostAgent(config)
    build_verifier = BuildVerifier(config)

    # ─── Node functions ───────────────────────────────────────────────────────

    def guardrails_pre(state: ForgeState) -> ForgeState:
        return pre_agent.run(state)

    def java_upgrade(state: ForgeState) -> ForgeState:
        return upgrade_agent.run(state)

    check_syntax = syntax.enabled(config)
    _SYNTAX_ERROR = "Transform output does not parse"
    _SYNTAX_SKIPPED = "syntax check skipped: no javac found for the project's JDK"

    def syntax_check(state: ForgeState) -> ForgeState:
        """javac parses the transform output before a reviewer is paid to read it.

        A model sometimes damages a file it otherwise migrated correctly -- a
        stray brace, stray characters after one -- and a reviewer reading for
        meaning passes it. A FAIL goes back through the same retry loop as a
        low score, with javac's errors as the feedback. A unit the transform
        already sent to manual review passes through untouched.
        """
        file_status = dict(state["current_file"])
        if (not check_syntax or file_status.get("status") == "MANUAL_REVIEW"
                or file_status.get("transform_malformed")):
            return state
        # This attempt's verdict replaces the last one's, so a retry that
        # parses does not carry the previous failure's error forward.
        if str(file_status.get("error") or "").startswith(_SYNTAX_ERROR):
            file_status["error"] = None
        files = (file_status.get("transform_output") or {}).get("files") or {}
        verdict, errors = syntax.check_files(files, config)
        file_status["syntax_verdict"] = verdict
        file_status["syntax_errors"] = errors
        findings = list(file_status.get("guardrail_findings") or [])
        if verdict == syntax.FAIL:
            _log.info("%s does not parse (attempt %d)", file_status["file_path"],
                      (file_status.get("retry_count") or 0) + 1)
            file_status["error"] = f"{_SYNTAX_ERROR}: {errors[0]}"
            file_status["review_feedback"] = (
                "Your output does not parse. Return the whole file again with exactly these errors "
                "fixed, and change nothing else:\n" + "\n".join(errors)
            )
            findings += [f"syntax: {e}" for e in errors[:3]]
        elif verdict == syntax.SKIPPED and _SYNTAX_SKIPPED not in findings:
            findings.append(_SYNTAX_SKIPPED)
        file_status["guardrail_findings"] = findings
        return {**state, "current_file": file_status}

    def java_reviewer(state: ForgeState) -> ForgeState:
        return reviewer.review(state)

    def guardrails_post(state: ForgeState) -> ForgeState:
        return post_agent.run(state)

    def write_file(state: ForgeState) -> ForgeState:
        written = write_output(state)
        file_status = dict(state["current_file"])
        file_status["written_paths"] = written
        file_status["status"] = "DONE"
        return {**state, "current_file": file_status}

    def verify_build(state: ForgeState) -> ForgeState:
        """Compile what was written. A failure is fed back as review feedback."""
        result = build_verifier.verify(state)
        file_status = dict(state["current_file"])
        file_status["build_verdict"] = result["verdict"]
        file_status["build_output"] = result["output"]

        if result["verdict"] == "FAIL":
            _log.info("Build verification failed for %s", file_status["file_path"])
            # Reuse the transform agent's feedback channel so the compiler errors
            # are injected into the retry prompt.
            file_status["review_feedback"] = (
                "The transformed code does not compile. Fix these compiler errors:\n"
                f"{result['output']}"
            )
        return {**state, "current_file": file_status}

    _CEILINGS = ("auto", "review-high", "review-all")

    def risk_ceiling() -> str:
        value = (config.get("decisions") or {}).get("risk_ceiling", "review-high")
        if value not in _CEILINGS:
            _log.warning("Unknown risk_ceiling %r; treating as review-high", value)
            return "review-high"
        return value

    def must_hold(fs) -> bool:
        ceiling = risk_ceiling()
        return ceiling == "review-all" or (ceiling == "review-high" and fs.get("risk_tier") == "HIGH")

    def hold_for_review(state: ForgeState) -> ForgeState:
        """Stage instead of write: a human decides before this lands in output."""
        staged = stage_output(state)
        file_status = dict(state["current_file"])
        file_status["status"] = "HELD"
        file_status["held_paths"] = staged
        file_status["hold_reason"] = f"risk_ceiling={risk_ceiling()}, risk_tier={file_status.get('risk_tier')}"
        _log.info("Held %s for review (%s)", file_status["file_path"], file_status["hold_reason"])
        return {**state, "current_file": file_status}

    def manual_queue(state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_status["status"] = "MANUAL_REVIEW"
        return {**state, "current_file": file_status}

    def blocked(state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_status["status"] = "BLOCKED"
        return {**state, "current_file": file_status}

    def increment_retry(state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        retry_count = (file_status.get("retry_count") or 0) + 1
        file_status["retry_count"] = retry_count
        file_status["status"] = f"RETRY_{retry_count}"
        return {**state, "current_file": file_status}

    def update_state(state: ForgeState) -> ForgeState:
        fs = state["current_file"]
        status = fs.get("status")
        new_state = dict(state)
        new_state["files_processed"] = state.get("files_processed", 0) + 1
        if status == "DONE":
            new_state["files_passed"] = state.get("files_passed", 0) + 1
            if (fs.get("retry_count") or 0) > 0:
                new_state["files_retried"] = state.get("files_retried", 0) + 1
        elif status == "MANUAL_REVIEW":
            new_state["files_manual"] = state.get("files_manual", 0) + 1
        elif status == "BLOCKED":
            new_state["files_blocked"] = state.get("files_blocked", 0) + 1
        elif status == "HELD":
            new_state["files_held"] = state.get("files_held", 0) + 1
        return new_state

    # ─── Routing ──────────────────────────────────────────────────────────────

    def route_pre(state: ForgeState) -> str:
        if state["current_file"].get("status") == "BLOCKED":
            return "blocked"
        return "java_upgrade"

    def route_reviewer(state: ForgeState) -> str:
        fs = state["current_file"]
        score = fs.get("review_score", 0) or 0
        retry_count = fs.get("retry_count", 0) or 0
        pass_threshold = config.get("pass_threshold", 80)
        retry_threshold = config.get("retry_threshold", 50)
        max_retries = config.get("max_retries", 2)

        if score >= pass_threshold:
            return "guardrails_post"
        if score >= retry_threshold and retry_count < max_retries:
            return "increment_retry"
        return "manual_queue"

    def route_syntax(state: ForgeState) -> str:
        """Everything the transform node can conclude, routed before a review is paid for.

        An answer that could not be read and one that does not parse are the
        same kind of failure -- the model's output is wrong, not the migration --
        so both retry within ``max_retries`` and cost no review call. A unit
        the transform already sent to manual review (its source could not be
        read) goes straight there: a reviewer has nothing to grade, and on a
        retry would grade the previous attempt's output.
        """
        fs = state["current_file"]
        if fs.get("status") == "MANUAL_REVIEW":
            return "manual_queue"
        if not fs.get("transform_malformed") and fs.get("syntax_verdict") != syntax.FAIL:
            return "java_reviewer"
        if (fs.get("retry_count") or 0) < config.get("max_retries", 2):
            return "increment_retry"
        return "manual_queue"

    def route_verify(state: ForgeState) -> str:
        fs = state["current_file"]
        if fs.get("build_verdict") != "FAIL":
            return "update_state"
        retry_count = fs.get("retry_count", 0) or 0
        if retry_count < config.get("max_retries", 2):
            return "increment_retry"
        return "manual_queue"

    def route_post(state: ForgeState) -> str:
        fs = state["current_file"]
        if fs.get("status") == "MANUAL_REVIEW":
            return "manual_queue"
        if must_hold(fs):
            return "hold_for_review"
        return "write_file"

    # ─── Build graph ──────────────────────────────────────────────────────────

    graph = StateGraph(ForgeState)

    graph.add_node("guardrails_pre", guardrails_pre)
    graph.add_node("java_upgrade", java_upgrade)
    graph.add_node("syntax_check", syntax_check)
    graph.add_node("java_reviewer", java_reviewer)
    graph.add_node("guardrails_post", guardrails_post)
    graph.add_node("write_file", write_file)
    graph.add_node("verify_build", verify_build)
    graph.add_node("hold_for_review", hold_for_review)
    graph.add_node("manual_queue", manual_queue)
    graph.add_node("blocked", blocked)
    graph.add_node("increment_retry", increment_retry)
    graph.add_node("update_state", update_state)

    graph.set_entry_point("guardrails_pre")

    graph.add_conditional_edges("guardrails_pre", route_pre, {
        "blocked": "blocked",
        "java_upgrade": "java_upgrade",
    })
    graph.add_edge("java_upgrade", "syntax_check")
    graph.add_conditional_edges("syntax_check", route_syntax, {
        "java_reviewer": "java_reviewer",
        "increment_retry": "increment_retry",
        "manual_queue": "manual_queue",
    })
    graph.add_conditional_edges("java_reviewer", route_reviewer, {
        "guardrails_post": "guardrails_post",
        "increment_retry": "increment_retry",
        "manual_queue": "manual_queue",
    })
    graph.add_edge("increment_retry", "java_upgrade")
    graph.add_conditional_edges("guardrails_post", route_post, {
        "write_file": "write_file",
        "hold_for_review": "hold_for_review",
        "manual_queue": "manual_queue",
    })
    # A held unit never reaches verify_build: nothing was written to output.
    graph.add_edge("hold_for_review", "update_state")
    graph.add_edge("write_file", "verify_build")
    graph.add_conditional_edges("verify_build", route_verify, {
        "update_state": "update_state",
        "increment_retry": "increment_retry",
        "manual_queue": "manual_queue",
    })
    graph.add_edge("manual_queue", "update_state")
    graph.add_edge("blocked", "update_state")
    graph.add_edge("update_state", END)

    checkpointer = DynamoDBSaver(config)
    return graph.compile(checkpointer=checkpointer)
