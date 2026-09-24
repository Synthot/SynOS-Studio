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
`verified: true` claim (docs/BUNDLE.md, "The bundle catalog"). That file is
tracked in this repository, though, and a local proof run must not modify a
tracked file on its own: by default every run writes its own copy to
`--output/build-status.yml` (beside `report.json`) and leaves
`bundle-catalog/build-status.yml` alone entirely; `--update-catalog-status-file`
opts into also writing the tracked copy, merged so an `--only` run never
truncates the rest of the catalog's record. Only the scheduled workflow
passes that flag ("Cheap and expensive CI", below) — never a local run.

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
`--output DIR` (default `dist/matrix/`), `--update-catalog-status-file`
(also write the tracked `bundle-catalog/build-status.yml`; off by default —
a local run's own copy always lands at `--output/build-status.yml` instead,
never touching a file this repository tracks).

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
proven by `tests/unit/test_build_matrix.py`'s own `IsolationTests`).
`tools/catalog_conformance.py build` does not call `tools/synos` at all any
more (below) and so has no engine checkout to protect this way; what it
still shares with the matrix is the *reason* for a per-worker resource
under `--jobs` — see the cache-volume paragraph a little further down.

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

`tools/catalog_conformance.py build` runs the real launcher instead of
`tools/synos` (below), so it has no engine checkout to isolate — but it has
the exact collision `bundle_launcher.sh`'s own cache volume
(`synos-cache-<base>-<suite>`) can still cause between two of *its* own
workers: above `jobs: "1"`, each worker that was not given an explicit
`container_root` gets its own podman storage root under `workdir`, so two
workers that happen to build the same base and suite never share that
volume's name.

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
config (`"1"` or `"auto"`, default `"1"`) and the same `--jobs auto`
formula, with the per-worker podman storage isolation described above.

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
same rule for the conformance tool: a fake `browser_factory` (a context
manager whose `download_bundle()` returns fixed, in-memory `.tar.gz` bytes
built with Python's own `tarfile` module) covers "drive the real page"
without starting a real browser, and a fake `build.sh` script (one that
writes `dist/build.log` then exits 3, one that exits 2 with no
`dist/build.log` at all, one that succeeds) covers "behave like the
person" and pins the `page`/`launcher`/`engine` stage split without
starting a real launcher; `tests/unit/test_devtools_browser.py` covers
`tools/devtools_browser.py`'s own CDP-message plumbing (the websocket
frame format, `_wait_for_debug_port`'s retry loop) the same way, entirely
against a fake socket — no test anywhere in this project starts
`google-chrome`. `check`'s archive and registry lookups go through
injectable `fetcher`/`inspector` parameters a test replaces with an
in-memory fake `Packages.gz` and a fake `skopeo` answer — no test in that
file opens a real network connection either. `tests/unit/test_scratch_checkout.py`,
`tests/unit/test_host_resources.py` and `tests/unit/test_job_queue.py`
carry the same rule down into the isolation and parallelism layer
underneath `tools/build_matrix.py`: the first two run the real
`git`/disk-usage/CPU-count machinery against fixed numbers or this actual
repository (worktree creation is not a build), and the third runs
`tools/job_queue.py`'s scheduler — several fake builds at once, one made
to crash and retried, several made to crash at once — entirely against
in-memory fakes, never a
real `tools/synos`.

## Proving the published catalog: tools/catalog_conformance.py

`tools/build_matrix.py` builds what is in this checkout, through the
engine's own `tools/synos`. It cannot catch a catalog that drifted after
the Studio site last deployed, a package or container image an upstream
archive quietly dropped between deploys, or — the failure that actually
cost a day, found only by building an image and booting it — the Studio
*page's own bundle generation* silently dropping fields
(`software.files`, `security.open_ports`, a service's capability, an
appliance's startup command) when it serializes a catalogued entry back
out. A tool that reads the catalog's embedded `files` and hands them to
`tools/synos` directly never asks the page to generate anything, so it
cannot see that class of bug either. `tools/catalog_conformance.py build`
exists specifically to see it: it drives the real page and runs the real
launcher, not a shortcut through either.

```bash
python3 tools/catalog_conformance.py build --config conformance.yml
python3 tools/catalog_conformance.py build --config conformance.yml --only web-server-nginx
python3 tools/catalog_conformance.py check --config conformance.yml
python3 tools/catalog_conformance.py build --config conformance.yml --dry-run   # fetch and list, build nothing
```

`packaging/catalog-conformance/conformance.example.yml` documents every
config key: the catalog URL, the Studio page's own URL (`site_url`,
required to build), a working directory, the same disk thresholds the
matrix guards on, how many entries one `build` run attempts and its
per-entry timeout, and an optional URL to `POST` the finished report to.

### The flow, per entry

1. **Drive the real page** (`tools/devtools_browser.py`, `StudioSession`).
   Headless Chrome opens `site_url`, clicks "Bundle Catalog", clicks the
   entry's own card (through the real `chooseCatalogEntry()` — the exact
   code path that turned out to drop fields), fills the configuration's
   name — deterministically, the entry's own id — into the real name
   field, and clicks "Download bundle". The bytes are captured by
   overriding `URL.createObjectURL` on the page and reading the `Blob`
   back directly: the *exact* bytes `downloadBundle()` produced, the same
   technique the Studio repository's own `tests/e2e_open_bundle.py`
   already uses to verify a download, reused rather than reinvented. No
   selenium, no non-stdlib driver: a small stdlib websocket client speaks
   the Chrome DevTools Protocol directly, the same way that test does.
   Missing `google-chrome`/`chromium` is a clean, printed skip, not a
   failure — this stage, and only this stage, needs a browser.
