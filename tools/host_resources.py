#!/usr/bin/env python3
"""host_resources — how many builds this machine can actually feed at once.

A build needs, roughly and each independently binding: MIN_FREE_GB under the
engine checkout it builds in (tools/synos's own threshold), MIN_STORE_GB in
the container runtime's storage (bundle_launcher.sh's own threshold),
MIN_MEMORY_GB_PER_JOB of memory (a documented assumption, not a measurement:
debootstrap, squashfs compression and the chroot together are memory-hungry
but this engine does not publish a number; picked conservatively and named
here so it can be argued with), and CPUS_PER_JOB cores (squashfs compression
and the package manager both parallelize somewhat; oversubscribing cores
slows every concurrent build down together rather than failing any one of
them, which is why CPUS_PER_JOB is the one factor floored at 1 regardless
of core count — the other three are not floored, so a machine that cannot
feed even one build (1 GB free against a 40 GB requirement, say) is refused
outright rather than silently offered "1").

`derive_safe_jobs` returns the smallest of the four, with the numbers that
produced it, so the reason a run refuses a `--jobs` above it is not "no",
it is "disk allows 3, memory allows 6, CPUs allow 16 — the binding one is
disk, pass --jobs auto to use 3".
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

MIN_FREE_GB = 40.0          # tools/synos MIN_FREE_GB: the working checkout
MIN_STORE_GB = 30.0         # bundle_launcher.sh: the container runtime's own storage
MIN_MEMORY_GB_PER_JOB = 4.0  # a documented assumption (see module docstring), not a measurement
CPUS_PER_JOB = 2             # squashfs compression and package installs use more than one core


def container_engine() -> str | None:
    return shutil.which("podman") or shutil.which("docker")


def container_store(engine: str, container_root: str | None = None) -> str | None:
    """Where the runtime keeps images, layers and the chroot. When podman was
    told to use another location (SYNOS_CONTAINER_ROOT, the same setting
    bundle_launcher.sh and tools/synos honor), that path is authoritative and
    is returned directly rather than asked back of `podman info`, which
    reports the daemon default and would otherwise measure the wrong disk."""
    if container_root and "podman" in Path(engine).name:
        return container_root
    for fmt in ("{{.DockerRootDir}}", "{{.Store.GraphRoot}}"):
        result = subprocess.run([engine, "info", "--format", fmt], capture_output=True, text=True, check=False, timeout=20)
        value = result.stdout.strip()
        if result.returncode == 0 and value and "{{" not in value and value != "<no value>":
            return value
    return None


def total_memory_gb() -> float | None:
    """None when this platform does not expose SC_PHYS_PAGES (not Linux/POSIX);
    callers must not treat that as zero memory."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return None
    if pages < 0 or page_size < 0:
        return None
    return pages * page_size / 1024**3


_UNSET = object()  # distinct from None: None is a legitimate override meaning "no such constraint"


