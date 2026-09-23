#!/usr/bin/env python3
"""scratch_checkout — a disposable git worktree of this engine checkout.

`tools/synos build <bundle>` applies a bundle's files (profiles/,
manifests/, branding/, keys/, answers/) into *whatever checkout the
`tools/synos` script it was invoked as lives in* (its own `ROOT`,
`Path(__file__).resolve().parent.parent`) before building — that is
correct and exactly what a person building by hand wants. It is wrong for
an unattended matrix or conformance run: invoking this repository's own
`tools/synos` would leave one `profiles/<id>.yml` behind per catalog entry
it builds, dirtying the checkout the matrix itself is running from and,
downstream, tripping `tests/unit/test_bundle_catalog.py`'s own guard
against a catalog entry shadowing an engine profile.

The fix used by `tools/build_matrix.py` and `tools/catalog_conformance.py`
is to never invoke *this* checkout's `tools/synos` for a real build: create
a disposable `git worktree` of the current commit, invoke *that* worktree's
own `tools/synos` (whose `ROOT` then resolves to the worktree, not here),
and remove the worktree afterwards. `git worktree` is used rather than a
plain file copy because it is fast (no `packages/*/upstream` vendored
sources to copy) and because the object store is shared, not duplicated;
a worktree checks out the current commit's *tracked* files only, the same
thing a fresh clone of this exact commit would have, deliberately excluding
uncommitted local edits — a proof run should prove what is about to be
committed, not a work in progress.

Cleanup is best-effort and defensive: `git worktree remove --force` (a
build ran `--privileged` and may have left root-owned files under the
worktree's own `.build`/`dist`, which a plain `rm` as the invoking user
cannot always remove) falls back to `shutil.rmtree(..., ignore_errors=True)`
and, either way, `git worktree prune` clears stale metadata. Every scratch
checkout this process creates is registered for cleanup on process exit and
on SIGTERM/SIGINT, so a killed run leaves at most an orphaned directory
under its own `--output`/`workdir` (never a dirty *engine* checkout, since
nothing is ever applied there) and, best-effort, not even that.
"""
from __future__ import annotations

import atexit
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

_ACTIVE: dict[Path, Path] = {}   # scratch path -> the root it was created from
_SIGNAL_HANDLERS_INSTALLED = False


class ScratchCheckoutError(Exception):
    """git worktree could not be created or removed."""


def _cleanup_all() -> None:
    for path in list(_ACTIVE):
        remove(path)


def _install_signal_handlers() -> None:
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED:
        return
    _SIGNAL_HANDLERS_INSTALLED = True
    atexit.register(_cleanup_all)
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum, frame, _previous=previous):  # noqa: ANN001
            _cleanup_all()
            if callable(_previous) and _previous not in (signal.SIG_DFL, signal.SIG_IGN):
                _previous(signum, frame)
            else:
                signal.signal(signum, signal.SIG_DFL)
                import os
                os.kill(os.getpid(), signum)

        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not the main thread / signal not available here: best-effort only


def create(root: Path, base_dir: Path, *, label: str = "scratch") -> Path:
    """A disposable worktree of `root`'s current commit, under `base_dir`.
    Registered for cleanup on process exit and on SIGTERM/SIGINT; the
    caller should still remove() it explicitly (a `finally` or the
    `scratch()` context manager below) as soon as it is done with it."""
    base_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_." else "-" for c in label) or "scratch"
    path = base_dir / f"{safe_label}-{uuid.uuid4().hex[:12]}"
    result = subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(path), "HEAD"],
                            cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ScratchCheckoutError(f"git worktree add {path} (from {root}) failed: {result.stderr.strip()}")
    _install_signal_handlers()
    _ACTIVE[path] = root
    return path


def remove(path: Path) -> None:
    """Best-effort: tolerates a build's own root-owned leftovers and a
    path that is already gone."""
    root = _ACTIVE.pop(path, None)
    if root is not None:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=root,
                       capture_output=True, text=True, check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if root is not None:
        subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True, check=False)


@contextmanager
def scratch(root: Path, base_dir: Path, *, label: str = "scratch"):
    """with scratch(root, base_dir, label="my-target") as path: ..."""
    path = create(root, base_dir, label=label)
    try:
        yield path
    finally:
        remove(path)


def _main(argv: list[str] | None = None) -> int:  # a tiny manual smoke check, not part of the public API
    import argparse
    parser = argparse.ArgumentParser(description="Create a scratch worktree, print its path, remove it on exit.")
    parser.add_argument("root", type=Path)
    parser.add_argument("--base", type=Path, default=Path("/tmp/synos-scratch"))
    args = parser.parse_args(argv)
    with scratch(args.root, args.base, label="manual") as path:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
