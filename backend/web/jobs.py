"""Background fetch jobs: the panel asks for a code, a thread runs the same build chain the CLI runs.

A build hits eight network sources and takes tens of seconds, so it cannot hold an HTTP request open;
the browser instead starts a job and polls it for a step name. One build per code at a time because two
concurrent builds of the same ticker each create a runs/<code>/<timestamp> batch and the panel would read
whichever landed last. A second request joins the build already in flight only when it asks for *that*
build: the join compares the whole BuildOptions, since a request for another benchmark — or a replay, while
a live build is running — would otherwise be answered with a batch built to a different question.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

from backend import builder

KEEP_FINISHED = 50

# Tests swap this out; the panel never sets it.
run_build: Callable[..., Any] = builder.build

_jobs: dict[str, dict] = {}
_inflight: dict[str, tuple[str, builder.BuildOptions]] = {}  # code -> (job_id, 那一个作业在建什么)
_code_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _asked(options: builder.BuildOptions) -> str:
    return f"复现 {options.from_run}" if options.from_run else f"取数（基准 {options.benchmark}）"


def start(code: str, options: builder.BuildOptions) -> str:
    """Queue a build and return its job id — or the id of the identical build already running for this code."""
    with _guard:
        existing = _inflight.get(code)
        if existing:
            job_id, running = existing
            if running == options:
                return job_id
            step = _jobs.get(job_id, {}).get("step", "")
            raise ValueError(
                f"{code} 已有一个作业在跑：{_asked(running)}，当前「{step}」；这次要的是 {_asked(options)}。"
                f"同一只票同时只跑一个作业，参数不同的请求不并进去——等它跑完再按。"
            )
        job_id = uuid.uuid4().hex[:12]
        _jobs[job_id] = {
            "job_id": job_id,
            "code": code,
            "state": "queued",
            "step": "排队中",
            "run_id": "",
            "error": "",
            "elapsed_ms": None,
        }
        _inflight[code] = (job_id, options)
        lock = _code_locks.setdefault(code, threading.Lock())
        _prune()
    threading.Thread(target=_run, args=(job_id, code, options, lock), daemon=True).start()
    return job_id


def get(job_id: str) -> dict:
    with _guard:
        job = _jobs.get(job_id)
        if job is None:
            raise ValueError(f"未知取数作业 {job_id!r}，服务可能已经重启")
        return dict(job)


def _prune() -> None:
    finished = [key for key, job in _jobs.items() if job["state"] not in ("queued", "running")]
    for key in finished[: max(0, len(finished) - KEEP_FINISHED)]:
        _jobs.pop(key)


def _record(job_id: str, **changes: Any) -> None:
    with _guard:
        _jobs.get(job_id, {}).update(changes)


def _run(job_id: str, code: str, options: builder.BuildOptions, lock: threading.Lock) -> None:
    started = time.monotonic()
    with lock:  # serialise per code; a waiting thread keeps its 'queued' step until here
        _record(job_id, state="running", step="取数中")
        try:
            result = run_build(options, progress=lambda step: _record(job_id, step=step))
        except Exception as exc:  # noqa: BLE001 - a failed build must surface as a panel message, not a dead thread
            outcome = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
        else:
            outcome = {"state": "done", "step": "基线已写出", "run_id": result.run_id}
        outcome["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        with _guard:
            # Terminal state and the inflight slot flip together, so no poller can see 'done' while the
            # code still looks busy to the next request.
            _jobs.get(job_id, {}).update(outcome)
            if _inflight.get(code, (None, None))[0] == job_id:
                _inflight.pop(code)
