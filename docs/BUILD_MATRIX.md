# The catalog build matrix

`tools/build_matrix.py` is the unattended proof that the whole catalog
builds: every entry in `bundle-catalog/index.yml`, and every base/suite
combination this engine supports ("the distro cores"), each built through
`tools/synos` exactly the way a person builds it — nothing here talks to
the engine any other way. `tools/smoke_test.py` then boots the ISO a
successful build produced and checks that the appliance it promised is
real. `.github/workflows/catalog-matrix.yml` runs both nightly.

This is what feeds `bundle-catalog/build-status.yml`, the file
`tools/export_catalog.py` merges into `bundle_catalog[].build_status` so a
front end can show "last built" honestly instead of only the maintainer's
`verified: true` claim (docs/BUNDLE.md, "The bundle catalog").

`tools/build_matrix.py` proves *this checkout's* `bundle-catalog/` builds.
`tools/catalog_conformance.py`, covered below ("Proving the published
catalog"), is a separate, smaller tool that proves the catalog a real
person actually downloads — the Studio site's exported JSON — still
builds, which is not the same claim and cannot be checked by only looking
at this repository.

## Running it

```bash
python3 tools/build_matrix.py --list                 # every planned target, nothing touched
python3 tools/build_matrix.py --dry-run               # the plan plus the exact command each target would run
python3 tools/build_matrix.py --only web-server-nginx  # one target
python3 tools/build_matrix.py --kind core              # only the base/suite cores
python3 tools/build_matrix.py --base ubuntu --suite noble
python3 tools/build_matrix.py --resume                 # a nightly run: skip what already succeeded
```

Useful flags: `--jobs N` or `--jobs auto` (more than one build at a time;
"Isolation and parallelism" below explains how many is safe and what each
one gets), `--timeout MINUTES` (per build, default 90), `--image` or
`$SYNOS_BUILDER_IMAGE` (use one builder image for every target instead of
the engine building one per base and suite from `bases/<base>/Containerfile`),
`--pull` (fetch the published builder image instead of building it locally),
`--no-smoke` (skip `tools/smoke_test.py` even after a successful build),
`--output DIR` (default `dist/matrix/`).

Exit code: 0 when every target this run attempted succeeded, 1 when at
least one failed or timed out, 2 when the host itself refuses to start
(not enough disk, memory or CPUs for `--jobs`, no podman or docker).

## What it proves, and what it does not

A "success" means `tools/synos build` exited 0 for that bundle or manifest
on this engine, on this host, today: the base bootstrapped, every package
the profile named resolved and installed, the image assembled and produced
an ISO. It does not mean the appliance inside works — that is what the
smoke test adds when it can run (see below), and even the smoke test
cannot exercise anything that needs a real network, a signed-in package
mirror or an installed system's real first boot (docs/BUNDLE.md's own
"most appliances were built, run and hit with a real request" note is a
stronger claim than an unattended matrix run can make by itself; a maintainer
still stands behind `verified: true`, `bundle-catalog/build-status.yml` is
the engine's own record of the last time the matrix actually built it).

## What it costs

Per target: the same as building it by hand — 40+ GB free under the
checkout, another 30+ GB in the container runtime's own storage, and,
uncached, on the order of 40 minutes (`bundle_launcher.sh`'s own estimate;
a warm build cache is faster — "Isolation and parallelism" below). Fifty-odd
catalog entries plus four cores, one at a time, is the better part of a day;
`--jobs` shortens that at the cost of `--jobs` times the disk, memory and
CPU a build needs. This is why the matrix's own job in CI only runs on a
self-hosted runner (below) and why `--resume` exists: a nightly run only
rebuilds what changed or previously failed.

## Resuming

`dist/matrix/report.json` is written after every target finishes, not only
at the end, so killing a run loses at most the target that was mid-build.
`--resume` re-reads that file: a target whose last recorded result was
`success` at the exact checksum being planned now (the bundle folder's
bytes for a catalog entry, the generated manifest for a core) is skipped;
everything else — never run, failed, timed out, or changed since — is
retried. Without `--resume`, every planned target runs again from scratch.

## Isolation and parallelism

`tools/synos build <bundle>` applies the bundle's files (`profiles/`,
`manifests/`, `branding/`, `keys/`, `answers/`) into whatever checkout its
own `tools/synos` script lives in before building — exactly right for a
person building by hand, wrong for an unattended run: invoked as this
checkout's own `tools/synos`, it would leave one `profiles/<id>.yml` behind
per catalog entry, dirtying the very checkout the matrix runs from (and
tripping `tests/unit/test_bundle_catalog.py`'s own guard against a catalog
entry shadowing an engine profile). `tools/scratch_checkout.py` fixes this:
every build runs inside a disposable `git worktree` of this checkout — a
fast, space-shared checkout of the current commit (deliberately *not* any
uncommitted local edits: a proof run proves what is about to be committed)
— never the checkout itself, removed again once the build using it is
done, including when the run is killed (`atexit`/`SIGTERM`/`SIGINT`
cleanup; `git status --porcelain` here is unchanged by a run either way,
proven by `tests/unit/test_build_matrix.py`'s and
`tests/unit/test_catalog_conformance.py`'s own `IsolationTests`).

`--jobs` runs that many builds at once, each on its own worker. Each worker
creates **one** scratch checkout the first time it is handed a target and
reuses it for every target it builds after that, for as long as the run
lasts — never created fresh per target, never shared with another worker.
That reuse is this project's build cache (apt lists, downloaded packages:
whatever of a build's own `.build/` survives between builds) and its
isolation in the same move: two workers never share one checkout's apt
state, which is exactly the collision a shared cache volume caused this
project once before (two `./build.sh` runs on one bundle, the reason
`bundle_launcher.sh` carries its own `dist/build.pid` guard today). A
worker whose attempt at a target raises outright — not an ordinary failed
build, which always comes back as a normal result, but a crash in the
scheduling code around it — does not wedge the run: `tools/job_queue.py`
requeues that one target once for another worker to attempt, and only
records it as failed if the retry also raises. Each target's log still
streams to its own file as it happens (`--log`, unaffected by how many
workers are running; nothing here buffers a target's log until it finishes),
so watching one target in a parallel run never shows another's output.

`--jobs auto` derives the safe count from this machine rather than
guessing: free disk under the checkout (40 GB/build), free space in the
container runtime's own storage (30 GB/build), memory (4 GB/build — a
documented assumption, not a measurement: this engine publishes no number,
`tools/host_resources.py` names it so it can be argued with) and CPUs (2/build,
the one factor floored at 1 regardless of core count, since running one
build with fewer cores than assumed only slows it down — the other three
are not floored, so a machine that cannot feed even one build is refused
outright, `--jobs` value or not). The smallest of the four wins; an
explicit `--jobs` above it is refused with the reason, not silently capped:

```
--jobs 11 exceeds what this machine can safely feed (10): disk allows 10
(400 GB free / 40 GB per build), memory allows 16 (64 GB / 4 GB per build),
CPUs allow 16 (32 cores / 2 per build); pass --jobs auto to use 10
```

`tools/catalog_conformance.py build` takes the same `jobs` key in its
config (`"1"` or `"auto"`, default `"1"`) and the same treatment: one
scratch checkout per worker, reused across whatever entries that worker is
handed, `--jobs auto`'s formula unchanged.

### The queue seam

`tools/job_queue.py`'s `LocalJobQueue` is in-process and in-memory — gone
when the run ends — behind a small interface (`take()` the next target,
`mark_running()`, `record_result()`, `requeue()`) neither
`tools/build_matrix.py` nor `tools/catalog_conformance.py` reaches past.
Nothing here builds it, but that seam is where a later hosted build service
("What a hosted build service would still need", below) would put a real
queue shared across machines: a remote implementation leases the next
target from a service over HTTP in `take()`, renews the lease in
`mark_running()`, posts the outcome and releases the lease in
`record_result()`, and abandons it early in `requeue()` — without changing
a line of `run_parallel` or either caller, which only ever call those four
methods on whatever `JobQueue` they were handed.

## The report

`dist/matrix/report.json` (machine-readable) and `dist/matrix/summary.txt`
(a short table: target, result, duration, ISO size) are written under
`--output` (default `dist/matrix/`). One example entry:

```json
{
  "id": "web-server-nginx",
  "kind": "catalog",
  "base": "ubuntu",
  "suite": "noble",
  "profile": "web-server-nginx",
  "checksum": "5f2c...",
  "engine": "0.2.0",
  "start": "2026-09-23T03:00:12+00:00",
  "end": "2026-09-23T03:41:07+00:00",
  "duration_s": 2455.3,
  "exit_code": 0,
  "status": "success",
  "iso": {
    "path": "dist/matrix/targets/web-server-nginx/synos-web-server-nginx-1.0.0-amd64.iso",
    "size": 2415919104,
    "sha256": "9c1f...":
  },
  "log_path": "dist/matrix/targets/web-server-nginx/build.log",
  "log_tail": [],
  "smoke": {
    "status": "passed",
    "checks": [
      {"name": "default-target", "passed": true, "target": "graphical.target"},
      {"name": "open-ports", "passed": true, "expected": ["22/tcp", "443/tcp", "80/tcp"], "found": ["22/tcp", "443/tcp", "80/tcp"]},
      {"name": "service-unit:nginx", "passed": true, "state": "enabled"},
      {"name": "file:/srv/www/index.html", "passed": true, "mode": "644", "expected_mode": "0644", "size": 143}
    ],
    "transcript": "dist/matrix/targets/web-server-nginx/smoke-serial.log"
  }
}
```

A failed target keeps the same shape with `status` one of `invalid`, `host`,
`build_failed`, `timeout` or `error`, `iso: null`, and `log_tail` holding the
last lines of `build.log`.

## The smoke test

`tools/smoke_test.py` boots the built ISO headless (`qemu-system-x86_64`,
no disk) and, over a root shell on the serial console, checks:

- the live system reaches its default systemd target (`systemctl get-default`,
  then polls `systemctl is-active` on it);
- the ports the shipped first-boot firewall script
  (`/usr/libexec/synos-first-boot-services`) would open match the profile's
  `security.open_ports` exactly. This checks the *shipped configuration*,
  not live enforcement: that script only runs on an installed system's real
  first boot (`ConditionKernelCommandLine=!rd.synos.live`), never inside the
  live session this test boots, so the ports are never actually open here —
  the report says so in every `open-ports` check's `note`;
- every `software.services` entry has a quadlet-generated systemd unit
  (`<name>.service`, from `/etc/containers/systemd/<name>.container`) that
  exists and is enabled — not that the container's image was pulled, which
  needs the network;
- every `software.files` entry is present with the mode the profile asked
  for.

It sidesteps the themed, graphical GRUB menu entirely: the live kernel and
initrd are extracted straight out of the ISO (`xorriso -osirrox`) and booted
directly with `-kernel`/`-initrd`, with a root debug shell wired to the
serial console the same way the full acceptance suite's
`tests/framework/grub.py` does for its own debug boots (`console=ttyS0`,
`systemd.debug_shell=ttyS0`, the getty on that tty masked) — without that
suite's screenshot-driven menu navigation, which this headless appliance
check does not need. `qemu-system-x86_64` and `xorriso` missing on
`PATH` is a clean `"skipped"` result, never a failure.

### How it was tested without booting anything

Every parsing and comparison rule the smoke test applies — recovering a
command's output from a raw serial transcript, matching the shipped
firewall script's `ufw allow` lines against a profile's `open_ports`,
reading `systemctl is-enabled`'s answer, reading a `stat` line's mode — is a
small, pure function, and `tests/unit/test_smoke_test.py` exercises every
one of them against fixed fixtures: a fake firewall script's text, a fake
`systemctl` answer, a fake `stat -c '%a %s'` line, and a fake serial
transcript carrying the shell's own echoed input around the markers this
tool sends. A `FakeSession` stands in for the QEMU serial connection in the
handful of tests that exercise a whole check (`check_open_ports`,
`check_service_unit`, `check_shipped_file`) end to end. No test in that file
starts `qemu-system-x86_64`; `tests/unit/test_build_matrix.py` likewise
never shells out to a real build, standing a small fake script in for
`tools/synos` (success, failure and hang, to cover the timeout path) so the
report shape, the failure path and `--resume` are proven without touching a
container runtime. `tests/unit/test_catalog_conformance.py` follows the
same rule for the conformance tool: its own fake `tools/synos` covers
`build`, and `check`'s archive and registry lookups go through injectable
`fetcher`/`inspector` parameters a test replaces with an in-memory fake
`Packages.gz` and a fake `skopeo` answer — no test in that file opens a
real network connection either. `tests/unit/test_scratch_checkout.py`,
`tests/unit/test_host_resources.py` and `tests/unit/test_job_queue.py`
carry the same rule down into the isolation and parallelism layer
underneath both tools: the first two run the real `git`/disk-usage/CPU-count
machinery against fixed numbers or this actual repository (worktree
creation is not a build), and the third runs `tools/job_queue.py`'s
scheduler — several fake builds at once, one made to crash and retried,
several made to crash at once — entirely against in-memory fakes, never a
real `tools/synos`.

## Proving the published catalog: tools/catalog_conformance.py

`tools/build_matrix.py` builds what is in this checkout. It cannot catch a
catalog that drifted after the Studio site last deployed, or a package or
container image an upstream archive quietly dropped between deploys.
`tools/catalog_conformance.py` is a separate, smaller tool for exactly
that: it downloads the catalog a real person gets — the Studio site's own
exported JSON (`tools/export_catalog.py`'s `bundle_catalog` key, typically
served at `<site>/data/catalog.json`) — and builds every entry from that
download, not from `bundle-catalog/`. It shares `tools/smoke_test.py` and
the per-target report shape (`id`, `kind`, `base`, `suite`, `status`,
`iso`, `log_tail`, `smoke`, ...) with the matrix, but is otherwise
independent code, a different trigger, and, in production, a different
machine: a plain, unattended checkout, not a developer's.

```bash
python3 tools/catalog_conformance.py build --config conformance.yml
python3 tools/catalog_conformance.py check --config conformance.yml
python3 tools/catalog_conformance.py build --config conformance.yml --dry-run   # fetch and list, build nothing
```

`packaging/catalog-conformance/conformance.example.yml` documents every
config key: the catalog URL, a working directory, the same disk thresholds
the matrix guards on, how many entries one `build` run attempts and its
per-entry timeout, and an optional URL to `POST` the finished report to.

For each catalog entry, `build` writes the entry's own `files` (exactly as
`tools/export_catalog.py` exports them) into a temporary bundle directory,
fills in whatever a real person filling in the Studio form would have
filled in — deterministically from the entry's id, today just the
`bundle.json`/manifest `name` when either is absent — and then validates,
builds and smoke-tests it exactly the way `tools/synos build` and the
matrix do. An entry that is missing something no default can supply (no
`manifest` key, a manifest missing `base` or `profile`) is recorded with
status `unbuildable` and the reason, never silently dropped from the
report: the point of this tool is to find exactly this kind of problem
before a person does.

