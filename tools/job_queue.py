#!/usr/bin/env python3
"""job_queue — where a build runner gets its next target and reports what
happened to it, and the worker pool that drives several of them at once.

The queue seam (why it is a small interface, not a bare queue.Queue): a
runner only ever calls `take()`, `mark_running()`, `record_result()` and
`requeue()` on whatever it was given. `LocalJobQueue` below is the only
implementation today — in-process, in-memory, gone when the run ends — but
nothing in tools/build_matrix.py or tools/catalog_conformance.py depends on
that: a later hosted build service (docs/BUILD_MATRIX.md, "What a hosted
build service would still need") can hand out targets from a real queue
across machines by implementing the same four methods against an HTTP
service instead — take() leases the next target from the service, mark_
running() renews the lease, record_result() posts the outcome and releases
it, requeue() abandons the lease early — without changing a line of
run_parallel or either caller. Nothing here builds that service; this is
the seam it would plug into.

run_parallel gives each worker its own scratch checkout
(tools/scratch_checkout.py), created once and reused for every target that
worker processes — this is that worker's own build cache (apt lists,
downloaded packages, the parts of .build/ that survive between builds), not
shared with any other worker, and is why concurrent builds do not collide
the way two `./build.sh` runs on one bundle once did (a shared cache volume
and no per-run isolation; docs/BUILD_MATRIX.md). A worker whose build_one
call raises outright (a crash, not an ordinary failed-build result, which
build_one always turns into a normal result dict) does not wedge the run:
the target is requeued once for another worker to attempt, and only
recorded as failed if the retry also raises.
"""
from __future__ import annotations

import queue as _queue
import threading
from typing import Callable, Protocol

MAX_ATTEMPTS_PER_TARGET = 2  # one retry when a worker's attempt raises outright, not when a build merely fails


class JobQueue(Protocol):
    def take(self): ...                          # the next item, or None once nothing is pending or in flight
    def mark_running(self, item) -> None: ...     # called once an attempt on `item` has started
    def record_result(self, item, result: dict) -> None: ...
    def requeue(self, item) -> None: ...          # put `item` back after an attempt that never produced a result
    def attempts(self, item) -> int: ...
    def results(self) -> dict: ...


class LocalJobQueue:
    """The only implementation today: an in-process list guarded by a
    condition variable, plus a results dict. take() blocks briefly while
    something is in flight (it might be requeued) and returns None only
    once the queue is truly drained: nothing pending and nothing running."""

    def __init__(self, items):
        self._condition = threading.Condition()
        self._pending: list = list(items)
        self._in_flight = 0
        self._results: dict = {}
        self._attempts: dict = {}

    def take(self):
        with self._condition:
            while True:
                if self._pending:
                    item = self._pending.pop(0)
                    self._in_flight += 1
                    return item
                if self._in_flight == 0:
                    return None
                self._condition.wait(timeout=0.2)

    def mark_running(self, item) -> None:
        with self._condition:
            self._attempts[item.id] = self._attempts.get(item.id, 0) + 1

    def record_result(self, item, result: dict) -> None:
        with self._condition:
            self._results[item.id] = result
            self._in_flight -= 1
            self._condition.notify_all()

    def requeue(self, item) -> None:
        with self._condition:
            self._pending.append(item)
            self._in_flight -= 1
            self._condition.notify_all()

    def attempts(self, item) -> int:
        with self._condition:
            return self._attempts.get(item.id, 0)

    def results(self) -> dict:
        with self._condition:
            return dict(self._results)


def _worker(job_queue: JobQueue, worker_id: int, build_one: Callable, on_result: Callable[[object, dict], None] | None) -> None:
    while True:
        item = job_queue.take()
        if item is None:
            return
        job_queue.mark_running(item)
        try:
            result = build_one(item, worker_id)
        except Exception as exc:  # noqa: BLE001 - a worker dying must not wedge the queue
            if job_queue.attempts(item) < MAX_ATTEMPTS_PER_TARGET:
                job_queue.requeue(item)
                continue
            result = {"id": getattr(item, "id", str(item)), "status": "error",
                      "log_tail": [f"worker {worker_id} crashed on attempt {job_queue.attempts(item)}: {type(exc).__name__}: {exc}"]}
        job_queue.record_result(item, result)
        if on_result is not None:
            on_result(item, result)


def run_parallel(items, jobs: int, build_one: Callable, *, on_result: Callable[[object, dict], None] | None = None) -> dict:
    """Runs `build_one(item, worker_id)` over `items` with `jobs` worker
    threads sharing a LocalJobQueue, and returns {item.id: result}.
    `build_one` is responsible for its own worker-scoped resources (a
    scratch checkout, in both current callers) using `worker_id` to name
    them; run_parallel only schedules, it does not know what a target is."""
    job_queue = LocalJobQueue(items)
    threads = [threading.Thread(target=_worker, args=(job_queue, worker_id, build_one, on_result), daemon=True)
              for worker_id in range(max(jobs, 1))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return job_queue.results()
