from typing import TypedDict, Optional, List, Literal, Any


class FileStatus(TypedDict):
    file_path: str
    status: Literal["PENDING", "TRANSFORMING", "REVIEWING", "RETRY_1", "RETRY_2", "DONE", "MANUAL_REVIEW", "BLOCKED"]
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
    transform_model: Optional[str]
    review_model: Optional[str]
    error: Optional[str]


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
        transform_model=None,
        review_model=None,
        error=None,
    )