## The cheap early warning: `catalog_conformance.py check`

A full `build` pass over fifty-odd entries costs the better part of a
machine-day (the same 40+ minutes per entry the matrix pays). The failure
this tool exists to catch early — a package or container image tag
disappearing from an archive — does not need a build to detect. `check`
resolves every package every catalogued profile names against its pinned
base and suite's own archive (the real `Packages` index under
`<APT_MIRROR>/dists/<suite>/<component>/binary-<arch>/Packages.gz`, one
fetch per base/suite/component shared across every entry that uses it, not
a guess from the local package map) and checks every pinned container
image tag the profile's `software.services` names still resolves in its
registry (`skopeo inspect`, skipped honestly rather than guessed when
`skopeo` is not installed). It runs in minutes and reports, per entry,
which packages and image tags have gone missing (`status: "drift"`) — this
is the mode meant to run often and be the thing that pages someone.

## Reporting and diffing runs

Both modes write one report (`<workdir>/build-report.json` or
`check-report.json`) and a short summary (`build-summary.txt` /
`check-summary.txt`) under the config's `workdir`, and print the summary.
Each run also diffs itself against the previous report of the same mode:
which entries newly failed, which recovered, which are still failing,
printed in the summary and included alongside the report when
`report_url` is set (`POST` of `{"report": ..., "diff": ...}` as JSON; a
delivery failure is a warning on stderr, never fatal to the run — nothing
here invents an email or notification service, point `report_url` at
whatever already receives webhooks). This is what turns "the catalog has
fifty entries and three are broken" into "two entries broke since last
time, here they are."

