"""tools/build_packages.py's --jobs support: the static scan that decides
which recipes must be locked against which others (source_cache_keys), the
note it prints for a path that scan does not recognize, and that --jobs 1
still takes the plain, unchanged serial path with no thread pool at all."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_packages", ROOT / "tools" / "build_packages.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_recipe(directory: str, name: str, download_sh: str, prebuild_sh: str | None = None) -> Path:
    pkg = Path(directory) / name
    upstream = pkg / "upstream"
    upstream.mkdir(parents=True)
    (pkg / "prebuild.sh").write_text(prebuild_sh or "#!/bin/bash\nset -e\ncd \"$(dirname \"$0\")/upstream\"\nbash download.sh\n")
    (pkg / "prebuild.sh").chmod(0o755)
    (upstream / "download.sh").write_text(download_sh)
    return pkg


class UnrecognizedPathTests(unittest.TestCase):
    """The class of bug found by actually running a concurrent build
    (resolve-gnome-ext.py's old hardcoded /tmp/gnome-ext-poc.zip): a recipe
    that hardcodes a path outside its own work tree, that source_cache_keys
    does not otherwise recognize, must not be let through as if it were
    safe."""

    def test_unrecognized_fixed_path_is_found_and_locked(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = _write_recipe(directory, "synos-x", "#!/bin/bash\nrm -rf /tmp/synos-x-shared-cache\necho done\n")
            keys = builder.source_cache_keys(pkg)
            self.assertIn(("unknown-path", "/tmp/synos-x-shared-cache"), keys)

    def test_two_recipes_sharing_the_same_unrecognized_path_share_one_lock(self) -> None:
        # This is what actually makes it safe: two recipes that hardcode the
        # identical unrecognized path are the ones serialized against each
        # other (the same corruption risk gnome-ext-poc.zip had).
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            a = _write_recipe(directory, "synos-a", "#!/bin/bash\nrm -rf /tmp/widget-cache\n")
            b = _write_recipe(directory, "synos-b", "#!/bin/bash\ncp -a /tmp/widget-cache /out\n")
            key_a = next(k for k in builder.source_cache_keys(a) if k[0] == "unknown-path")
            key_b = next(k for k in builder.source_cache_keys(b) if k[0] == "unknown-path")
            self.assertEqual(key_a, key_b)
            self.assertIs(builder._lock_for(("source-cache",) + key_a), builder._lock_for(("source-cache",) + key_b))

    def test_two_recipes_with_different_unrecognized_paths_do_not_share_a_lock(self) -> None:
        # Correctness never trades away speed it does not need to: an
        # unrelated recipe's own distinct path must not wait on this one.
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            a = _write_recipe(directory, "synos-a", "#!/bin/bash\nrm -rf /tmp/widget-cache-a\n")
            b = _write_recipe(directory, "synos-b", "#!/bin/bash\nrm -rf /tmp/widget-cache-b\n")
            key_a = next(k for k in builder.source_cache_keys(a) if k[0] == "unknown-path")
            key_b = next(k for k in builder.source_cache_keys(b) if k[0] == "unknown-path")
            self.assertNotEqual(key_a, key_b)
            self.assertIsNot(builder._lock_for(("source-cache",) + key_a), builder._lock_for(("source-cache",) + key_b))

    def test_run_prebuild_prints_a_note_naming_the_recipe_and_the_path(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = Path(directory) / "synos-x"
            pkg.mkdir()
            script = pkg / "prebuild.sh"
            script.write_text("#!/bin/bash\nset -e\ntrue  # would touch /tmp/synos-x-scratch/data here\n")
            script.chmod(0o755)
            subs = {"ARCH": "amd64", "SUITE": "test", "BASE": "test"}
            log: list[str] = []
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SYNOS_SKIP_PREBUILD", None)
                with contextlib.redirect_stderr(buf):
                    builder.run_prebuild(pkg, subs, log)
            note = buf.getvalue()
            self.assertIn("synos-x", note)
            self.assertIn("/tmp/synos-x-scratch/data", note)
            self.assertIn("serially", note)


class KnownPatternsStillGetTheirLocksTests(unittest.TestCase):
    """The generic unknown-path scan must not also flag what the two
    specific patterns already cover — that would be noisy and wrong (those
    are proven-safe-by-construction locks, not unknown state)."""

    def test_fetch_git_commit_destination_is_not_also_flagged_unknown(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = _write_recipe(directory, "synos-git", (
                "#!/bin/bash\nset -euo pipefail\n"
                "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
                "source \"$SCRIPT_DIR/../lib/build-guards.sh\"\n"
                "FOO_COMMIT=\"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\"   # pinned\n"
                "rm -rf /tmp/synos-git-checkout\n"
                "fetch_git_commit https://example.com/foo.git \"$FOO_COMMIT\" /tmp/synos-git-checkout\n"
                "cp -a /tmp/synos-git-checkout/data \"$SCRIPT_DIR/deploy\"\n"
                "rm -rf /tmp/synos-git-checkout\n"
            ))
            keys = builder.source_cache_keys(pkg)
            self.assertIn(("git-commit", "https://example.com/foo.git", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"), keys)
            self.assertFalse(any(k[0] == "unknown-path" for k in keys),
                              f"fetch_git_commit's own destination should not also show up as unknown: {keys}")

    def test_gnome_ext_download_call_is_not_also_flagged_unknown(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = _write_recipe(directory, "synos-ext", (
                "#!/bin/bash\nset -euo pipefail\n"
                "SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)\"\n"
                "DEPLOY_DIR=\"deploy/test/myext@example.com\"\n"
                "mkdir -p \"$DEPLOY_DIR\"\n"
                "python3 \"$SCRIPT_DIR/../lib/resolve-gnome-ext.py\" myext@example.com --target 47 --download "
                "--out \"$DEPLOY_DIR\"\n"
            ))
            keys = builder.source_cache_keys(pkg)
            self.assertEqual(keys, {("gnome-ext-download",)})


class SharedRustupHomeTests(unittest.TestCase):
    """The second class of bug found by running a real build: two Rust
    recipes both downloading one rustup component wrote the same
    $HOME/.rustup/downloads/<hash>.partial and one lost the rename. No
    recipe spells .rustup, so the literal-path scan cannot see it; the
    signal is the recipe invoking rustup or cargo at all."""

    def test_a_recipe_that_runs_rustup_gets_the_shared_key(self) -> None:
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = _write_recipe(directory, "synos-rs", (
                "#!/bin/bash\nset -e\n"
                "rustup target add aarch64-unknown-linux-gnu\n"))
            self.assertIn(("rustup-home",), builder.source_cache_keys(pkg))

    def test_a_recipe_that_only_runs_cargo_gets_the_same_shared_key(self) -> None:
        # cargo is the rustup proxy on these images (bases/*/Containerfile
        # puts /root/.cargo/bin on PATH), so `cargo build` alone can fetch a
        # component into $HOME/.rustup — which is how the real failure
        # happened: neither recipe called rustup directly.
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            a = _write_recipe(directory, "synos-a", "#!/bin/bash\nset -e\ncargo build --release\n")
            b = _write_recipe(directory, "synos-b", (
                "#!/bin/bash\nset -e\nneed_cmd rustup\n"
                "rustup target add aarch64-unknown-linux-gnu\n"
                "cargo build --release --target aarch64-unknown-linux-gnu\n"))
            key_a = next(k for k in builder.source_cache_keys(a) if k[0] == "rustup-home")
            key_b = next(k for k in builder.source_cache_keys(b) if k[0] == "rustup-home")
            # one key for all of them: the hazard is one shared directory,
            # not one shared file, so any two Rust recipes serialize.
            self.assertEqual(key_a, key_b)
            self.assertIs(builder._lock_for(("source-cache",) + key_a),
                          builder._lock_for(("source-cache",) + key_b))

    def test_a_recipe_that_runs_neither_gets_no_rustup_key(self) -> None:
        # The lock must serialize Rust recipes against each other, never the
        # whole build; prose mentioning cargo is not an invocation either.
        builder = load_builder()
        with tempfile.TemporaryDirectory() as directory:
            pkg = _write_recipe(directory, "synos-plain", (
                "#!/bin/bash\nset -e\n"
                "# the image's own cargo/rustup are apt packages, not used here\n"
                "make -C src all\n"))
            self.assertNotIn(("rustup-home",), builder.source_cache_keys(pkg))
            self.assertEqual(set(), builder.source_cache_keys(pkg))

    def test_the_real_rust_recipes_are_the_ones_that_share_it(self) -> None:
        builder = load_builder()
        locked = sorted(p.name for p in sorted((ROOT / "packages").iterdir())
                        if p.is_dir() and ("rustup-home",) in builder.source_cache_keys(p))
        self.assertIn("synos-swapcontrol-gtk", locked)   # the two that actually collided
        self.assertIn("synos-yubikey-manager", locked)
        # and not every recipe in the repository
        self.assertLess(len(locked), len([p for p in (ROOT / "packages").iterdir() if p.is_dir()]) // 2)


class JobsOneIsStillPlainSerialTests(unittest.TestCase):
    """--jobs 1 must be the same code path as before this feature existed:
    no thread pool, not even one of size 1. SYNOS_KEYS_DIR is pointed at a
    throwaway directory (the same thing test_manifest_render.py and
    test_toolchain.py do), so running this never touches the tracked
    keys/ directory a plain `main()` call would otherwise generate a
    development key into."""

    def test_jobs_one_never_constructs_a_thread_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"SYNOS_KEYS_DIR": str(Path(directory) / "keys")}):
                builder = load_builder()
                with mock.patch.object(builder.cf, "ThreadPoolExecutor", side_effect=AssertionError(
                        "ThreadPoolExecutor must not be constructed when --jobs 1")):
                    rc = builder.main(["--only", "synos-appstore", "--jobs", "1",
                                      "--output", str(Path(directory) / "repo")])
            self.assertEqual(0, rc)

    def test_default_jobs_is_auto(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"SYNOS_KEYS_DIR": str(Path(directory) / "keys")}):
                builder = load_builder()
                with mock.patch.object(builder, "resolve_jobs", wraps=builder.resolve_jobs) as spy:
                    rc = builder.main(["--only", "synos-appstore", "--output", str(Path(directory) / "repo")])
            self.assertEqual(0, rc)
            spy.assert_called_once_with("auto")


if __name__ == "__main__":
    unittest.main()
