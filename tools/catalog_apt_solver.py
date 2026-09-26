#!/usr/bin/env python3
"""catalog_apt_solver — can apt actually install this catalogue entry, on
top of what the engine already puts in an image, on this base and suite?

    python3 tools/catalog_apt_solver.py
    python3 tools/catalog_apt_solver.py --json

tools/catalog_apps_audit.py answers the first half of "can a person pick
this application and have it work": does the name exist in the archive at
all. It never runs apt's own dependency solver, so it cannot see the other
half — a name that exists but that apt cannot actually install alongside
what the engine already installs: an unsatisfiable dependency, or a
conflict with the base package set. That is the failure a person hits
forty minutes into a real build, on their own machine, after the name
itself passed every check that only ever looked at package *existence*.

This tool answers that second half, for real, with apt's own solver: for
every catalogue entry the archive genuinely has (reusing
tools/catalog_apps_audit.py's own fetch_index() — never re-deciding
existence with a second copy of that logic), on every (base, suite) it
claims, it asks a real `apt-get install --simulate --no-remove` inside a
throwaway container of that suite's own official base image, with the
engine's own apt sources configured (bases/<base>/sources.tmpl) and the
engine's own minimal profile's resolved package set already installed —
the floor every image has, regardless of which of the 9 machine profiles a
person actually builds (profiles/minimal.yml: "Parent of every profile").
`--no-remove` matters: without it, apt would silently offer to remove a
floor package to satisfy a conflicting one and call that success, which is
exactly the kind of surprise a person's build should never spring on them
without it being reported.

Approximation, named honestly: the installed floor is minimal.yml's own
resolved package list — not literally byte-for-byet what every real build
produces. Two things it does not capture: (1) profiles/minimal.yml's own
`desktop-core` bundle maps `gnome-desktop` to nothing in
bases/*/packages.map ("provided by the upstream metapackage (mod 05) until
packages/ ships" — packages.map's own comment) — the actual desktop
environment a real image ships is installed by a separate mechanism this
tool has no access to, so the floor here never actually has a desktop
environment in it; a catalogue entry that would only conflict with *that*
(an alternate display manager, say) cannot be caught here. (2) a small
number of the minimal profile's own resolved names do not exist in the
archive at all under the hardcoded English locale
(tools/catalog_conformance.resolved_install_packages's own `["en"]`, not
something this tool controls) — `hunspell-en`, `libreoffice-l10n-en`,
`firefox-esr-l10n-en` as of this writing — and are simply left out of the
installed floor, named in the report so the omission is visible rather
than silently swallowed by an install command that would otherwise fail
outright on an unrelated missing name.

Outcomes, reported separately, never merged:
    clean            apt would install it (and whatever it pulls in) cleanly
    unsatisfiable     an unmet Depends/PreDepends — the missing name is reported
    conflict          installing it would need to remove something from the
                      floor (a real Conflicts/Breaks, or a file collision) —
                      the reason apt gave is reported
    not_in_archive    tools/catalog_apps_audit.py's own finding, reused
                      as-is, never recomputed
    could_not_check   no container runtime, no network, an image that would
                      not pull, or an apt-get update/floor install that
                      itself failed — never "clean" and never a "conflict"
                      or "unsatisfiable" either; an infrastructure problem
                      must never read as a catalogue problem

Never touches this checkout's own system: every apt-get call happens inside
a throwaway container (podman, or docker via the same `host_resources.
container_engine()` this repository's other tools already use to pick
one), removed in a `finally` whether the run succeeded or not — nothing is
installed for real, no ISO is built, and no container or image is left
running. Pulled base images are left in the local image cache (not
"leaving something behind" in the sense that matters — the same image is
reused by the next run) but every container this tool starts is always
removed.

Never hits a container runtime or the network in its own test suite
(tests/unit/test_catalog_apt_solver.py); the "runner" (podman lifecycle)
and the archive fetcher are both plain arguments a test replaces with an
in-memory fake — see FakeRunner there for the exact interface a real
PodmanRunner implements.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import importlib.util
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCH = "amd64"
SIMULATE_TIMEOUT_S = 60
UPDATE_TIMEOUT_S = 300
FLOOR_INSTALL_TIMEOUT_S = 900
PULL_TIMEOUT_S = 600
CONTAINER_START_TIMEOUT_S = 60
CONTAINER_STOP_TIMEOUT_S = 30

# Official image repository for each base; the suite name is used as the
# tag as-is (confirmed against the real registry for every (base, suite)
# this checkout currently supports: debian:trixie, ubuntu:noble,
# ubuntu:resolute all pull cleanly). A suite this checkout adds later that
# has no matching official tag fails the image pull outright, reported as
# "could not check" for that (base, suite) — never silently substituted
# for a different tag.
IMAGE_REPOSITORY = {"debian": "docker.io/library/debian", "ubuntu": "docker.io/library/ubuntu"}

STATUS_CLEAN = "clean"
STATUS_UNSATISFIABLE = "unsatisfiable"
STATUS_CONFLICT = "conflict"
STATUS_NOT_IN_ARCHIVE = "not_in_archive"
STATUS_COULD_NOT_CHECK = "could_not_check"
STATUS_ERROR = "error"


def _load_module(name: str, relative: str):
    """tests/unit/test_catalog_conformance.py's own pattern for loading a
    sibling tool as a module: register it in sys.modules *before* exec, so
    a dataclass defined inside it can resolve its own module for
    annotations."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render_manifest = _load_module("render_manifest_for_apt_solver", "tools/render_manifest.py")