## Installing it as a service

`packaging/catalog-conformance/` ships four systemd units and an example
config, meant to be dropped onto a spare machine and left alone — not
packaged as a `.deb`: it is host-side tooling that runs *against* a SynOS
engine checkout, not something a built SynOS image installs, so it does
not fit this repository's `packages/` recipe pipeline (which builds
packages for images this engine produces, not for the machine that drives
the build).

1. Clone this repository somewhere stable, e.g. `/opt/synos-engine`
   (`git clone … /opt/synos-engine && cd /opt/synos-engine && make check`
   to confirm the host can build at all — `tools/synos check`).
2. Create a system user with access to the container runtime for `build`
   (`useradd --system --home /var/lib/synos-conformance synos-conformance`,
   then add it to the `podman` or `docker` group); `check` never touches a
   container runtime and can run as the same or a more restricted user.
3. `install -d -o synos-conformance -g synos-conformance /var/lib/synos-conformance`
   and `install -d /etc/synos`, then copy
   `packaging/catalog-conformance/conformance.example.yml` to
   `/etc/synos/conformance.yml` and fill in `catalog_url` at least.
4. Copy the four unit files in `packaging/catalog-conformance/` to
   `/etc/systemd/system/`, editing each `.service`'s `WorkingDirectory=`
   and `ExecStart=` path if the checkout from step 1 is not
   `/opt/synos-engine`.
