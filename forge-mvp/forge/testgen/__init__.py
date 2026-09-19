"""Test generation: JUnit 5 tests for the code the migration just produced.

The migration's own gates answer "did it transform correctly?" and "does it
compile?". Neither answers "does it still do what it did", and nothing in a
legacy J2EE codebase is less likely to exist than the tests that would. This
package writes them — deterministically choosing the classes, deterministically
choosing the destination, and asking a model for exactly one thing: the test.
"""

from forge.testgen.report import RECORD_NAME, REPORT_NAME, build_record, render_report, write_record, write_report
from forge.testgen.settings import RunSettings, TestGenSettings
from forge.testgen.state import TestGenState, TestUnit, make_test_unit
from forge.testgen.targets import (
    KINDS, SkippedTarget, TargetScan, TestTarget, classify, read_surface, scan_test_targets,
    target_from_unit, test_rel_path_for,
)

__all__ = [
    "KINDS", "RECORD_NAME", "REPORT_NAME", "RunSettings", "SkippedTarget", "TargetScan", "TestGenSettings",
    "TestGenState", "TestTarget", "TestUnit", "build_record", "classify", "make_test_unit", "read_surface",
    "render_report", "scan_test_targets", "target_from_unit", "test_rel_path_for", "write_record", "write_report",
]