catalog_conformance = _load_module("catalog_conformance_for_apt_solver", "tools/catalog_conformance.py")
catalog_apps_audit = _load_module("catalog_apps_audit_for_apt_solver", "tools/catalog_apps_audit.py")
host_resources = _load_module("host_resources_for_apt_solver", "tools/host_resources.py")


class SolverError(Exception):
    """Setup itself (config, catalogue, base adapters) could not be read —
    nothing to check per-entry."""


# ------------------------------------------------------------- apt sources
def render_sources(template_text: str, base_env: dict, suite: str) -> str:
    """bases/<base>/sources.tmpl, substituted with base_env's own values and
    `suite` — the exact apt configuration the real engine's chroot is
    given, reused as a template rather than a URL/suite layout of this
    tool's own invention. `${SUITE}-updates`/`-backports`/`-security` come
    from the template itself, unaffected here."""
    security_mirror = base_env.get("SECURITY_MIRROR") or base_env["APT_MIRROR"]
    substitutions = {
        "APT_MIRROR": base_env["APT_MIRROR"], "SECURITY_MIRROR": security_mirror,
        "COMPONENTS": base_env["COMPONENTS"], "KEYRING": base_env["KEYRING"], "SUITE": suite,
    }
    text = template_text
    for key, value in substitutions.items():
        text = text.replace(f"${{{key}}}", value)
    return text


# --------------------------------------------------------------- the floor
def floor_packages(base_id: str, archive_names: frozenset) -> tuple[list[str], list[str]]:
    """profiles/minimal.yml resolved for `base_id` (catalog_conformance.
    resolved_install_packages — the same helper tools/catalog_conformance.py's
    own `check` mode uses), split into (present, absent) against the real
    archive `archive_names` names. `absent` is never installed — apt-get
    install would refuse the whole floor for one bad name — and is named in
    the report rather than silently dropped (see the module docstring's
    English-locale caveat)."""
    merged, _chain = render_manifest.resolve_profile("minimal")
    manifest = {"base": base_id, "arch": DEFAULT_ARCH}
    wanted = catalog_conformance.resolved_install_packages(manifest, merged)
    present = [p for p in wanted if p in archive_names]
    absent = [p for p in wanted if p not in archive_names]
    return present, absent


# ------------------------------------------------------------- classifying
_UNMET_HEADER = "have unmet dependencies"
_CONFLICT_MARKERS = ("Conflicts:", "Breaks:")
_DEPENDS_MARKERS = ("Depends:", "PreDepends:")