5. `systemctl daemon-reload && systemctl enable --now synos-conformance-check.timer synos-conformance-build.timer`.
   `synos-conformance-check.timer` runs a few minutes after boot and every
   15 minutes after; `synos-conformance-build.timer` runs weekly. Both are
   `Persistent=true`, so a run missed while the machine was off happens
   once it is back. Run either service once by hand first
   (`systemctl start synos-conformance-check.service`) and read
   `journalctl -u synos-conformance-check.service` before trusting the
   timer.

## What a hosted build service would still need

`tools/catalog_conformance.py` is the first real piece of that service —
it already runs unattended, against a URL instead of a checkout, and
reports what broke — but a paid, multi-tenant hosted build service is a
larger system built on top of the same runner, and honestly still needs:

- **A queue in front of it.** This tool (and the matrix under it) refuses
  to *start* a run the host cannot feed and otherwise runs its entries one
  at a time (or `--jobs` at a time, matrix-side); a service needs to accept
  requests faster than it can build them and queue them, not reject or
  serialize everything onto one machine.
- **Isolation between customers' builds.** `--privileged` (`tools/synos`'s
  own container invocation, which this tool and the matrix both use
  unchanged) is fine for one trusted operator building a known catalog and
  is not something to hand a customer's own bundle to unsupervised; a
  service needs each customer's build in its own container or VM so one
  cannot see or affect another's.
