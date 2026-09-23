"""tools/host_resources.py: the --jobs auto formula. Every test pins the
machine's numbers via derive_safe_jobs's keyword overrides, so none of
these depend on the disk, memory or CPU count of whatever machine runs
them."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


hr = load_module("host_resources_under_test", "tools/host_resources.py")


class DeriveSafeJobsTests(unittest.TestCase):
    def test_disk_binds_when_it_is_the_smallest_factor(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=120.0, free_store_gb=None,
                                            memory_gb=64.0, cpu_count=32)
        # 120 GB / 40 GB per build = 3; memory 64/4=16; cpu 32/2=16 -> disk binds
        self.assertEqual(3, safe)
        self.assertEqual(3, factors["disk_jobs"])

    def test_memory_binds_when_it_is_the_smallest_factor(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=4000.0, free_store_gb=None,
                                            memory_gb=8.0, cpu_count=64)
        # disk 4000/40=100; memory 8/4=2; cpu 64/2=32 -> memory binds
        self.assertEqual(2, safe)
        self.assertEqual(2, factors["memory_jobs"])

    def test_cpu_binds_when_it_is_the_smallest_factor(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=4000.0, free_store_gb=None,
                                            memory_gb=256.0, cpu_count=2)
        # disk 100; memory 64; cpu 2/2=1 -> cpu binds
        self.assertEqual(1, safe)
        self.assertEqual(1, factors["cpu_jobs"])

    def test_container_store_can_bind_tighter_than_the_checkout_disk(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=5000.0, free_store_gb=65.0,
                                            memory_gb=64.0, cpu_count=32)
        # checkout disk: 5000/40=125; store: 65/30=2 -> store binds
        self.assertEqual(2, safe)
        self.assertEqual(2, factors["disk_store_jobs"])

    def test_unknown_memory_falls_back_to_the_disk_figure_not_zero(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=120.0, free_store_gb=None,
                                            memory_gb=None, cpu_count=64)
        self.assertIsNone(factors["memory_gb"])
        self.assertEqual(3, factors["memory_jobs"])  # same as disk_jobs, not 0
        self.assertEqual(3, safe)

    def test_a_machine_with_less_than_one_builds_worth_of_disk_derives_zero(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=1.0, free_store_gb=None,
                                            memory_gb=64.0, cpu_count=32)
        self.assertEqual(0, safe)
        self.assertEqual(0, factors["disk_jobs"])

    def test_cpu_factor_is_floored_at_one_even_with_a_single_core(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=4000.0, free_store_gb=None,
                                            memory_gb=64.0, cpu_count=1)
        self.assertEqual(1, factors["cpu_jobs"])

    def test_custom_thresholds_are_honored(self) -> None:
        safe, factors = hr.derive_safe_jobs(ROOT, min_free_gb=10.0, min_memory_gb_per_job=1.0, cpus_per_job=1,
                                            free_root_gb=100.0, free_store_gb=None, memory_gb=8.0, cpu_count=8)
        # disk 100/10=10; memory 8/1=8; cpu 8/1=8 -> memory/cpu tie at 8
        self.assertEqual(8, safe)


class ResolveJobsTests(unittest.TestCase):
    def _kwargs(self):
        return dict(free_root_gb=400.0, free_store_gb=None, memory_gb=64.0, cpu_count=32)  # safe = min(10, 16, 16) = 10

    def test_auto_returns_the_derived_safe_count_with_no_problems(self) -> None:
        jobs, problems = hr.resolve_jobs("auto", ROOT, **self._kwargs())
        self.assertEqual(10, jobs)
        self.assertEqual([], problems)

    def test_an_explicit_count_at_or_below_the_safe_maximum_is_accepted(self) -> None:
        jobs, problems = hr.resolve_jobs("10", ROOT, **self._kwargs())
        self.assertEqual(10, jobs)
        self.assertEqual([], problems)

    def test_an_explicit_count_above_the_safe_maximum_is_refused_with_the_numbers(self) -> None:
        jobs, problems = hr.resolve_jobs("11", ROOT, **self._kwargs())
        self.assertTrue(problems)
        text = problems[0]
        self.assertIn("11", text)
        self.assertIn("10", text)
        self.assertIn("disk allows", text)
        self.assertIn("memory allows", text)
        self.assertIn("CPUs allow", text)
        self.assertIn("--jobs auto", text)

    def test_zero_is_refused(self) -> None:
        jobs, problems = hr.resolve_jobs("0", ROOT, **self._kwargs())
        self.assertTrue(problems)

    def test_negative_is_refused(self) -> None:
        jobs, problems = hr.resolve_jobs("-3", ROOT, **self._kwargs())
        self.assertTrue(problems)

    def test_garbage_is_refused_cleanly(self) -> None:
        jobs, problems = hr.resolve_jobs("banana", ROOT, **self._kwargs())
        self.assertTrue(problems)
        self.assertIn("banana", problems[0])

    def test_a_machine_that_cannot_feed_one_build_is_refused_even_for_one(self) -> None:
        jobs, problems = hr.resolve_jobs("1", ROOT, free_root_gb=1.0, free_store_gb=None, memory_gb=64.0, cpu_count=32)
        self.assertEqual(0, jobs)
        self.assertTrue(problems)

    def test_a_machine_that_cannot_feed_one_build_refuses_auto_too(self) -> None:
        jobs, problems = hr.resolve_jobs("auto", ROOT, free_root_gb=1.0, free_store_gb=None, memory_gb=64.0, cpu_count=32)
        self.assertEqual(0, jobs)
        self.assertTrue(problems)


class ExplainTests(unittest.TestCase):
    def test_mentions_every_factor(self) -> None:
        _safe, factors = hr.derive_safe_jobs(ROOT, free_root_gb=400.0, free_store_gb=90.0, memory_gb=64.0, cpu_count=32)
        text = hr.explain(factors)
        self.assertIn("disk allows", text)
        self.assertIn("store", text)
        self.assertIn("memory allows", text)
        self.assertIn("CPUs allow", text)


if __name__ == "__main__":
    unittest.main()
