#!/usr/bin/env python3
"""build_status — the one small file the Studio page fetches on every load
to show, per catalogued bundle *and per base it was built against*, a
badge with one of three things at a glance and a date: not yet tested
(which covers queued and testing right now too), succeeded, or failed
since a date — plus a short history so a page can later chart each
bundle's story on each base.

tools/catalog_conformance.py's own build-report.json is the full record of
a run (every stage, every log tail, every smoke-test detail) and is not
meant to be fetched by a browser on every page load. This module derives
the small, public subset of it — build-status.json — and keeps it correct
not just across the many hours a full pass over the catalog takes, but
*during* it: an entry is marked "testing" the moment its build starts and
resolved to a real outcome when it finishes, so a file fetched mid-run
tells the truth about a run in progress rather than showing yesterday's
result until the whole pass completes.

    updater = StatusUpdater(path, on_change=maybe_upload)
    updater.queue(entry_id, base)             # about to be attempted this run
    updater.start(entry_id, base)              # the build for it has begun
    updater.record(entry_id, base, result)     # it finished: a build_one() dict
    updater.resolve_interrupted()              # once, at the start of a run: see below

Shape (schema_version 2)::

    {
      "schema_version": 2,
      "generated_at": "2026-09-24T16:40:00+00:00",
      "entries": {
        "web-server-nginx": {
          "bases": {
            "ubuntu": {
              "state": "success",
              "date": "2026-09-24T16:28:52+00:00",
              "since": "2026-09-20T09:00:00+00:00",
              "engine": "0.2.0",
              "suite": "noble",
              "size": 1234567890,
              "checksum": "b1946ac92492d2347c6235b4d2611184...",
              "boot_passed": true,
              "error": null,
              "history": [
                {"date": "2026-09-20T09:00:00+00:00", "state": "success", "engine": "0.2.0",
                 "suite": "noble", "duration_s": 2412.3, "stage": null, "error": null}
              ]
            }
          }
        }
      }
    }

### Item 111: why per base

The Studio page lets a person change an appliance's base *after* choosing
it from the catalog, so a badge that only ever covered the base the entry
happens to pin (schema_version 1's own shape — one result per entry, full
stop) is a claim about something the person may not actually build. Every
per-entry record here became a per-*(entry, base)* one instead: `bases`
maps a base name (`"ubuntu"`, `"debian"` — the page renders one small icon
per base with a tooltip, so these stay exactly the two names the engine
already uses) to the exact same shape schema_version 1 kept at the
entry's own top level. **An entry with no result for a base says nothing
for that base** — `bases` simply has no key for it, the same "absence
means never tested" rule schema_version 1 already used one level up, now
applied one level deeper.

`migrate_v1_to_v2()` (called automatically by `load_status()` on an old
file) carries a v1 entry's single result into `bases[<that entry's own
base>]`; an entry that was only ever `queued`/`testing` (no real outcome,
so no base was ever confirmed) has nothing worth migrating and is simply
dropped — it will be queued again on the next run regardless, the same as
a brand new entry that was never tested at all.

Not to be confused with tools/build_matrix.py's own bundle-catalog/build-
status.yml: that one is a YAML file tracked *in this repository*, derived
from a self-hosted CI run proving this checkout's own bundle-catalog/
still builds, and is meant for a person reviewing a pull request. This one
is JSON, lives only under a conformance run's own workdir (never tracked
here) and, once optionally uploaded (tools/status_uploader.py), is meant
for a browser fetching it from the deployed Studio site — proof of the
*published* catalog, not this checkout's copy of it.

### States

`state` is one of ``"queued"``, ``"testing"``, ``"success"``, ``"failed"``,
``"skipped"`` (map_state() folds every one of build_one()'s finer-grained
final statuses — error, timeout, invalid, host, build_failed, and a build
that succeeded but whose boot check did not ("smoke_failed") — into
``"failed"``; those distinctions are what build-report.json is for). The
sixth state the owner listed, **"never tested"**, is never written
explicitly: a (entry, base) pair the catalog could produce but this file's
`entries[id].bases` does not have a key for is "never tested" by its
absence — the smallest possible encoding of "no result yet, ever", keeping
the file small and giving the page side exactly one extra rule ("absent =
never tested") instead of one more literal string to special-case.

### since

`since` is the date this (entry, base)'s *current* `state` began, as an
unbroken run of that same state at the end of `history` (skipping the live
`queued`/`testing` states a run passes through on the way to a real
outcome) — not merely the date of the latest attempt. A bundle that fails
every night for a week shows the date the failing streak started, not
last night; a bundle that has been green for a month shows when it turned
green, not today's unremarkable re-confirmation. Both directions use the
exact same rule (compare the new outcome's state against the most recent
`history` entry's state), which is simpler to reason about and to test
than two different notions of "since" for success and failure.

### An interrupted "testing"

A run that is killed mid-build leaves whatever (entry, base) it was on
written as `"testing"` — accurate at the time, stale forever after unless
something resolves it. `resolve_interrupted()`, called once at the very
start of the *next* run before anything is queued, turns every (entry,
base) still stuck in `"testing"` into `"failed"` with `error` explaining
why (`INTERRUPTED_ERROR`) — never guessed as `"success"`: a public "safe
to use" badge must never claim an outcome nobody actually observed. The
resolution is itself recorded as a history entry, and the caller (`run_
build`) is handed back which (entry, base) pairs this touched, to say so
plainly in the run's own summary.

### History

Bounded to the most recent `HISTORY_LIMIT` entries (a fixed count, chosen
over a time window because it bounds the file's worst-case size exactly
regardless of how often builds run — a time window's size depends on
build frequency, which is not this file's business to assume; the full,
unbounded detail already lives in build-report.json, which this file is
explicitly the small public shadow of). Oldest first, newest last.

### error

Only ever a single sanitized line (`error_summary()`/`sanitize_error()`):
the stage that failed and the first real error line, with control
characters stripped, this machine's own home directory and working
directory (and the generic `/home/*`, `/root` patterns as a backstop),
this machine's own hostname, IPv4 addresses, and anything that looks like
a `password=`/`token=`/`secret=`-shaped credential redacted, and the
result capped at `MAX_ERROR_LEN` characters — published on a public page,
so treated as untrusted output being published, not as an internal log
line. Best-effort, not a guarantee: it catches the specific, named classes
of leak item 96 asked for, not every conceivable one, and is not a
substitute for keeping real secrets out of build.sh's own log lines in the
first place.

"size"/"checksum" are the built ISO's, not the downloaded bundle
archive's (build_one()'s own top-level "checksum" is the archive's,
recorded because the archive is what proves the page did not silently
drop a field; what a badge means by "safe to use" is the image), and are
null whenever there is none (nothing but a `success` result ever has an
ISO). "boot_passed" (item 111's own "boot result") is null when the smoke
test did not run at all (smoke: false in conformance.yml, or the build
did not reach that point); true/false only once it actually ran — and can
be false even while `state` is "success", since a build succeeding and
its boot check certifying it are two different things build_one() itself
now keeps apart (build-report.json's own "smoke_failed" status).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 2
HISTORY_LIMIT = 10
MAX_ERROR_LEN = 240
INTERRUPTED_ERROR = "the previous run was interrupted before this entry finished testing"

_EMPTY_BASE_RECORD = {"state": None, "date": None, "since": None, "engine": None, "suite": None,
                      "size": None, "checksum": None, "boot_passed": None, "error": None, "history": []}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------- sanitizing
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HOME_PATH_RE = re.compile(r"/home/[^\s\"'()]+")
_ROOT_PATH_RE = re.compile(r"/root(?:/[^\s\"'()]*)?")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_CREDENTIAL_RE = re.compile(r"(?i)\b(pass(?:word)?s?|passwd|token|secret|api[_-]?keys?|credentials?)\s*[:=]\s*\S+")


def sanitize_error(text: str | None, *, own_paths: Iterable[str] = (), hostname: str | None = None) -> str:
    """One line, safe to publish. `own_paths` is this run's own machine-
    specific absolute paths (typically the workdir and the home directory
    of whatever user runs the service) to blank out exactly; the `/home/*`
    and `/root` patterns are a backstop for a path under some *other*
    user's home the log happened to mention. Never raises on odd input."""
    if not text:
        return ""
    line = next((candidate.strip() for candidate in str(text).splitlines() if candidate.strip()), "")
    line = _CONTROL_CHARS_RE.sub("", line)
    for own_path in own_paths:
        if own_path:
            line = line.replace(own_path, "<path>")
    line = _HOME_PATH_RE.sub("<path>", line)
    line = _ROOT_PATH_RE.sub("<path>", line)
    if hostname:
        line = line.replace(hostname, "<host>")
    line = _IPV4_RE.sub("<ip>", line)
    line = _CREDENTIAL_RE.sub(lambda m: f"{m.group(1)}=***", line)
    line = line.strip()
    if len(line) > MAX_ERROR_LEN:
        line = line[:MAX_ERROR_LEN].rstrip() + "…"
    return line