def classify_simulation(returncode: int, stdout: str, stderr: str) -> tuple[str, str | None]:
    """apt-get install --simulate --no-remove's own exit code and text is
    the entire input; this never re-runs or second-guesses it. `returncode
    == 0` is always "clean" — simulate mode makes no changes either way,
    so that verdict is apt's own, taken as-is.

    A failure's "have unmet dependencies:" block lists one line per
    offending package, each naming the relation that could not be
    satisfied. A line naming Conflicts/Breaks means installing this entry
    would require removing something already there (the floor, with
    --no-remove in force) — "conflict"; a line naming
    Depends/PreDepends means no candidate satisfies it at all —
    "unsatisfiable". Both can appear together; Conflicts/Breaks wins the
    classification (it is the more specific, more actionable of the two:
    naming exactly what would have to go), but every offending line is
    kept in the detail either way, never only the first.

    A second, textually distinct failure --no-remove itself produces (real
    example: installing `exim4-daemon-light` in a container that already has
    `postfix` — no "unmet dependencies" block at all): apt happily plans
    the whole transaction, including removing the conflicting package, and
    only then refuses with "E: Packages need to be removed but remove is
    disabled." — the packages actually named under its own "The following
    packages will be REMOVED:" header are exactly what this entry conflicts
    with in the floor, and are reported as such.

    Anything else with a non-zero return is "error" — apt failed in a way
    this does not recognise (a transient archive hiccup inside the
    container, a genuinely new message format); the raw tail is the
    detail, on purpose, rather than folding an unrecognised failure into
    one of the two named outcomes and reporting something this did not
    actually establish."""
    combined = stdout + "\n" + stderr
    if returncode == 0:
        return STATUS_CLEAN, None

    lines = combined.splitlines()

    def _block_after(header: str) -> list[str]:
        block: list[str] = []
        in_block = False
        for line in lines:
            if header in line:
                in_block = True
                continue
            if in_block:
                if not line.strip() or not line.startswith(" "):
                    in_block = False
                    continue
                block.append(line.strip())
        return block

    unmet = _block_after(_UNMET_HEADER)
    if unmet:
        has_conflict = any(marker in u for u in unmet for marker in _CONFLICT_MARKERS)
        has_depends = any(marker in u for u in unmet for marker in _DEPENDS_MARKERS)
        detail = "; ".join(unmet)
        if has_conflict:
            return STATUS_CONFLICT, detail
        if has_depends:
            return STATUS_UNSATISFIABLE, detail
        return STATUS_ERROR, detail

    if "need to be removed but remove is disabled" in combined:
        removed = _block_after("The following packages will be REMOVED:")
        detail = ("would require removing from the base set: " + ", ".join(removed)) if removed \
            else "apt would need to remove something from the base set to satisfy this (remove is disabled)"
        return STATUS_CONFLICT, detail

    if "Trying to overwrite" in combined or "trying to overwrite" in combined:
        return STATUS_CONFLICT, next((l.strip() for l in lines if "verwrite" in l), combined.strip()[-400:])

    tail = "\n".join(l for l in lines if l.strip())[-800:]
    return STATUS_ERROR, tail or f"apt-get exited {returncode} with no output"


# ------------------------------------------------------------- the runner
@dataclasses.dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


class Runner:
    """What run_solver() needs from a container for one (base, suite):
    pull the image, bring it up with the engine's own apt sources and the
    floor installed, simulate one package at a time, and tear itself down.
    A real PodmanRunner and a test's FakeRunner both implement exactly
    this."""

    def pull(self) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def configure_sources(self, sources_text: str) -> ExecResult:
        raise NotImplementedError

    def apt_update(self) -> ExecResult:
        raise NotImplementedError

    def install_floor(self, packages: list[str]) -> ExecResult:
        raise NotImplementedError

    def simulate(self, package: str) -> ExecResult:
        raise NotImplementedError

    def cleanup(self) -> None:
        raise NotImplementedError