2. **Behave like the person.** The downloaded `.tar.gz` is unpacked into
   the run's own working directory (refusing any archive member that would
   write outside it) and its own `./build.sh` is run, unattended
   (`SYNOS_YES=1`, so it never blocks on a prompt) but otherwise
   untouched: no `SYNOS_NO_UPDATE_CHECK`, no `SYNOS_CHANNEL` override, so
   the launcher's own update check and channel handling run for real,
   because they are as much a part of what a person experiences as the
   engine is. `tools/synos` is never called directly here. Container
   storage location (`container_root`/`container_runroot`) and a pinned
   builder image are the only environment this tool adds on top of what a
   person would already have to set by hand.
3. **Check the result**, exactly as `tools/build_matrix.py` does: the
   newest ISO under the launcher's own `dist/`, its SHA-256, and
   `tools/smoke_test.py` against the profile resolved from what the
   archive actually contained (its own `profiles/<id>.yml`, not the
   catalog JSON's copy) — so a smoke-test failure reflects what the page
   really produced, not what it was supposed to.

### What a failure was

A build failure is recorded with a `stage`, because a failure in the
page's generation, in the launcher, or in the engine are three different
problems with three different owners:

- `"page"` — the browser could not reach or drive the page, its own
  validation refused to generate a bundle, the entry was not offered at
  all, or the downloaded archive itself does not unpack or has no usable
  `bundle.json`/manifest. The page's fault.
- `"launcher"` — `./build.sh` exited before a containerized build ever
  started (no container runtime, could not resolve or pull an engine
  image, a host check failed) — no `dist/build.log` exists. The
  launcher's fault.
- `"engine"` — `dist/build.log` exists: the containerized build actually
  ran and failed (or timed out) inside it. The engine's fault, the same
  as a `tools/build_matrix.py` failure.
- `"smoke"` — the build succeeded but `tools/smoke_test.py` did not pass.

`status` keeps its existing meaning (`success`, `build_failed`, `host`,
`invalid`, `timeout`, `error`, `skipped`) independent of `stage`; `stage`
is `null` on a genuine success. An engine-stage failure, real:

```json
{
  "id": "web-server-nginx", "kind": "catalog", "base": "ubuntu", "suite": "noble",
  "checksum": "9c1f...", "status": "build_failed", "stage": "engine", "exit_code": 3,
  "download": {"filename": "web-server-nginx-bundle.tar.gz", "size": 4318},
  "iso": null, "log_path": ".../build.log",
  "log_tail": ["debootstrap: retrieving Release", "E: Failed to fetch ..."],
  "smoke": null
}
```

### Running one entry by hand

To watch it work, not just read a report:

```bash
# 1. Download exactly what the automated run would, and nothing else:
python3 tools/devtools_browser.py https://studio.example web-server-nginx --output /tmp/synos-watch

# 2. Unpack it yourself:
mkdir -p /tmp/synos-watch/bundle
tar xzf /tmp/synos-watch/web-server-nginx-bundle.tar.gz -C /tmp/synos-watch/bundle

# 3. Run the real launcher interactively, watching its own output live:
cd /tmp/synos-watch/bundle && SYNOS_YES=1 ./build.sh
```

`tools/devtools_browser.py <site_url> <entry_id> [--output DIR]` is the
same `StudioSession` the automated run uses, run standalone; step 3 is
exactly what `tools/catalog_conformance.py build` runs unattended, minus
the environment variables — so this recipe and an automated `--only`
run should behave identically, one silent, one on your own terminal.

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
2. Install `google-chrome` or `chromium` (`build` skips every entry with a
   printed reason, never a crash, when neither is on `PATH` — but then it
   proves nothing) and a container runtime. Create a system user with
   access to the runtime for `build` (`useradd --system --home
   /var/lib/synos-conformance synos-conformance`, then add it to the
   `podman` or `docker` group); `check` never touches a browser or a
   container runtime and can run as the same or a more restricted user.
3. `install -d -o synos-conformance -g synos-conformance /var/lib/synos-conformance`
   and `install -d /etc/synos`, then copy
   `packaging/catalog-conformance/conformance.example.yml` to
   `/etc/synos/conformance.yml` and fill in `catalog_url` and, for `build`,
   `site_url` (the real Studio page this service will drive).
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
  runs `tools/build_matrix.py --resume --update-catalog-status-file` and
  uploads `report.json`, `build-status.yml`, `summary.txt` and every
  target's logs as a workflow artifact. `--update-catalog-status-file` is
  passed here, deliberately, and nowhere else in this repository — it never
  pushes `bundle-catalog/build-status.yml` directly to a protected branch;
  when that file changed, the job prints the diff for a person to carry
  into a pull request.

A hosted GitHub runner has neither the disk (a single ISO already needs
40+70 GB) nor the time (40+ minutes per target, fifty-plus targets) for
this job; that is the whole reason for the split, not a preference.