def error_summary(result: dict, *, own_paths: Iterable[str] = (), hostname: str | None = None) -> str | None:
    """"the stage that failed and the first real error" (item 96) — never
    the whole log_tail. None when there is nothing to say (a clean success,
    or a failure result carrying no log_tail at all)."""
    log_tail = result.get("log_tail") or []
    first_line = next((candidate for candidate in log_tail if candidate.strip()), "")
    stage = result.get("stage")
    parts = [p for p in (stage, first_line.strip()) if p]
    if not parts:
        return None
    return sanitize_error(": ".join(parts), own_paths=own_paths, hostname=hostname) or None


# ------------------------------------------------------------------ state
def map_state(status: str | None) -> str:
    """Folds build_one()'s finer final statuses down to the three a
    completed attempt can end in here. Anything that is not a clean
    "success" or an explicit "skipped" (no browser available) is "failed"
    — including "smoke_failed" (a build that succeeded but whose boot
    check did not): a badge does not need to know whether it was the
    launcher, the engine, a timeout, or the boot check; build-report.json
    already says which, and this file's own "boot_passed" field on
    success still says whether the check itself ran clean."""
    if status == "success":
        return "success"
    if status == "skipped":
        return "skipped"
    return "failed"


def empty_status() -> dict:
    return {"schema_version": SCHEMA_VERSION, "generated_at": _now(), "entries": {}}


