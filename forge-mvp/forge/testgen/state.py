"""State for one test-generation unit.

Deliberately its own type rather than a reuse of ``FileStatus``: a migration
unit and a test unit answer different questions, and half of ``FileStatus``
(risk tier, hold reason, deleted files, human decision) has no meaning here.
The two share nothing but the shape of the idea — a dict the graph threads
through its nodes and returns.
"""

from typing import Any, List, Literal, Optional, TypedDict

# GENERATED  the test was written into the output tree
# HELD       the test exists but is staged, not written — a human decides
# BLOCKED    nothing was sent to a model (secret gate, unreadable, too large)
# SKIPPED    nothing to do (an existing test, a type with no public API)
TestStatus = Literal["PENDING", "GENERATING", "REVIEWING", "GENERATED", "HELD", "BLOCKED", "SKIPPED"]


class TestUnit(TypedDict):
    """One class under test, and everything the run learned about it."""

    # The migrated class this tests, as an absolute path in the output tree.
    file_path: str
    rel_path: str
    package: str
    type_name: str
    # controller | service | repository | entity | config | plain — decided by
    # forge/testgen/targets.py from annotations and naming, never by a model.
    kind: str
    test_class: str
    # src/test/java/<pkg>/<Type>Test.java — this pipeline's, not the model's.
    test_rel_path: str
    status: TestStatus
    style: str
    test_output: Optional[dict]
    review_score: Optional[int]
    review_verdict: Optional[Literal["PASS", "RETRY", "MANUAL"]]
    review_feedback: Optional[str]
    # Mechanical failures found in code — JUnit 4 imports, javax.*, no @Test.
    check_failures: List[str]
    # Kinds and line numbers from the local secret scan. Never the bytes.
    scan_findings: List[str]
    retry_count: int
    written_paths: List[str]
    held_paths: List[str]
    hold_reason: Optional[str]
    test_verdict: Optional[Literal["PASS", "FAIL", "SKIPPED"]]
    test_output_log: Optional[str]
    cases: List[dict]
    untested: List[dict]
    dependencies: List[str]
    notes: List[str]
    generate_model: Optional[str]
    review_model: Optional[str]
    error: Optional[str]


class TestGenState(TypedDict):
    current_unit: TestUnit
    source_dir: str
    output_dir: str
    dry_run: bool
    units_processed: int
    units_generated: int
    units_held: int
    units_blocked: int
    bedrock_calls: int
    estimated_cost_usd: float
    messages: List[Any]


def make_test_unit(*, file_path: str, rel_path: str, package: str, type_name: str, kind: str,
                   test_rel_path: str, style: str = "junit5") -> TestUnit:
    return TestUnit(
        file_path=file_path,
        rel_path=rel_path,
        package=package,
        type_name=type_name,
        kind=kind,
        test_class=f"{type_name}Test",
        test_rel_path=test_rel_path,
        status="PENDING",
        style=style,
        test_output=None,
        review_score=None,
        review_verdict=None,
        review_feedback=None,
        check_failures=[],
        scan_findings=[],
        retry_count=0,
        written_paths=[],
        held_paths=[],
        hold_reason=None,
        test_verdict=None,
        test_output_log=None,
        cases=[],
        untested=[],
        dependencies=[],
        notes=[],
        generate_model=None,
        review_model=None,
        error=None,
    )
