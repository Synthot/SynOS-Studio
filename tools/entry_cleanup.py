#!/usr/bin/env python3
"""entry_cleanup — frees the heavy things a single catalog-conformance
build entry created (the unpacked bundle, its build tree, the multi-GB
ISO) as soon as that entry succeeds, so a full pass over fifty-odd
catalogued bundles — each a 2 GB image plus an unpacked bundle and a build
tree — does not fill the disk long before it finishes.

Two small, generic, heavily-guarded primitives (`guard_workdir`,
`safe_remove`) plus a compression helper (`maybe_gzip`); the domain
policy of *what* counts as heavy versus evidence for one catalog-
conformance entry lives in tools/catalog_conformance.py's own
`cleanup_one()`, which is the only caller of these that needs to know
this project's directory layout.

Safety rules, applied by every function here that can delete anything:

- **Only inside the run's own working directory.** `safe_remove(root,
  target)` refuses if `target`, once resolved, is not `root` itself or
  somewhere under it.
- **Never follow a symlink out of it.** Every path component between
  `root` and `target` — `target` itself included — is checked with
  `Path.is_symlink()` before anything is touched; the first symlink found
  refuses the whole removal. A build's own `build.sh` runs arbitrary
  shell inside the bundle it unpacked; nothing here assumes that tree is
  well-behaved.
- **Refuse to run at all somewhere that looks like something else.**
  `guard_workdir(path)` refuses a filesystem root, the invoking user's
  home directory, or a directory that itself looks like a source
  checkout (a `.git`/`.hg`/`.svn` entry directly inside it) — called once,
  unconditionally, at the start of every `build` run (tools/catalog_
  conformance.py's `run_build`), whether or not cleanup is even enabled,
  because pointing `workdir` at the wrong place is a configuration
  mistake worth refusing outright, not only once deletion is about to
  happen.
"""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path

DEFAULT_GZIP_THRESHOLD_BYTES = 1_000_000  # 1 MB: build.sh logs are usually much smaller than this
_DANGEROUS_MARKERS = (".git", ".hg", ".svn")


class CleanupError(Exception):
    """A workdir or a removal request failed one of the safety checks
    above. Always refuses; never partially deletes."""


def guard_workdir(path: Path) -> None:
    """Refuses to treat `path` as a build-conformance workdir (and so,
    transitively, refuses the whole run) when it looks like something a
    person would never want emptied out from under them."""
    resolved = path.resolve()
    if str(resolved) == resolved.anchor:
        raise CleanupError(f"refusing to use {resolved} as a build-conformance workdir: it is a filesystem root")
    home = Path.home().resolve()
    if resolved == home:
        raise CleanupError(f"refusing to use {resolved} as a build-conformance workdir: it is a home directory")
    for marker in _DANGEROUS_MARKERS:
        if (resolved / marker).exists():
            raise CleanupError(
                f"refusing to use {resolved} as a build-conformance workdir: it looks like a source checkout "
                f"({marker} found directly inside it) — point workdir at a directory dedicated to this tool")


def _resolve_or_none(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def safe_remove(root: Path, target: Path) -> None:
    """Removes `target` (file or directory), refusing outright rather than
    removing anything if `target` is not inside `root`, or if any path
    component from `root` down to (and including) `target` is a symlink.
    A no-op, not an error, when `target` does not exist."""
    if not target.exists() and not target.is_symlink():
        return
    if root.is_symlink():
        raise CleanupError(f"refusing to remove anything under {root}: it is itself a symlink")
    root_resolved = root.resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        raise CleanupError(f"refusing to remove {target}: it is not inside {root}") from None

    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise CleanupError(f"refusing to remove {target}: {current} is a symlink (would follow it out of {root})")

    resolved_target = _resolve_or_none(target)
    if resolved_target is None:
        raise CleanupError(f"refusing to remove {target}: could not resolve its real path")
    if resolved_target != root_resolved and root_resolved not in resolved_target.parents:
        raise CleanupError(f"refusing to remove {target}: resolves to {resolved_target}, outside {root}")

    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink()


def maybe_gzip(path: Path, *, threshold_bytes: int = DEFAULT_GZIP_THRESHOLD_BYTES) -> Path:
    """Compresses `path` to `path.gz` in place and removes the original
    when it is larger than `threshold_bytes`; otherwise leaves it exactly
    as it was. Returns whichever path now holds the content. A missing
    file is returned unchanged (nothing to compress)."""
    if not path.is_file():
        return path
    if path.stat().st_size <= threshold_bytes:
        return path
    gz_path = path.with_name(path.name + ".gz")
    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()
    return gz_path