def _migrate_history_entry_v1_to_v2(entry: dict) -> dict:
    """Drops the old per-history-entry "base" field: redundant once the
    history itself lives inside a specific base's own bucket."""
    return {"date": entry.get("date"), "state": entry.get("state"), "engine": entry.get("engine"),
            "suite": entry.get("suite"), "duration_s": entry.get("duration_s"),
            "stage": entry.get("stage"), "error": entry.get("error")}


def migrate_v1_to_v2(v1_status: dict) -> dict:
    """schema_version 1 kept exactly one result per entry, at whatever
    base its manifest happened to pin. Item 111: the owner wants a result
    per base instead, since the page lets a person change the base after
    choosing a catalogued bundle. Each v1 entry with a *known* base (it
    reached a real outcome at least once) becomes that base's own result
    under the new `bases` key; an entry only ever `queued`/`testing` (no
    real outcome yet, so no base was ever confirmed) has nothing to
    migrate and is simply dropped — on the next run it is queued again,
    indistinguishable from an entry that was never tested at all."""
    entries: dict = {}
    for entry_id, old in (v1_status.get("entries") or {}).items():
        base = old.get("base")
        if not base:
            continue
        record = {"state": old.get("state"), "date": old.get("date"), "since": old.get("since"),
                 "engine": old.get("engine"), "suite": old.get("suite"), "size": old.get("size"),
                 "checksum": old.get("checksum"), "boot_passed": old.get("smoke_passed"),
                 "error": old.get("error"),
                 "history": [_migrate_history_entry_v1_to_v2(h) for h in (old.get("history") or [])]}
        entries[entry_id] = {"bases": {base: record}}
    return {"schema_version": SCHEMA_VERSION, "generated_at": _now(), "entries": entries}


def load_status(path: Path) -> dict:
    """A missing or unreadable file starts fresh, same as a page treats it
    (the page side is being told to treat a missing/unreadable file as "no
    badge at all"; this side must never let a corrupt on-disk file wedge a
    run — it starts over rather than refusing to write). A schema_version
    1 file is migrated (migrate_v1_to_v2()) rather than discarded: real
    history a previous run already earned is worth carrying forward."""
    if not path.is_file():
        return empty_status()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_status()
    if not isinstance(data, dict):
        return empty_status()
    version = data.get("schema_version")
    if version == SCHEMA_VERSION and isinstance(data.get("entries"), dict):
        return data
    if version == 1 and isinstance(data.get("entries"), dict):
        return migrate_v1_to_v2(data)
    return empty_status()