- **Somewhere to keep the artefacts.** ISOs are gigabytes each; a service
  needs object storage, not the build host's own disk, plus a retention
  and deletion policy and a way to hand a customer a download without
  exposing the host that built it.
- **Per-customer signing keys.** `SYNOS_SIGNING_KEY` here is one operator's
  key for one repository; a service needs a key (or a signing path) per
  customer or per build, and a way to bill for the disk-minutes a build
  actually used — neither this tool, the matrix, nor the engine they drive
  currently tracks that.

## Cheap and expensive CI

`.github/workflows/catalog-matrix.yml` splits by cost:

- **`plan`** (hosted `ubuntu-24.04`, every push and pull request, and before
  the nightly `matrix` job): validates every catalogued bundle, applies each
  one into the disposable checkout and renders its manifest, checks whether
  every base/suite's builder image tag still resolves on the configured
  registry (informational — a tag can legitimately be unpublished before
  the first release), and runs this feature's own unit tests. No image is
  built.
- **`matrix`** (self-hosted only, gated by the repository variable
  `SYNOS_SELF_HOSTED=true` on a runner carrying the `synos-builder` label,
  the same gate `.github/workflows/build.yml` uses for its own build job):
  runs `tools/build_matrix.py --resume` and uploads `report.json`,
  `summary.txt` and every target's logs as a workflow artifact. It never
  pushes `bundle-catalog/build-status.yml` directly to a protected branch;
  when that file changed, the job prints the diff for a person to carry
  into a pull request.

A hosted GitHub runner has neither the disk (a single ISO already needs
40+70 GB) nor the time (40+ minutes per target, fifty-plus targets) for
this job; that is the whole reason for the split, not a preference.
