"""tools/job_queue.py: LocalJobQueue and run_parallel, against fake work —
no scratch checkout, no tools/synos, no real build anywhere in this file.
tests/unit/test_build_matrix.py and tests/unit/test_catalog_conformance.py
cover the real integration (scratch-per-worker isolation) on top of this."""
from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


jq = load_module("job_queue_under_test", "tools/job_queue.py")


class FakeItem:
    def __init__(self, item_id: str):
        self.id = item_id

    def __repr__(self) -> str:
        return f"FakeItem({self.id!r})"


class LocalJobQueueTests(unittest.TestCase):
    def test_take_returns_every_item_once(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(5)]
        q = jq.LocalJobQueue(items)
        taken = []
        while True:
            item = q.take()
            if item is None:
                break
            taken.append(item.id)
            q.record_result(item, {"status": "success"})
        self.assertEqual({"t0", "t1", "t2", "t3", "t4"}, set(taken))

    def test_take_returns_none_once_drained(self) -> None:
        q = jq.LocalJobQueue([])
        self.assertIsNone(q.take())

    def test_take_blocks_while_something_in_flight_might_be_requeued(self) -> None:
        q = jq.LocalJobQueue([FakeItem("only")])
        item = q.take()
        self.assertIsNotNone(item)

        second = {}

        def taker():
            second["result"] = q.take()  # must block until requeue() or record_result()

        thread = threading.Thread(target=taker)
        thread.start()
        time.sleep(0.3)
        self.assertNotIn("result", second, "take() must not return while the only item is still in flight")
        q.requeue(item)
        thread.join(timeout=2)
        self.assertEqual("only", second["result"].id)
        q.record_result(second["result"], {"status": "success"})

    def test_record_result_is_visible_in_results(self) -> None:
        q = jq.LocalJobQueue([FakeItem("a")])
        item = q.take()
        q.record_result(item, {"status": "success", "value": 42})
        self.assertEqual({"a": {"status": "success", "value": 42}}, q.results())

    def test_attempts_counts_mark_running_calls(self) -> None:
        q = jq.LocalJobQueue([FakeItem("a")])
        item = q.take()
        self.assertEqual(0, q.attempts(item))
        q.mark_running(item)
        self.assertEqual(1, q.attempts(item))
        q.requeue(item)
        item2 = q.take()
        q.mark_running(item2)
        self.assertEqual(2, q.attempts(item2))


class RunParallelTests(unittest.TestCase):
    def test_every_item_gets_a_result(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(10)]

        def build_one(item, worker_id):
            return {"id": item.id, "status": "success", "worker": worker_id}

        results = jq.run_parallel(items, 3, build_one)
        self.assertEqual({f"t{i}" for i in range(10)}, set(results))
        self.assertTrue(all(r["status"] == "success" for r in results.values()))

    def test_more_than_one_worker_actually_participates(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(8)]
        barrier = threading.Barrier(4, timeout=5)

        def build_one(item, worker_id):
            try:
                barrier.wait()  # every worker must reach this point before any proceeds: proves real concurrency
            except threading.BrokenBarrierError:
                pass
            return {"id": item.id, "status": "success", "worker": worker_id}

        results = jq.run_parallel(items, 4, build_one)
        workers_used = {r["worker"] for r in results.values()}
        self.assertGreater(len(workers_used), 1, "several fake builds must run on more than one worker")

    def test_single_job_processes_everything_sequentially(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(5)]
        seen_worker_ids = set()

        def build_one(item, worker_id):
            seen_worker_ids.add(worker_id)
            return {"id": item.id, "status": "success"}

        results = jq.run_parallel(items, 1, build_one)
        self.assertEqual({0}, seen_worker_ids)
        self.assertEqual(5, len(results))

    def test_on_result_callback_fires_once_per_item(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(6)]
        seen = []

        def build_one(item, worker_id):
            return {"id": item.id, "status": "success"}

        jq.run_parallel(items, 3, build_one, on_result=lambda item, result: seen.append(item.id))
        self.assertEqual({f"t{i}" for i in range(6)}, set(seen))

    def test_a_crashing_attempt_is_retried_and_the_retry_is_recorded(self) -> None:
        """Simulates a worker that "dies" processing t0 the first time (an
        exception escapes build_one entirely, not an ordinary failed-build
        result): the queue must not lose t0, another attempt must happen,
        and the final, successful attempt must be what is recorded."""
        items = [FakeItem("t0"), FakeItem("t1"), FakeItem("t2")]
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def build_one(item, worker_id):
            with lock:
                attempts[item.id] = attempts.get(item.id, 0) + 1
                count = attempts[item.id]
            if item.id == "t0" and count == 1:
                raise RuntimeError("simulated worker crash")
            return {"id": item.id, "status": "success", "attempt": count}

        results = jq.run_parallel(items, 2, build_one)
        self.assertEqual({"t0", "t1", "t2"}, set(results))
        self.assertEqual("success", results["t0"]["status"], "the retried attempt must be recorded, not lost")
        self.assertEqual(2, results["t0"]["attempt"], "the second attempt is the one that succeeded")
        self.assertGreaterEqual(attempts["t0"], 2, "a crash must trigger a retry")

    def test_an_attempt_that_always_crashes_is_recorded_as_error_after_max_attempts(self) -> None:
        items = [FakeItem("always-crashes")]

        def build_one(item, worker_id):
            raise RuntimeError("this target can never be built")

        results = jq.run_parallel(items, 1, build_one)
        self.assertEqual("error", results["always-crashes"]["status"])
        self.assertIn("crashed", results["always-crashes"]["log_tail"][0])

    def test_no_target_is_lost_when_several_workers_crash_at_once(self) -> None:
        items = [FakeItem(f"t{i}") for i in range(6)]
        attempts: dict[str, int] = {}
        lock = threading.Lock()

        def build_one(item, worker_id):
            with lock:
                attempts[item.id] = attempts.get(item.id, 0) + 1
                count = attempts[item.id]
            if count == 1:
                raise RuntimeError("simulated crash")
            return {"id": item.id, "status": "success"}

        results = jq.run_parallel(items, 3, build_one)
        self.assertEqual({f"t{i}" for i in range(6)}, set(results))
        self.assertTrue(all(r["status"] == "success" for r in results.values()),
                        "every target must recover on its retry, none left unrecorded")

    def test_zero_items_returns_empty_results(self) -> None:
        results = jq.run_parallel([], 4, lambda item, worker_id: {"status": "success"})
        self.assertEqual({}, results)


if __name__ == "__main__":
    unittest.main()