def _stamp(status: dict, entries: dict) -> dict:
    return {"schema_version": SCHEMA_VERSION, "generated_at": _now(), "entries": entries}


def _get_base_record(entries: dict, entry_id: str, base: str) -> dict | None:
    entry = entries.get(entry_id)
    if not entry:
        return None
    return (entry.get("bases") or {}).get(base)


def _set_base_record(entries: dict, entry_id: str, base: str, record: dict) -> None:
    entry = dict(entries.get(entry_id) or {"bases": {}})
    bases = dict(entry.get("bases") or {})
    bases[base] = record
    entry["bases"] = bases
    entries[entry_id] = entry


def mark_queued(status: dict, entry_id: str, base: str) -> tuple[dict, bool]:
    """About to be attempted this run, on `base`. A fresh (entry, base)
    starts a brand new record; one that already has one (a previous real
    outcome, or one left `queued`/`testing` by a run that never got to it)
    only has its live `state`/`date` moved — `since` and `history`
    describe the last *real* outcome and are left alone, so a page still
    has something meaningful to show while this (entry, base) sits in the
    queue again."""
    entries = dict(status.get("entries") or {})
    existing = _get_base_record(entries, entry_id, base)
    changed = existing is None or existing.get("state") != "queued"
    record = dict(existing) if existing else dict(_EMPTY_BASE_RECORD, history=[])
    record["state"] = "queued"
    record["date"] = _now()
    if record.get("since") is None:
        record["since"] = record["date"]
    _set_base_record(entries, entry_id, base, record)
    return _stamp(status, entries), changed


def mark_testing(status: dict, entry_id: str, base: str) -> tuple[dict, bool]:
    """The build for `entry_id` on `base` has actually started."""
    entries = dict(status.get("entries") or {})
    existing = _get_base_record(entries, entry_id, base)
    changed = existing is None or existing.get("state") != "testing"
    record = dict(existing) if existing else dict(_EMPTY_BASE_RECORD, history=[])
    record["state"] = "testing"
    record["date"] = _now()
    if record.get("since") is None:
        record["since"] = record["date"]
    _set_base_record(entries, entry_id, base, record)
    return _stamp(status, entries), changed


def apply_result(status: dict, entry_id: str, base: str, result: dict, *, own_paths: Iterable[str] = (),
                 hostname: str | None = None) -> tuple[dict, bool]:
    """The (entry, base) finished. Computes `since` by comparing against
    the most recent *history* entry's state (never the live `queued`/
    `testing` state a moment ago), appends this attempt to `history`
    (bounded to HISTORY_LIMIT, oldest first), and fills `error` for a
    failure or a skip.

    `changed` deliberately also compares against the last real *history*
    entry, not the live `queued`/`testing` state a moment ago (which would
    make it true on every single completed attempt, since "testing" never
    equals a real outcome) — item 73's original ask, "upload after each
    entry whose state changed, not every entry", is about the *outcome*,
    e.g. a bundle that re-confirms `success` every night should not
    re-upload every night just because it briefly passed through
    `testing` to get there. mark_queued()/mark_testing() are the ones that
    make a run's live progress visible (item 93); this is the one that
    decides whether the final outcome is worth a fresh upload on its own.
    Never raises: a malformed result still produces a record, with
    whatever it could find and null for the rest."""
    entries = dict(status.get("entries") or {})
    previous = _get_base_record(entries, entry_id, base)
    new_state = map_state(result.get("status"))
    date = result.get("end") or result.get("start") or _now()

    previous_history = list(previous.get("history") or []) if previous else []
    previous_final_state = previous_history[-1]["state"] if previous_history else None
    since = previous.get("since") if (previous and previous_final_state == new_state and previous.get("since")) else date

    error = None
    if new_state in ("failed", "skipped"):
        error = error_summary(result, own_paths=own_paths, hostname=hostname)

    iso = result.get("iso") or {}
    smoke = result.get("smoke") or {}
    boot_passed = None
    if smoke.get("status") in ("passed", "failed"):
        boot_passed = smoke["status"] == "passed"

    history_entry = {"date": date, "state": new_state, "engine": result.get("engine"),
                     "suite": result.get("suite"), "duration_s": result.get("duration_s"),
                     "stage": result.get("stage"), "error": error}
    history = (previous_history + [history_entry])[-HISTORY_LIMIT:]

    new_record = {"state": new_state, "date": date, "since": since, "engine": result.get("engine"),
                 "suite": result.get("suite"), "size": iso.get("size"), "checksum": iso.get("sha256"),
                 "boot_passed": boot_passed, "error": error, "history": history}

    changed = previous_final_state != new_state
    _set_base_record(entries, entry_id, base, new_record)
    return _stamp(status, entries), changed


