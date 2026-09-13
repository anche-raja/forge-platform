from typing import TypedDict, Optional, List, Literal, Any


class FileStatus(TypedDict):
    file_path: str
    status: Literal["PENDING", "TRANSFORMING", "REVIEWING", "VERIFYING", "RETRY_1", "RETRY_2", "DONE", "MANUAL_REVIEW", "BLOCKED"]
    phase: str
    risk_tier: str
    risk_score: int
    transform_output: Optional[dict]
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
    transform_model: Optional[str]
    review_model: Optional[str]
    error: Optional[str]
    # A unit the pack creates rather than edits (Liberty server.xml): there is
    # no source to read, so the transform is given the context instead.
    generate: bool
    module_dir: Optional[str]
    # Which extractor fed the prompts, and a digest of the exact block — the
    # audit trail records what the model saw without storing it in state.
    context_name: Optional[str]
    context_digest: Optional[str]


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
        transform_output=None,
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
        transform_model=None,
        review_model=None,
        error=None,
        generate=False,
        module_dir=None,
        context_name=None,
        context_digest=None,
    )