_POLICY_RC_D = "#!/bin/sh\nexit 101\n"  # standard Debian/Ubuntu convention: decline every service start/stop
# inside a container with no init running, so a postinst that tries to start a service (ufw, auditd,
# unattended-upgrades all do) never hangs waiting for one instead of failing or succeeding promptly.


class PodmanRunner(Runner):
    """One throwaway container of `image`, driven with `engine exec` (no
    long-lived Python<->container connection, no extra dependency beyond
    the `podman`/`docker` binary this repository's other tools already
    require) - torn down in cleanup() unconditionally."""

    def __init__(self, engine: str, image: str) -> None:
        self.engine = engine
        self.image = image
        self.name = f"synos-apt-solver-{uuid.uuid4().hex[:12]}"
        self._started = False

    def _run(self, argv: list[str], *, timeout: float) -> ExecResult:
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, exc.stdout or "", (exc.stderr or "") + f"\ntimed out after {timeout}s")
        except OSError as exc:
            return ExecResult(127, "", f"{type(exc).__name__}: {exc}")
        return ExecResult(completed.returncode, completed.stdout, completed.stderr)

    def _exec(self, script: str, *, timeout: float) -> ExecResult:
        return self._run([self.engine, "exec", self.name, "bash", "-lc", script], timeout=timeout)

    def pull(self) -> None:
        result = self._run([self.engine, "pull", self.image], timeout=PULL_TIMEOUT_S)
        if result.returncode != 0:
            raise SolverError(f"could not pull {self.image}: {(result.stderr or result.stdout).strip()[-500:]}")

    def start(self) -> None:
        result = self._run([self.engine, "run", "-d", "--name", self.name, self.image, "sleep", "infinity"],
                           timeout=CONTAINER_START_TIMEOUT_S)
        if result.returncode != 0:
            raise SolverError(f"could not start a container of {self.image}: {(result.stderr or result.stdout).strip()[-500:]}")
        self._started = True
        # Item: never let a postinst script hang this run waiting on a service
        # manager that does not exist inside this container.
        policy = self._exec(f"cat > /usr/sbin/policy-rc.d <<'SYNOS_EOF'\n{_POLICY_RC_D}SYNOS_EOF\n"
                            "chmod +x /usr/sbin/policy-rc.d", timeout=30)
        if policy.returncode != 0:
            raise SolverError(f"could not install policy-rc.d in {self.name}: {policy.stderr.strip()[-500:]}")

    def configure_sources(self, sources_text: str) -> ExecResult:
        script = ("cat > /etc/apt/sources.list.d/synos.sources <<'SYNOS_EOF'\n" + sources_text + "SYNOS_EOF\n"
                 "rm -f /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/debian.sources; "
                 ": > /etc/apt/sources.list")
        return self._exec(script, timeout=30)

    def apt_update(self) -> ExecResult:
        return self._exec("apt-get update -qq", timeout=UPDATE_TIMEOUT_S)

    def install_floor(self, packages: list[str]) -> ExecResult:
        if not packages:
            return ExecResult(0, "(no floor packages to install)", "")
        quoted = " ".join(shlex.quote(p) for p in packages)
        script = f"export DEBIAN_FRONTEND=noninteractive; apt-get install -y --no-install-recommends {quoted}"
        return self._exec(script, timeout=FLOOR_INSTALL_TIMEOUT_S)

    def simulate(self, package: str) -> ExecResult:
        return self._exec(f"apt-get install -s -y --no-remove {shlex.quote(package)}", timeout=SIMULATE_TIMEOUT_S)

    def cleanup(self) -> None:
        if self._started:
            self._run([self.engine, "rm", "-f", self.name], timeout=CONTAINER_STOP_TIMEOUT_S)
            self._started = False


