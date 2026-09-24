"""tools/entry_cleanup.py: the small, heavily-guarded primitives that free
a build entry's heavy directories. Every test here only ever touches a
temporary directory it made itself - nothing real is ever deleted."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import entry_cleanup as ec  # noqa: E402


class GuardWorkdirTests(unittest.TestCase):
    def test_an_ordinary_dedicated_directory_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ec.guard_workdir(Path(tmp) / "conformance-work")  # need not exist yet

    def test_the_filesystem_root_is_refused(self) -> None:
        with self.assertRaises(ec.CleanupError):
            ec.guard_workdir(Path("/"))

    def test_the_users_home_directory_is_refused(self) -> None:
        with self.assertRaises(ec.CleanupError):
            ec.guard_workdir(Path.home())

    def test_a_directory_with_a_dot_git_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "some-checkout"
            (checkout / ".git").mkdir(parents=True)
            with self.assertRaises(ec.CleanupError):
                ec.guard_workdir(checkout)

    def test_a_directory_with_a_dot_hg_or_dot_svn_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for marker in (".hg", ".svn"):
                checkout = Path(tmp) / f"checkout-{marker.strip('.')}"
                (checkout / marker).mkdir(parents=True)
                with self.assertRaises(ec.CleanupError):
                    ec.guard_workdir(checkout)

    def test_a_subdirectory_of_a_checkout_is_fine_the_checkout_itself_is_the_problem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "some-checkout"
            (checkout / ".git").mkdir(parents=True)
            work = checkout / "conformance-work"
            work.mkdir()
            ec.guard_workdir(work)  # no .git directly inside *this* directory


class SafeRemoveTests(unittest.TestCase):
    def test_removes_a_file_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a" / "b.txt"
            target.parent.mkdir(parents=True)
            target.write_text("x", encoding="utf-8")
            ec.safe_remove(root, target)
            self.assertFalse(target.exists())

    def test_removes_a_directory_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "bundle"
            (target / "dist").mkdir(parents=True)
            (target / "dist" / "x.iso").write_bytes(b"fake")
            ec.safe_remove(root, target)
            self.assertFalse(target.exists())

    def test_nonexistent_target_is_a_silent_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ec.safe_remove(root, root / "does-not-exist")  # must not raise

    def test_refuses_a_target_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_other:
            root = Path(tmp_root)
            outside = Path(tmp_other) / "victim.txt"
            outside.write_text("do not delete me", encoding="utf-8")
            with self.assertRaises(ec.CleanupError):
                ec.safe_remove(root, outside)
            self.assertTrue(outside.exists())

    def test_refuses_when_an_intermediate_path_component_is_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_other:
            root = Path(tmp_root)
            victim_dir = Path(tmp_other) / "victim"
            victim_dir.mkdir()
            (victim_dir / "important.txt").write_text("do not delete me", encoding="utf-8")
            root.mkdir(exist_ok=True)
            (root / "bundle").symlink_to(victim_dir, target_is_directory=True)
            with self.assertRaises(ec.CleanupError):
                ec.safe_remove(root, root / "bundle" / "important.txt")
            self.assertTrue((victim_dir / "important.txt").exists())

    def test_refuses_when_the_target_itself_is_a_symlink_out_of_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_other:
            root = Path(tmp_root)
            victim = Path(tmp_other) / "victim.txt"
            victim.write_text("do not delete me", encoding="utf-8")
            link = root / "bundle-link"
            link.symlink_to(victim)
            with self.assertRaises(ec.CleanupError):
                ec.safe_remove(root, link)
            self.assertTrue(victim.exists())

    def test_removing_root_itself_is_refused_by_symlink_free_but_still_requires_it_be_inside_itself(self) -> None:
        # root.relative_to(root) == "." which has zero parts, so the
        # component-walk finds nothing to check; the resolved-path
        # comparison below is what actually protects a bare root == target
        # call: it is allowed (root is trivially "inside" itself), which
        # is intentional - some callers may legitimately want to remove
        # the whole workdir - but it must still refuse if root is a
        # symlink to somewhere else.
        with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as tmp_other:
            outside = Path(tmp_other) / "somewhere"
            outside.mkdir()
            root = Path(tmp_root) / "link-root"
            root.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ec.CleanupError):
                ec.safe_remove(root, root)


class MaybeGzipTests(unittest.TestCase):
    def test_a_small_file_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build.log"
            path.write_text("short log", encoding="utf-8")
            result = ec.maybe_gzip(path, threshold_bytes=1_000_000)
            self.assertEqual(path, result)
            self.assertTrue(path.is_file())

    def test_a_large_file_is_compressed_and_the_original_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build.log"
            path.write_bytes(b"x" * 2000)
            result = ec.maybe_gzip(path, threshold_bytes=1000)
            self.assertEqual(path.with_name("build.log.gz"), result)
            self.assertTrue(result.is_file())
            self.assertFalse(path.exists())

    def test_a_missing_file_is_returned_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does-not-exist.log"
            self.assertEqual(path, ec.maybe_gzip(path))

    def test_compressed_content_round_trips(self) -> None:
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build.log"
            content = b"line one\nline two\n" * 200
            path.write_bytes(content)
            result = ec.maybe_gzip(path, threshold_bytes=100)
            with gzip.open(result, "rb") as fh:
                self.assertEqual(content, fh.read())


if __name__ == "__main__":
    unittest.main()
