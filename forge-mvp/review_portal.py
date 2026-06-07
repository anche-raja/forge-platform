"""FORGE manual-review portal (Streamlit).

Single-user internal tool to review files the pipeline escalated to
MANUAL_REVIEW. Lists them, shows a diff of the transformed output vs the
original, and lets a human Approve (write the file + mark DONE) or Reject
(re-enqueue as PENDING for --resume, or keep flagged).

Run:
    cd forge-mvp
    set FORGE_AGENTS_YAML=agents.local.yaml   (Windows)  / export on *nix
    streamlit run review_portal.py

DynamoDB is the source of truth (read via the status-index GSI); the
manual-review-queue.json file is offered as a fallback when DynamoDB is empty
or unreachable. SQS is NOT consumed here — it's only the "a human is needed"
signal; this portal works off DynamoDB/JSON.
"""

import difflib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from forge.config import ForgeConfig
from forge.state_store.dynamodb import DynamoDBStateManager
from forge.utils.file_writer import write_output

QUEUE_JSON = "manual-review-queue.json"


# ─── Data loading ───────────────────────────────────────────────────────────

@st.cache_resource
def _config():
    return ForgeConfig(os.environ.get("FORGE_AGENTS_YAML", "agents.yaml"))


@st.cache_resource
def _state_manager():
    return DynamoDBStateManager(_config())


def load_from_dynamo():
    try:
        return _state_manager().get_files_by_status("MANUAL_REVIEW"), None
    except Exception as e:
        return [], str(e)


def load_from_json():
    p = Path(QUEUE_JSON)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


# ─── Actions ─────────────────────────────────────────────────────────────────

def approve(fs: dict, source_dir: str, output_dir: str, reviewer: str):
    state = {
        "dry_run": False,
        "source_dir": source_dir,
        "output_dir": output_dir,
        "current_file": fs,
    }
    write_output(state)  # writes transform_output["files"] under output_dir
    fs2 = dict(fs)
    fs2["status"] = "DONE"
    fs2["reviewer"] = reviewer
    fs2["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    _state_manager().put_file_status(fs2)


def reject(fs: dict, note: str, reviewer: str, *, reenqueue: bool):
    fs2 = dict(fs)
    prefix = "[REVIEWER] "
    existing = fs2.get("review_feedback") or ""
    fs2["review_feedback"] = (existing + f"\n{prefix}{note}").strip()
    fs2["reviewer"] = reviewer
    fs2["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    # PENDING -> migrate.py --resume reprocesses it; else keep MANUAL_REVIEW (flagged).
    fs2["status"] = "PENDING" if reenqueue else "MANUAL_REVIEW"
    _state_manager().put_file_status(fs2)


# ─── UI ────────────────────────────────────────────────────────────────────────

def render_diff(fs: dict):
    transform = fs.get("transform_output") or {}
    files = transform.get("files") or {}
    if not files:
        st.info("No transform_output.files on this record (nothing to diff).")
        return
    for path, new_content in files.items():
        st.markdown(f"**{path}**")
        try:
            original = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            original = ""
        diff = difflib.unified_diff(
            original.splitlines(),
            (new_content or "").splitlines(),
            fromfile="original",
            tofile="migrated",
            lineterm="",
        )
        body = "\n".join(diff) or "(identical — no changes)"
        st.code(body, language="diff")


def main():
    st.set_page_config(page_title="FORGE Review Portal", layout="wide")
    st.title("FORGE — Manual Review Portal")

    cfg = _config()
    st.sidebar.header("Settings")
    source_dir = st.sidebar.text_input("Source root", value=r"d:\Forge-POC\cisco-device")
    output_dir = st.sidebar.text_input("Approved output dir", value="./migrated-approved")
    reviewer = st.sidebar.text_input("Reviewer", value=os.environ.get("USERNAME", "reviewer"))
    use_dynamo = st.sidebar.radio("Source", ["DynamoDB", "manual-review-queue.json"]) == "DynamoDB"

    if use_dynamo:
        items, err = load_from_dynamo()
        if err:
            st.warning(f"DynamoDB read failed ({err}); falling back to JSON.")
            items = load_from_json()
    else:
        items = load_from_json()

    st.caption(f"{len(items)} file(s) awaiting manual review")
    if not items:
        st.success("Queue is empty.")
        return

    labels = [f"{Path(fs.get('file_path','?')).name}  (score {fs.get('review_score','-')})" for fs in items]
    idx = st.selectbox("Select a file", range(len(items)), format_func=lambda i: labels[i])
    fs = items[idx]

    st.subheader(fs.get("file_path", "?"))
    c1, c2, c3 = st.columns(3)
    c1.metric("Review score", fs.get("review_score", "-"))
    c2.metric("Verdict", fs.get("review_verdict", "-"))
    c3.metric("Retries", fs.get("retry_count", 0))

    if fs.get("review_feedback"):
        st.markdown("**Reviewer feedback / notes**")
        st.write(fs["review_feedback"])
    if fs.get("guardrail_findings"):
        st.markdown("**Guardrail findings**")
        st.write(fs["guardrail_findings"])
    if fs.get("error"):
        st.error(fs["error"])

    st.divider()
    render_diff(fs)

    st.divider()
    a, b = st.columns(2)
    with a:
        if st.button("✅ Approve — write & mark DONE", type="primary", use_container_width=True):
            try:
                approve(fs, source_dir, output_dir, reviewer)
                st.success(f"Approved. Wrote to {output_dir} and set status=DONE.")
            except Exception as e:
                st.error(f"Approve failed: {e}")
    with b:
        note = st.text_input("Rejection note")
        reenqueue = st.checkbox("Re-enqueue for retry (status PENDING)", value=True)
        if st.button("❌ Reject", use_container_width=True):
            try:
                reject(fs, note, reviewer, reenqueue=reenqueue)
                st.success("Rejected." + (" Re-enqueued as PENDING." if reenqueue else " Kept flagged."))
            except Exception as e:
                st.error(f"Reject failed: {e}")


if __name__ == "__main__":
    main()
