"""Background jobs for the UI.

A migration run blocks for minutes inside ``app.invoke``; the browser needs
progress in the meantime and a way to stop it. Each job runs on its own daemon
thread and reports through the same ``on_event`` callback the CLI prints from.
Events are kept in order with a sequence number so a page that reloads, or a
connection that drops, can pick up exactly where it left off.

One job at a time. The extract cache is process-global and cleared per run,
and boto3 resources are not thread-safe: two concurrent runs would corrupt
each other's context. ``start`` refuses with ``JobBusy`` while one is running.
"""

import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

FINISHED = ("done", "failed", "cancelled")


class JobBusy(Exception):
    """Another job is still running."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str                       # "run" | "apply"
    params: dict
    state: str = "queued"           # queued | running | done | failed | cancelled
    events: List[dict] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    def to_json(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "state": self.state, "params": self.params,
            "error": self.error, "result": self.result,
            "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
            "events": len(self.events), "cancel_requested": self.cancel.is_set(),
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._cond = threading.Condition()

    # ─── lifecycle ────────────────────────────────────────────────────────────

    def start(self, kind: str, params: dict, target: Callable[[Job, Callable[[dict], None]], Any],
              *, summarise: Callable[[Any], Any] = lambda r: r) -> Job:
        """Run ``target(job, emit)`` on a thread. ``summarise`` turns its return value into JSON."""
        with self._cond:
            active = self._active_locked()
            if active is not None:
                raise JobBusy(f"job {active.id} ({active.kind}) is still {active.state}")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params)
            self._jobs[job.id] = job
            self._order.append(job.id)
            job.state = "running"
            job.started_at = _now()
        thread = threading.Thread(target=self._run, args=(job, target, summarise), name=f"forge-{kind}-{job.id}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, target, summarise) -> None:
        def emit(event: dict) -> None:
            self.emit(job, event)

        try:
            result = target(job, emit)
            summary = summarise(result)
            state = "cancelled" if (job.cancel.is_set() or bool(getattr(result, "cancelled", False))) else "done"
            with self._cond:
                job.result = summary
            self.emit(job, {"type": "done", "state": state, "result": summary})
            self._finish(job, state)
        except Exception as e:      # the job must always reach a terminal state
            detail = f"{type(e).__name__}: {e}"
            with self._cond:
                job.error = detail
            self.emit(job, {"type": "error", "error": detail, "traceback": traceback.format_exc()})
            self._finish(job, "failed")

    def _finish(self, job: Job, state: str) -> None:
        with self._cond:
            job.state = state
            job.finished_at = _now()
            self._cond.notify_all()

    def cancel(self, job: Job) -> None:
        job.cancel.set()
        with self._cond:
            self._cond.notify_all()

    # ─── events ───────────────────────────────────────────────────────────────

    def emit(self, job: Job, event: dict) -> None:
        with self._cond:
            job.events.append({**event, "seq": len(job.events) + 1})
            self._cond.notify_all()

    def subscribe(self, job: Job, after: int = 0, timeout: Optional[float] = None) -> Iterator[Optional[Tuple[int, dict]]]:
        """Yield ``(seq, event)`` from ``after`` onward, tailing until the job finishes.

        Yields ``None`` whenever ``timeout`` seconds pass with nothing new — the
        SSE route turns that into a keep-alive comment.
        """
        cursor = after
        while True:
            with self._cond:
                if cursor < len(job.events):
                    event = job.events[cursor]
                    cursor += 1
                    pending = True
                elif job.finished:
                    return
                else:
                    pending = False
                    self._cond.wait(timeout)
            if pending:
                yield event["seq"], event
            elif not job.finished and cursor >= len(job.events):
                yield None

    # ─── lookup ───────────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        return [self._jobs[i] for i in reversed(self._order)]

    def _active_locked(self) -> Optional[Job]:
        for job_id in reversed(self._order):
            job = self._jobs[job_id]
            if not job.finished:
                return job
        return None

    def active(self) -> Optional[Job]:
        with self._cond:
            return self._active_locked()

    def wait(self, job: Job, timeout: Optional[float] = 30) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: job.finished, timeout=timeout)
