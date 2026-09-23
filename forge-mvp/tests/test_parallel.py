"""Several files of one pack in flight at once (``max_parallel_files``).

What must hold however the files interleave: they really do overlap, the
run's record comes out in scan order, the totals add up, a cancel starts
nothing new, and the progress counter a person reads still counts 1..n.
"""

import threading
import time
from unittest.mock import patch

import pytest

from forge import service
from forge.config import parallel_files
from tests.conftest import MIGRATED_JAVA, llm_reply, mocked_aws, write_config

N = 6


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "proj/src/main/java/com/corp"
    base.mkdir(parents=True)
    for i in range(N):
        (base / f"F{i}.java").write_text(
            f"package com.corp;\nimport javax.servlet.Filter;\npublic class F{i} {{}}\n", encoding="utf-8")
    return tmp_path / "proj"


class _Transform:
    """The transform mock: counts calls, measures overlap, optionally fires a cancel."""

    def __init__(self, delay=0.05, cancel=None):
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0
        self.delay = delay
        self.cancel = cancel

    def __call__(self, messages):
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            if self.cancel is not None:
                self.cancel.set()
        time.sleep(self.delay)
        with self.lock:
            self.in_flight -= 1
        path = messages[1].content.split("File path: ", 1)[1].splitlines()[0]
        return llm_reply({"files": {path: MIGRATED_JAVA}, "manual_flags": []})


def _run(tmp_path, project, transform, workers, cancel=None):
    events = []
    cfg = write_config(tmp_path, max_parallel_files=workers)
    with mocked_aws() as mocks, \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.put_file_status"), \
         patch("forge.state_store.dynamodb.DynamoDBStateManager.mark_pending"):
        mocks["upgrade"].return_value.invoke.side_effect = transform
        result = service.run_migration(str(project), "javax-to-jakarta", str(tmp_path / "out"), cfg,
                                       on_event=events.append, cancel=cancel)
    return result, events


def test_files_overlap_and_the_record_stays_in_scan_order(tmp_path, project):
    transform = _Transform()
    result, events = _run(tmp_path, project, transform, workers=4)

    assert transform.peak >= 2, "with 4 workers the transforms must overlap"
    assert transform.peak <= 4, "and never exceed the worker count"
    names = [s["file_path"].rsplit("/", 1)[1] for s in result.statuses]
    assert names == [f"F{i}.java" for i in range(N)]
    assert result.totals["passed"] == N
    assert result.totals["bedrock_calls"] == 3 * N    # transform + review + post-check, per file
    # The progress counter counts finished files 1..n, whatever order they finished in.
    assert [e["index"] for e in events if e["type"] == "file"] == list(range(1, N + 1))
    assert [e["type"] for e in events][0] == "start" and events[-1]["type"] == "summary"


def test_one_worker_is_strictly_sequential(tmp_path, project):
    transform = _Transform(delay=0.01)
    result, _ = _run(tmp_path, project, transform, workers=1)
    assert transform.peak == 1
    assert result.totals["passed"] == N


def test_cancel_starts_nothing_new_and_keeps_what_finished(tmp_path, project):
    cancel = threading.Event()
    transform = _Transform(cancel=cancel)      # the first transform asks to stop
    result, events = _run(tmp_path, project, transform, workers=2, cancel=cancel)

    done = result.totals["total"]
    assert 1 <= done < N, "units already in flight finish; nothing starts after the cancel"
    assert transform.calls == done, "every unit that started is in the record"
    assert result.cancelled
    cancelled = [e for e in events if e["type"] == "cancelled"]
    assert cancelled == [{"type": "cancelled", "done": done, "total": N}]


@pytest.mark.parametrize("raw, workers", [(None, 1), (8, 8), ("4", 4), (0, 1), (-3, 1), (500, 32), ("x", 1)])
def test_parallel_files_setting_is_clamped(tmp_path, raw, workers):
    cfg = write_config(tmp_path, **({} if raw is None else {"max_parallel_files": raw}))
    assert parallel_files(cfg) == workers


def test_maven_build_verification_runs_one_file_at_a_time(tmp_path):
    """mvn compiles the whole output tree, so parallel files would see each other half-written."""
    maven = write_config(tmp_path, max_parallel_files=8,
                         build_verification={"enabled": True, "mode": "maven"})
    javac = write_config(tmp_path, max_parallel_files=8,
                         build_verification={"enabled": True, "mode": "javac"})
    assert service._workers_for(maven) == 1
    assert service._workers_for(javac) == 8
