"""The guard in conftest.py that keeps the suite out of forge-mvp/migrated.

Every check goes through ``sys.audit``, which raises the event a real write
raises without performing one — so a broken guard fails here and still never
touches the owner's files.
"""

import os
import sys
from pathlib import Path

import pytest

from tests.conftest import REAL_OUTPUT_DIR, _real_output_writes

_WRITE = os.O_WRONLY | os.O_CREAT | os.O_TRUNC


def test_a_write_into_the_real_output_directory_is_refused_and_pinned_on_the_test(monkeypatch):
    # forge-mvp, where `pytest` runs and where the default `./migrated` resolves.
    monkeypatch.chdir(Path(REAL_OUTPUT_DIR).parent)
    before = len(_real_output_writes)

    with pytest.raises(PermissionError):
        sys.audit("open", "./migrated/forge-profile.yaml", "w", _WRITE)
    with pytest.raises(PermissionError):
        sys.audit("os.rename", os.path.join(REAL_OUTPUT_DIR, "decisions-applied.jsonl"), "/elsewhere", -1, -1)

    assert len(_real_output_writes) - before == 2, "the autouse check fails whichever test these came from"
    del _real_output_writes[before:]    # expected here, so this test's own teardown check stays quiet


def test_reading_a_real_run_and_writing_anywhere_else_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(Path(REAL_OUTPUT_DIR).parent)
    sys.audit("open", os.path.join(REAL_OUTPUT_DIR, "migration-report.md"), "r", os.O_RDONLY)
    sys.audit("open", str(tmp_path / "migrated" / "forge-profile.yaml"), "w", _WRITE)
    # shutil.rmtree walks by directory fd, so a tmp dir's own `migrated` child
    # arrives named relative to that fd — not to the cwd, which is forge-mvp.
    sys.audit("os.rmdir", "migrated", 7)