def resolve_stuck_testing(status: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Called once, at the very start of a run, before anything new is
    queued: any (entry, base) still `"testing"` was left that way by a run
    that never finished it, so it is resolved to `"failed"` (never guessed
    as `"success"` — a public badge must never claim an outcome nobody
    actually observed) with `error` explaining why, and the resolution
    itself becomes a history entry. Returns the (possibly empty) list of
    (entry_id, base) pairs this touched, so a caller can say so plainly."""
    entries = dict(status.get("entries") or {})
    touched: list[tuple[str, str]] = []
    for entry_id, entry in list(entries.items()):
        bases = dict(entry.get("bases") or {})
        for base, record in list(bases.items()):
            if record.get("state") != "testing":
                continue
            date = _now()
            history = list(record.get("history") or [])
            history_entry = {"date": date, "state": "failed", "engine": record.get("engine"),
                             "suite": record.get("suite"), "duration_s": None, "stage": None,
                             "error": INTERRUPTED_ERROR}
            history = (history + [history_entry])[-HISTORY_LIMIT:]
            updated = dict(record)
            updated.update(state="failed", date=date, since=date, error=INTERRUPTED_ERROR, history=history)
            bases[base] = updated
            touched.append((entry_id, base))
        entry = dict(entry)
        entry["bases"] = bases
        entries[entry_id] = entry
    if not touched:
        return status, []
    return _stamp(status, entries), touched


def write_status_atomic(path: Path, status: dict) -> None:
    """Write-then-rename: a reader (this tool's own next run, the page's
    web server serving the file mid-write) never sees a half-written file,
    and a process killed mid-run leaves the previous, still-valid version
    in place rather than a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".build-status-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class StatusUpdater:
    """Every mutation the on-disk build-status.json ever needs, one
    (entry, base) (or, for resolve_interrupted, the whole file) at a time,
    each written atomically and immediately, with `on_change` called once
    whenever the live state actually moved (item 73: upload on change, not
    on every entry; item 93: a queued/testing transition counts as a
    change too, so a page fetching mid-run does not look stale). A single
    lock around read-modify-write-then-maybe-upload means the parallel
    build path (tools/catalog_conformance.py's job_queue.run_parallel,
    whose worker threads finish entries — and, with item 112's base
    variants, (entry, base) pairs — in any order) can hand every worker's
    calls to the same StatusUpdater without two workers' updates ever
    clobbering each other."""

    def __init__(self, path: Path, on_change=None, *, own_paths: Iterable[str] = (), hostname: str | None = None):
        self.path = path
        self._on_change = on_change
        self._own_paths = tuple(own_paths)
        self._hostname = hostname
        self._lock = threading.Lock()

    def _apply(self, fn) -> bool:
        with self._lock:
            status = load_status(self.path)
            status, changed = fn(status)
            write_status_atomic(self.path, status)
            if changed and self._on_change is not None:
                self._on_change(self.path)
        return changed

    def queue(self, entry_id: str, base: str) -> bool:
        return self._apply(lambda status: mark_queued(status, entry_id, base))

    def start(self, entry_id: str, base: str) -> bool:
        return self._apply(lambda status: mark_testing(status, entry_id, base))

    def record(self, entry_id: str, base: str, result: dict) -> bool:
        return self._apply(lambda status: apply_result(status, entry_id, base, result,
                                                        own_paths=self._own_paths, hostname=self._hostname))

    def resolve_interrupted(self) -> list[tuple[str, str]]:
        touched: list[tuple[str, str]] = []

        def fn(status: dict) -> tuple[dict, bool]:
            nonlocal touched
            new_status, touched_pairs = resolve_stuck_testing(status)
            touched = touched_pairs
            return new_status, bool(touched_pairs)

        self._apply(fn)
        return touched
