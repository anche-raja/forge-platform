from langgraph.graph import StateGraph, END

from forge.agents.guardrails_pre import GuardrailsPreAgent
from forge.agents.guardrails_post import GuardrailsPostAgent
from forge.agents.java_upgrade import JavaUpgradeAgent
from forge.config import ForgeConfig
from forge.review.java_reviewer import JavaReviewer
from forge.state import ForgeState
from forge.state_store.dynamodb import DynamoDBSaver
from forge.utils.file_writer import write_output


def build_graph(config: ForgeConfig):
    pre_agent = GuardrailsPreAgent(config)
    upgrade_agent = JavaUpgradeAgent(config)
    reviewer = JavaReviewer(config)
    post_agent = GuardrailsPostAgent(config)

    # ─── Node functions ───────────────────────────────────────────────────────

    def guardrails_pre(state: ForgeState) -> ForgeState:
        return pre_agent.run(state)

    def java_upgrade(state: ForgeState) -> ForgeState:
        return upgrade_agent.run(state)

    def java_reviewer(state: ForgeState) -> ForgeState:
        return reviewer.review(state)

    def guardrails_post(state: ForgeState) -> ForgeState:
        return post_agent.run(state)

    def write_file(state: ForgeState) -> ForgeState:
        write_output(state)
        file_status = dict(state["current_file"])
        file_status["status"] = "DONE"
        return {**state, "current_file": file_status}

    def manual_queue(state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_status["status"] = "MANUAL_REVIEW"
        return {**state, "current_file": file_status}

    def blocked(state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_status["status"] = "BLOCKED"
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
            # Increment retry count before looping back
            file_status = dict(fs)
            retry_count += 1
            file_status["retry_count"] = retry_count
            file_status["status"] = f"RETRY_{retry_count}"
            state["current_file"] = file_status
            return "java_upgrade"
        return "manual_queue"

    def route_post(state: ForgeState) -> str:
        if state["current_file"].get("status") == "MANUAL_REVIEW":
            return "manual_queue"
        return "write_file"

    # ─── Build graph ──────────────────────────────────────────────────────────

    graph = StateGraph(ForgeState)

    graph.add_node("guardrails_pre", guardrails_pre)
    graph.add_node("java_upgrade", java_upgrade)
    graph.add_node("java_reviewer", java_reviewer)
    graph.add_node("guardrails_post", guardrails_post)
    graph.add_node("write_file", write_file)
    graph.add_node("manual_queue", manual_queue)
    graph.add_node("blocked", blocked)
    graph.add_node("update_state", update_state)

    graph.set_entry_point("guardrails_pre")

    graph.add_conditional_edges("guardrails_pre", route_pre, {
        "blocked": "blocked",
        "java_upgrade": "java_upgrade",
    })
    graph.add_edge("java_upgrade", "java_reviewer")
    graph.add_conditional_edges("java_reviewer", route_reviewer, {
        "guardrails_post": "guardrails_post",
        "java_upgrade": "java_upgrade",
        "manual_queue": "manual_queue",
    })
    graph.add_conditional_edges("guardrails_post", route_post, {
        "write_file": "write_file",
        "manual_queue": "manual_queue",
    })
    graph.add_edge("write_file", "update_state")
    graph.add_edge("manual_queue", "update_state")
    graph.add_edge("blocked", "update_state")
    graph.add_edge("update_state", END)

    checkpointer = DynamoDBSaver(config)
    return graph.compile(checkpointer=checkpointer)