def derive_safe_jobs(root: Path, *, min_free_gb: float = MIN_FREE_GB, min_store_gb: float = MIN_STORE_GB,
                     min_memory_gb_per_job: float = MIN_MEMORY_GB_PER_JOB, cpus_per_job: int = CPUS_PER_JOB,
                     free_root_gb=_UNSET, free_store_gb=_UNSET, memory_gb=_UNSET, cpu_count=_UNSET,
                     container_root: str | None = None) -> tuple[int, dict]:
    """The largest --jobs this machine's own numbers can feed, and the four
    numbers that went into it. Can come back 0 (not floored to 1): a machine
    without even 40 GB free cannot safely run one build either, and must be
    refused, not quietly offered "1" (resolve_jobs does that refusing).
    The *_gb/cpu_count keyword-only overrides exist for tests: passing one
    fixes that number instead of measuring the real host (None is itself a
    valid override — "no container store" — distinct from omitting it).
    container_root, when given (or SYNOS_CONTAINER_ROOT is set and this
    keyword is left at its default None), measures free space at that
    location instead of the runtime's own default storage — an unset
    variable and an omitted keyword both leave this exactly as before."""
    container_root = container_root or os.environ.get("SYNOS_CONTAINER_ROOT")
    if free_root_gb is _UNSET:
        free_root_gb = shutil.disk_usage(root).free / 1024**3
    if free_store_gb is _UNSET:
        engine = container_engine()
        store = container_store(engine, container_root) if engine else None
        free_store_gb = (shutil.disk_usage(store).free / 1024**3) if store and Path(store).is_dir() else None
    if memory_gb is _UNSET:
        memory_gb = total_memory_gb()
    if cpu_count is _UNSET:
        cpu_count = os.cpu_count() or 1

    disk_checkout_jobs = int(free_root_gb // min_free_gb)
    disk_store_jobs = int(free_store_gb // min_store_gb) if free_store_gb is not None else None
    disk_jobs = min(disk_checkout_jobs, disk_store_jobs) if disk_store_jobs is not None else disk_checkout_jobs
    memory_jobs = int(memory_gb // min_memory_gb_per_job) if memory_gb is not None else disk_jobs
    cpu_jobs = max(1, cpu_count // cpus_per_job)  # the one factor floored at 1: see the module docstring

    safe = min(disk_jobs, memory_jobs, cpu_jobs)
    factors = {
        "safe_jobs": safe,
        "disk_jobs": disk_jobs, "disk_checkout_jobs": disk_checkout_jobs, "disk_store_jobs": disk_store_jobs,
        "memory_jobs": memory_jobs, "cpu_jobs": cpu_jobs,
        "free_root_gb": round(free_root_gb, 1), "free_store_gb": round(free_store_gb, 1) if free_store_gb is not None else None,
        "memory_gb": round(memory_gb, 1) if memory_gb is not None else None, "cpu_count": cpu_count,
        "min_free_gb": min_free_gb, "min_store_gb": min_store_gb,
        "min_memory_gb_per_job": min_memory_gb_per_job, "cpus_per_job": cpus_per_job,
    }
    return safe, factors


def explain(factors: dict) -> str:
    store_part = f", store {factors['free_store_gb']:.0f} GB free / {factors['min_store_gb']:.0f} GB per build" \
        if factors.get("free_store_gb") is not None else ""
    memory_part = f"{factors['memory_gb']:.0f} GB" if factors.get("memory_gb") is not None else "unknown"
    return (f"disk allows {factors['disk_jobs']} ({factors['free_root_gb']:.0f} GB free / {factors['min_free_gb']:.0f} GB per build{store_part}), "
           f"memory allows {factors['memory_jobs']} ({memory_part} / {factors['min_memory_gb_per_job']:.0f} GB per build), "
           f"CPUs allow {factors['cpu_jobs']} ({factors['cpu_count']} cores / {factors['cpus_per_job']} per build)")


def resolve_jobs(requested: str, root: Path, **derive_kwargs) -> tuple[int, list[str]]:
    """requested is "auto" or a base-10 integer string (argparse hands us the
    raw --jobs value either way). Returns (jobs, problems); problems is
    non-empty exactly when the run must not proceed with the returned jobs
    value (an explicit --jobs above what derive_safe_jobs allows, or a
    non-positive number)."""
    safe_jobs, factors = derive_safe_jobs(root, **derive_kwargs)
    if safe_jobs < 1:
        return 0, [f"this machine cannot safely feed even one build: {explain(factors)}"]
    if requested == "auto":
        return safe_jobs, []
    try:
        jobs = int(requested)
    except ValueError:
        return 0, [f"--jobs must be a positive integer or \"auto\", not {requested!r}"]
    if jobs < 1:
        return jobs, ["--jobs must be at least 1"]
    if jobs > safe_jobs:
        return jobs, [f"--jobs {jobs} exceeds what this machine can safely feed ({safe_jobs}): "
                      f"{explain(factors)}; pass --jobs auto to use {safe_jobs}"]
    return jobs, []