# --------------------------------------------------------------- the audit
def solve_one_combo(base: str, suite: str, entries: list[dict], *, archive, base_env: dict,
                    runner: Runner) -> tuple[list[dict], str | None, list[str]]:
    """Everything for one (base, suite): configure it, install its floor,
    simulate every entry's package, and always report either results or a
    single could-not-check reason for the whole combo — never a mix (a
    floor that failed to install makes every entry's own simulate result
    meaningless, not just absent). The third element is the floor's own
    absent-from-the-archive names (module docstring's English-locale
    caveat) — never folded into `results`/checks_total, which count
    catalogue entries only."""
    results: list[dict] = []
    template_text = (ROOT / "bases" / base / "sources.tmpl").read_text(encoding="utf-8")
    sources_text = render_sources(template_text, base_env, suite)

    setup_result = runner.configure_sources(sources_text)
    if setup_result.returncode != 0:
        return [], f"could not configure apt sources: {setup_result.stderr.strip()[-400:]}", []

    update_result = runner.apt_update()
    if update_result.returncode != 0:
        return [], f"apt-get update failed: {(update_result.stderr or update_result.stdout).strip()[-400:]}", []

    floor_present, floor_absent = floor_packages(base, archive.names)
    floor_result = runner.install_floor(floor_present)
    if floor_result.returncode != 0:
        return [], f"could not install the minimal-profile floor: {(floor_result.stderr or floor_result.stdout).strip()[-400:]}", floor_absent

    for entry in entries:
        pkg = entry["package"]
        if pkg not in archive.names:
            results.append({"id": entry["id"], "package": pkg, "base": base, "suite": suite,
                            "status": STATUS_NOT_IN_ARCHIVE, "detail": None})
            continue
        sim = runner.simulate(pkg)
        status, detail = classify_simulation(sim.returncode, sim.stdout, sim.stderr)
        results.append({"id": entry["id"], "package": pkg, "base": base, "suite": suite,
                        "status": status, "detail": detail})

    return results, None, floor_absent


def run_solver(entries: list[dict], matrix: dict[str, list[str]], base_envs: dict[str, dict], *,
               fetcher, runner_factory, arch: str = DEFAULT_ARCH, archive_cache: dict | None = None) -> dict:
    """The whole run: group every claimed (entry, base, suite) by combo,
    fetch each combo's real archive index exactly once
    (tools/catalog_apps_audit.fetch_index — never a second copy of that
    logic), and solve every combo that has one. `runner_factory(base,
    suite) -> Runner` is called once per combo that needs solving (never
    for a combo with no claimed entries, never for one whose archive index
    could not even be fetched)."""
    archive_cache = {} if archive_cache is None else archive_cache
    by_combo: dict[tuple[str, str], list[dict]] = {}
    for entry in entries:
        for base, suites in (entry.get("available") or {}).items():
            if base not in matrix:
                continue
            for suite in suites:
                if suite not in matrix[base]:
                    continue
                by_combo.setdefault((base, suite), []).append(entry)

    results: list[dict] = []
    could_not_check: list[dict] = []
    floor_gaps: list[dict] = []
    for (base, suite), combo_entries in sorted(by_combo.items()):
        archive = catalog_apps_audit.fetch_index(base, suite, arch, base_envs[base], fetcher=fetcher, cache=archive_cache)
        if not archive.ok:
            could_not_check.append({"base": base, "suite": suite, "reason": f"archive index unreachable: {archive.error}",
                                    "entries_affected": len(combo_entries)})
            continue

        runner = runner_factory(base, suite)
        try:
            try:
                runner.pull()
                runner.start()
            except SolverError as exc:
                could_not_check.append({"base": base, "suite": suite, "reason": str(exc),
                                        "entries_affected": len(combo_entries)})
                continue
            combo_results, failure_reason, floor_absent = solve_one_combo(
                base, suite, combo_entries, archive=archive, base_env=base_envs[base], runner=runner)
            if floor_absent:
                floor_gaps.append({"base": base, "suite": suite, "packages": floor_absent})
            if failure_reason:
                could_not_check.append({"base": base, "suite": suite, "reason": failure_reason,
                                        "entries_affected": len(combo_entries)})
            else:
                results.extend(combo_results)
        finally:
            runner.cleanup()

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "entries_total": len(entries),
        "checks_total": len(results),
        "counts": counts,
        "results": results,
        "could_not_check": could_not_check,
        "floor_gaps": floor_gaps,
    }


