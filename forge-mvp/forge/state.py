from typing import TypedDict, Optional, List, Literal, Any


class FileStatus(TypedDict):
    file_path: str
    status: Literal["PENDING", "TRANSFORMING", "REVIEWING", "VERIFYING", "RETRY_1", "RETRY_2", "DONE",
                    "MANUAL_REVIEW", "BLOCKED", "HELD", "REJECTED"]
    phase: str
    risk_tier: str
    risk_score: int
    # Why the score is what it is — the text a held unit shows a reviewer.
    risk_reasons: List[str]
    transform_output: Optional[dict]
    # The transform's reply could not be read (bad JSON, wrong shape). It is
    # retried like a low score, and `error` says why (issue #20).
    transform_malformed: bool
    review_score: Optional[int]
    review_verdict: Optional[Literal["PASS", "RETRY", "MANUAL"]]
    review_feedback: Optional[str]
    guardrail_pre_verdict: Optional[str]
    guardrail_post_verdict: Optional[str]
    guardrail_findings: List[str]
    retry_count: int
    written_paths: List[str]
    # XML configs the transform replaced with Java config (struts-spring6)
    deleted_files: List[str]
    build_verdict: Optional[Literal["PASS", "FAIL", "SKIPPED"]]
    build_output: Optional[str]
    # javac's parse of the transform output, before review (forge/verify/syntax.py)
    syntax_verdict: Optional[Literal["PASS", "FAIL", "SKIPPED"]]
    syntax_errors: List[str]
    transform_model: Optional[str]
    review_model: Optional[str]
    error: Optional[str]
    # A unit the pack creates rather than edits (Liberty server.xml): there is
    # no source to read, so the transform is given the context instead.
    generate: bool
    module_dir: Optional[str]
    # Which extractor fed the prompts, and a digest of the exact block — the
    # audit trail records what the model saw without storing it in state.
    # `context_name` is set whenever the pack declares one, even if no block
    # arrived; `context_missing` is then True. Without that pair, a pack running
    # blind because its extractor is unbuilt is indistinguishable from a pack
    # that declared `context: none`.
    context_name: Optional[str]
    context_digest: Optional[str]
    context_missing: bool
    # HELD: written to .forge-staging/ and waiting for a human, per risk_ceiling.
    held_paths: List[str]
    hold_reason: Optional[str]
    # The human's decision, once made — the audit trail carries it.
    human_decision: Optional[str]
    human_note: Optional[str]
    human_rule: Optional[str]
    human_decided_at: Optional[str]


class ForgeState(TypedDict):
    current_file: FileStatus
    phase: str
    dry_run: bool
    source_dir: str
    output_dir: str
    target_java_version: str
    target_spring_version: str
    files_processed: int
    files_passed: int
    files_retried: int
    files_manual: int
    files_blocked: int
    files_held: int
    bedrock_calls: int
    estimated_cost_usd: float
    messages: List[Any]


def make_file_status(file_path: str, phase: str) -> FileStatus:
    return FileStatus(
        file_path=file_path,
        status="PENDING",
        phase=phase,
        risk_tier="UNSCORED",
        risk_score=0,
        risk_reasons=[],
        transform_output=None,
        transform_malformed=False,
        review_score=None,
        review_verdict=None,
        review_feedback=None,
        guardrail_pre_verdict=None,
        guardrail_post_verdict=None,
        guardrail_findings=[],
        retry_count=0,
        written_paths=[],
        deleted_files=[],
        build_verdict=None,
        build_output=None,
        syntax_verdict=None,
        syntax_errors=[],
        transform_model=None,
        review_model=None,
        error=None,
        generate=False,
        module_dir=None,
        context_name=None,
        context_digest=None,
        context_missing=False,
        held_paths=[],
        hold_reason=None,
        human_decision=None,
        human_note=None,
        human_rule=None,
        human_decided_at=None,
    )
