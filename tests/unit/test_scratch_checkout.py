"""tools/scratch_checkout.py: the disposable git worktree tools/build_matrix.py
and tools/catalog_conformance.py both build inside, so applying a bundle's
files never touches this checkout. Every test here uses the real `git`
binary against this actual repository (worktree creation/removal, not a
build), and every test restores this checkout's own git status and worktree
list to what they were before it ran."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sc = load_module("scratch_checkout_under_test", "tools/scratch_checkout.py")


def _git_status(cwd: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _git_worktree_list(cwd: Path) -> str:
    return subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


class CreateRemoveTests(unittest.TestCase):
    def test_create_checks_out_the_current_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="t")
            try:
                self.assertTrue((path / "tools" / "synos").is_file())
                self.assertTrue((path / "bundle-catalog" / "web-server-nginx").is_dir())
                head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True).stdout.strip()
                root_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(root_head, head)
            finally:
                sc.remove(path)

    def test_create_does_not_dirty_the_source_checkout(self) -> None:
        before = _git_status(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="t")
            sc.remove(path)
        self.assertEqual(before, _git_status(ROOT))

    def test_remove_deletes_the_directory_and_the_worktree_registration(self) -> None:
        before_worktrees = _git_worktree_list(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="t")
            self.assertTrue(path.is_dir())
            sc.remove(path)
            self.assertFalse(path.exists())
        self.assertEqual(before_worktrees, _git_worktree_list(ROOT))

    def test_remove_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="t")
            sc.remove(path)
            sc.remove(path)  # must not raise

    def test_remove_tolerates_a_path_that_was_never_created_here(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            phantom = Path(tmp) / "never-existed"
            sc.remove(phantom)  # must not raise

    def test_labels_are_sanitized_into_a_safe_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="../../etc/passwd; rm -rf /")
            try:
                self.assertTrue(path.is_relative_to(Path(tmp)))
                self.assertNotIn("..", path.parts)
            finally:
                sc.remove(path)

    def test_two_scratch_checkouts_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = sc.create(ROOT, Path(tmp), label="a")
            b = sc.create(ROOT, Path(tmp), label="b")
            try:
                self.assertNotEqual(a, b)
                (a / "manifests" / "scratch-a-marker.yml").write_text("marker\n", encoding="utf-8")
                self.assertFalse((b / "manifests" / "scratch-a-marker.yml").exists())
            finally:
                sc.remove(a)
                sc.remove(b)


class ContextManagerTests(unittest.TestCase):
    def test_scratch_removes_on_normal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with sc.scratch(ROOT, Path(tmp), label="ctx") as path:
                self.assertTrue(path.is_dir())
            self.assertFalse(path.exists())

    def test_scratch_removes_even_when_the_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured_path = None
            with self.assertRaises(RuntimeError):
                with sc.scratch(ROOT, Path(tmp), label="ctx-raise") as path:
                    captured_path = path
                    raise RuntimeError("boom")
            self.assertIsNotNone(captured_path)
            self.assertFalse(captured_path.exists())


class RegistrationTests(unittest.TestCase):
    def test_active_registry_is_empty_once_every_checkout_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = sc.create(ROOT, Path(tmp), label="t")
            self.assertIn(path, sc._ACTIVE)
            sc.remove(path)
            self.assertNotIn(path, sc._ACTIVE)


if __name__ == "__main__":
    unittest.main()