def exit_code(report: dict) -> int:
    counts = report["counts"]
    if counts.get(STATUS_UNSATISFIABLE) or counts.get(STATUS_CONFLICT) or counts.get(STATUS_ERROR):
        return 1
    if report["could_not_check"]:
        return 2
    return 0


# ------------------------------------------------------------ reporting
def summary_text(report: dict) -> str:
    counts = report["counts"]
    lines = [f"catalog_apt_solver: {report['entries_total']} catalogue entr{'y' if report['entries_total'] == 1 else 'ies'}, "
             f"{report['checks_total']} (entry, base, suite) claim(s) run through apt's own solver"]
    lines.append(", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing was checked")

    for status, label in ((STATUS_UNSATISFIABLE, "UNSATISFIABLE"), (STATUS_CONFLICT, "CONFLICTS WITH THE BASE SET"),
                          (STATUS_ERROR, "APT FAILED UNRECOGNISABLY")):
        offenders = [r for r in report["results"] if r["status"] == status]
        if offenders:
            lines.append(f"{label}: {len(offenders)}")
            for r in offenders:
                lines.append(f"  {r['id']} ({r['package']}) on {r['base']}:{r['suite']}: {r['detail']}")

    if report["could_not_check"]:
        affected = sum(c["entries_affected"] for c in report["could_not_check"])
        lines.append(f"COULD NOT CHECK: {len(report['could_not_check'])} combo(s), "
                     f"leaving {affected} claimed entr{'y' if affected == 1 else 'ies'} unverified:")
        for c in report["could_not_check"]:
            lines.append(f"  {c['base']}:{c['suite']}: {c['reason']}")

    if report.get("floor_gaps"):
        lines.append("note: the installed floor (profiles/minimal.yml, resolved) is missing name(s) "
                     "the real archive does not have under the hardcoded English locale — excluded from "
                     "the floor, not a finding about any catalogue entry:")
        for g in report["floor_gaps"]:
            lines.append(f"  {g['base']}:{g['suite']}: {', '.join(g['packages'])}")

    stats = report.get("run_stats")
    if stats:
        lines.append(f"ran {stats['combos']} base/suite combo(s) in {stats['duration_s']}s "
                     f"({stats.get('images_pulled', 0)} image(s) pulled)")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog_apt_solver", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=ROOT / "profiles" / "catalog.apps.yml")
    parser.add_argument("--arch", default=DEFAULT_ARCH)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--only", help="comma-separated base:suite combos to run (default: every claimed combo)")
    args = parser.parse_args(argv)

    engine = host_resources.container_engine()
    if not engine:
        print("error: podman or docker is required (host_resources.container_engine found neither)", file=sys.stderr)
        return 2

    try:
        entries = catalog_apps_audit.load_catalog_entries(args.catalog)
        matrix, envs = catalog_apps_audit.engine_matrix(ROOT)
    except catalog_apps_audit.AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {tuple(pair.split(":", 1)) for pair in args.only.split(",")}
        matrix = {b: [s for s in suites if (b, s) in wanted] for b, suites in matrix.items()}

    pulled_images: set[str] = set()

    def runner_factory(base: str, suite: str) -> PodmanRunner:
        image = f"{IMAGE_REPOSITORY[base]}:{suite}"
        pulled_images.add(image)
        return PodmanRunner(engine, image)

    fetcher = catalog_conformance._http_get
    start = time.monotonic()
    report = run_solver(entries, matrix, envs, fetcher=fetcher, runner_factory=runner_factory, arch=args.arch)
    report["run_stats"] = {"combos": len(pulled_images), "images_pulled": len(pulled_images),
                           "duration_s": round(time.monotonic() - start, 1)}
    report["catalog_path"] = str(args.catalog)
    report["generated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(summary_text(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
