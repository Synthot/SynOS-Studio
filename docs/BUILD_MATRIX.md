# The catalog build matrix

`tools/build_matrix.py` is the unattended proof that the whole catalog
builds: every entry in `bundle-catalog/index.yml`, every base/suite
combination this engine supports ("the distro cores"), and the two
saturation targets (below), each built through `tools/synos` exactly the
way a person builds it — nothing here talks to the engine any other way.
`tools/smoke_test.py` then boots the ISO a successful build produced and
checks that the appliance it promised is real. `.github/workflows/catalog-matrix.yml`
runs all three nightly.

All of this — catalog, core and saturation — is how this project tests its
own catalogue. It is not part of what a customer runs: a person buys one
bundle from the real Bundle Catalog on the Studio page and builds it with
that bundle's own `tools/synos build` or `bundle_launcher.sh`, exactly as
always. Nothing in this document changes that path, and nothing in it runs
on a customer's machine.

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
python3 tools/build_matrix.py --kind saturation         # only saturation-ubuntu and saturation-debian
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

## The four layers, and the one question each answers

This file describes one of four checks, and they only make sense together.
Each answers a question the one before it cannot, and none of them runs on a
customer's machine — a person opens the Studio page, gets a bundle, and runs
its launcher.

1. **Does the name exist?** `tools/catalog_apps_audit.py` checks every entry
   of `profiles/catalog.apps.yml` against the real archive's own package
   list, on every base and suite it claims. Set membership against
   `Packages.gz`; 2184 entries in about fifteen seconds. A name the archive
   has never heard of is a build that dies at `apt-get install` on the
   buyer's machine — this is what stops the page offering one.
2. **Will apt install it here?** `tools/catalog_apt_solver.py` takes the
   entries that exist and asks apt's own solver whether each one installs,
   alone, on top of the floor every image already carries
   (`profiles/minimal.yml`, resolved) — `apt-get install --simulate
   --no-remove` inside a throwaway container of that suite's own image, with
   the sources the real chroot gets. "The name is real" and "apt will
   install it alongside what we already install" are different questions, and
   only the second one predicts the forty-minute failure. `--no-remove` is
   what makes the answer honest: without it, apt plans to remove a floor
   package to make room and calls that success. Full catalogue: about six
   hours, because `apt-get` re-parses every index on every invocation. It
   downloads no candidate packages at all — only the indexes, once per
   base and suite — so the cost does not grow with the catalogue.
3. **Do the engine's own packages build into an image?** The saturation
   targets below: every application group and every machine profile, per
   base, in one real ISO that boots. This proves the engine's own abstract
   names, not the open-ended catalogue a person may add to.
4. **Is a specific appliance's own configuration intact?**
   `tools/catalog_conformance.py build`, through the real Studio page and
   the real launcher, for one catalogued entry at a time. This is the only
   layer that sees `software.files`, `security.open_ports`, a service's
   `cap_add`, a kiosk's locked application or a pinned image tag — the class
   of bug that once produced an Nginx appliance that built and booted with no
   site to serve.

Layers 1 and 2 belong in a nightly run; 2 is hours, so it is never on a
per-commit path (`tools/run_checks.py` reaches neither by `--level`). What
none of the four answers: whether two catalogue entries conflict with *each
other*. A person picks a handful of applications, and every pair of 2184 is a
different problem from the one any of these solves.

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

A `saturation` target's success means something narrower still, and the
difference matters enough to say twice: it proves every *package* the
engine ships resolves and installs together in one image on that base. It
proves nothing about a bundle's own *configuration* — `software.files`
(the worst bug this project has had was exactly here: an Nginx appliance
that built and booted with no site to serve and the wrong ports open,
because the page dropped `software.files` and `security.open_ports` on
save), `security.open_ports`, a service's `cap_add`/`env_file`/`exec`,
`policy.kiosk_app`, a dconf lockdown, or a pinned container image tag —
because none of that lives in `profiles/bundles.yml` or a machine
profile's `software.packages.add`, the only two things a saturation
profile is built from. Those checks are the catalog and core targets'
job (offline, seconds per entry) and `tools/catalog_conformance.py check`'s
job (against the published catalog); a saturation build replaces neither.
See "The saturation targets" below for what it *is* for.

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

## The saturation targets

Fifty-odd catalog entries each pin one base and resolve their own small
package list. Measured once, honestly: those 51 entries resolve to a union
of 151 concrete packages on ubuntu and 158 on debian (142 shared); the
*whole* engine — every application group in `profiles/bundles.yml` plus
every machine profile's own `software.packages.add` — comes to 169
concrete packages on debian. Building one image per base that installs
every package the engine ships covers every package-resolution path the
catalog does, at a fraction of fifty-odd separate builds. That is what
`saturation-ubuntu` and `saturation-debian` are: internal test-engine
machinery, generated fresh at plan time by `tools/build_saturation.py`,
never a bundle-catalog entry — nothing under `bundle-catalog/` names one,
`tools/export_catalog.py` never reads one, and `tests/unit/test_saturation.py`
proves both, so a future change that starts exporting one fails a test,
not a code review. A person still buys one ordinary bundle from the real
catalog and builds it with that bundle's own `tools/synos build` or
`bundle_launcher.sh`; nothing about the saturation targets touches that
path.

```bash
python3 tools/build_saturation.py --check              # the plan and package counts, writes nothing
python3 tools/build_saturation.py                       # writes both bundles under .build/saturation/
python3 tools/build_matrix.py --kind saturation --list   # the two targets, as build_matrix plans them
```

**What the profile is built from.** `tools/build_saturation.py` reads
`profiles/bundles.yml` (every application group, every run — a group added
there needs no update here to be covered by the next saturation build) and
every machine profile file under `profiles/` for its own, un-inherited
`software.packages.add`. It resolves both through `bases/<base>/packages.map`,
the same `resolve_packages()` every other profile is resolved through, and
writes the result as an ordinary bundle (`bundle.json`, `manifests/`,
`profiles/`) — the generated profile itself just lists `software.bundles`
and `software.packages.add`/`remove`, resolved the normal way at build
time, not a pre-computed package list baked into the file.

**Two honest ways a name can be missing, never confused with each other.**
A group this base's own archive cannot serve at all —
`bases/<base>/packages.map`'s `unavailable:` (the same marker
`resolve_packages()` already refuses on any profile) — is skipped for that
base, named and reasoned in the generated profile's own `description:` and
in this tool's output. This is not an exclusion; it is the base saying no.
The current case: `test-engine` needs `browser-headless`, which
`bases/ubuntu/packages.map` marks unavailable (no non-snap Chromium in
Ubuntu's own archive), so `saturation-ubuntu` skips that one group and
`saturation-debian`, whose archive has a real Chromium, does not.

A genuine collision — two packages that `Conflicts:` or `Breaks:` each
other in the real archive, or a maintainer's own documented reason for a
non-package collision such as a kiosk policy against a full desktop — is a
different thing: an *exclusion*, listed in the optional
`bases/<base>/saturation-exclusions.map`, one `name = excluded: <reason>`
line per excluded bundle-group id or concrete package name, the same
marker idiom `bases/*/packages.map`'s `unavailable:` and `bases/*/live.map`
already use. Nothing is excluded without a written reason, and the tool
refuses to start if an entry names something that never appears anywhere
in the union on that base — a stale exclusion is exactly the drift this
idiom exists to prevent. As of this writing, the real archive check
documented in both files' own headers (every package in the union's own
`Conflicts:`/`Breaks:`/`Provides:` field, read from `Packages.gz` the same
way `tools/catalog_conformance.py check` already fetches it, and
cross-checked against every other package in the same union) found zero
genuine collisions on either base — both files exist, and are empty of
active entries, on purpose. A red saturation build is still meant to mean
something: when a future group does collide, the entry goes in with the
evidence above it.

**What this does and does not replace** is "What it proves, and what it
does not" above, worth repeating in one line here: a saturation build
proves packages resolve and install together; it cannot see a bundle's own
`software.files`, `security.open_ports`, a service's
`cap_add`/`env_file`/`exec`, `policy.kiosk_app`, or a pinned container
image tag, because none of that lives in `profiles/bundles.yml` or a
machine profile's `software.packages.add`. The catalog and core targets,
and `tools/catalog_conformance.py check`, are unchanged and still the only
things that check those.

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
- the live session has a *working* network: a managed, connected device
  (`nmcli`), a global IPv4 address on it (`ip addr`) and a default route
  (`ip route`) — in that order, so a failure names exactly which stage was
  never reached (`"lost_at": "device"/"address"/"route"`). This is checkable
  at all only because this one boot (not the graphical boot below) is given
  a virtio NIC against QEMU's own user-mode networking: a full DHCP server
  and virtual router that live inside the qemu process itself, so a real
  DHCP handshake is available with no host privileges, no host network
  setup, and (`restrict=on`) no way for the guest to reach anything past
  QEMU's own virtual router — a unit that tries to pull a container image
  at boot still cannot reach a real registry, on any host, exactly as
  before this check existed. What a pass here proves is that this image's
  own NetworkManager/netplan/ufw/systemd wiring can take a device from cold
  to managed-and-routed; what it *cannot* prove is that a real Wi-Fi or
  Ethernet adapter is recognized and bound to a driver on real hardware —
  a virtio device needs no firmware blob, no vendor driver and no probe
  delay, and never fails to appear the way a real one can. A build whose
  live session has no working network on real hardware despite every
  package for it being installed is exactly the failure this check cannot
  see; see `check_network`'s own docstring in `tools/smoke_test.py`;
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
reading `systemctl is-enabled`'s answer, reading a `stat` line's mode,
picking the one `nmcli` device that is actually connected out of terse
`DEVICE:TYPE:STATE` lines, reading the address off an `ip addr` line and
the default route off an `ip route` line — is a small, pure function, and
`tests/unit/test_smoke_test.py` exercises every one of them against fixed
fixtures: a fake firewall script's text, a fake `systemctl` answer, a fake
`stat -c '%a %s'` line, fake `nmcli`/`ip addr`/`ip route` output (including
ANSI colour codes `nmcli` adds the moment its stdout looks like a tty,
which the serial console's pty always does), and a fake serial transcript
carrying the shell's own echoed input around the markers this tool sends.
A `FakeSession` stands in for the QEMU serial connection in the handful of
tests that exercise a whole check (`check_open_ports`, `check_service_unit`,
`check_shipped_file`, `check_network`) end to end. No test in that file
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
`invalid`, `timeout`, `error`, `skipped`) independent of `stage`, with one
addition: **a build that succeeded but whose boot check did not is its
own status, `"smoke_failed"`** — never left as plain `"success"` (item
110). A person reading `build-status.json` or a run's own summary needs
one name for "the image built but is not certified safe", used
consistently everywhere something decides what counts as ok
(`OK_STATUSES`, shared by `diff_reports()`, the run's own exit code, and
`tools/build_status.py`'s `map_state()`, which folds it into the public
file's plain `"failed"` — the finer distinction, and `boot_passed` on
whichever earlier attempt still had one, are what `build-report.json` and
`error`/`history` are for). `stage` is `null` on a genuine success. An
engine-stage failure, real:

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

### The boot check certifies the image's name

`tools/smoke_test.py`'s own boot check (docs/BUNDLE.md, "the smoke test")
also certifies the booted image's `/etc/os-release` `NAME` against what
the resolved configuration actually promised — `brand.display_name`
inside `dist/<name>.resolved.json`, the engine writes next to the ISO,
exactly what `tools/render_brand.py` baked into the image. Item 108's own
bug: the first real end-to-end run showed `expected_name: null,
name_found: null` and a false `smoke_passed`, because the resolved
configuration was never being handed to the smoke test at all — nothing
gave the name check anything to certify against, so it had nothing to
say, and its own honest "skip" (see below) was not yet wired in either.
Fixed both ways: `build_one()` now copies `dist/<name>.resolved.json` out
unconditionally (see the next section) and reads its `brand.display_name`
straight into `tools/smoke_test.py`'s `expected_name`; and the check
itself — like `tools/smoke_test.py`'s other checks, "where the matrix
cannot check something honestly it is left out, not invented" — is
honestly `"skipped"`, never counted against the overall result, when
nothing at all (no resolved configuration and no `--brand`, for the
standalone CLI) supplied a name to certify.

### Item 109: keep the evidence for whatever actually failed

`dist/<name>.resolved.json` is copied out unconditionally now — success
or failure, `smoke: true` or `false` — alongside the build log and (once
the smoke test runs) its own serial transcript, the same three pieces of
evidence a person would want regardless of outcome. And cleanup (item
87-92, "Freeing disk as it goes" below) only ever runs for a plain
`"success"`: a build that succeeded but whose boot check did not
(`"smoke_failed"`) now keeps its *entire* working directory — the ISO
included — exactly like a launcher or engine failure already did. Before
this fix, `status` stayed `"success"` through a smoke failure, so cleanup
ran anyway and deleted the ISO for the one entry whose evidence actually
mattered.

### Covering more than one base

By default an entry builds once, on whatever base its own manifest pins
— unchanged, and this is the common case (fifty-odd entries, one base
each, no configuration needed). `cover_bases` in `conformance.yml` (or
`--cover-bases ubuntu,debian` on the command line, which replaces
whatever the config says for that one run) names every base to attempt
each *selected* entry against instead — base names are exactly `ubuntu`
and `debian`, the two this checkout has adapters for
(`bases/<name>/base.env`); an unknown name is refused before anything
runs, by `Config.load()` or, for the command line, by `_run()` itself.

Each (entry, base) pair becomes its own attempt, its own working
directory, and — only once more than one base is in play — its own key in
`build-report.json`'s `targets` and `build-status.json`'s own per-entry
`bases`: `"web-server-nginx@debian"` alongside `"web-server-nginx@ubuntu"`,
never colliding with the plain, single-base case's `"web-server-nginx"`.
Covering a base an entry does not itself pin **changes only the manifest's
own `base:`/`suite:` lines** — the suite to that base's own default
release (`bases/<base>/base.env`'s `DEFAULT_SUITE`) — leaving the profile,
the packages, the services, everything else the bundle carries, exactly
as the page generated it. This is a plain text substitution, never a YAML
round-trip, so nothing else in the manifest can be reformatted by
accident.

**What actually happens covering the other base is never suppressed or
worked around.** An entry pinned to Debian, asked to also cover Ubuntu
(or vice versa), is handed to the real launcher with nothing but its base
and suite changed — the same packages, the same container images, the
same profile. If those exist on the other base too, the build genuinely
succeeds there; if a package the bundle names is Debian-only (or
Ubuntu-only), the build genuinely fails, ordinarily at the `"engine"`
stage (apt cannot find it) rather than anywhere earlier — the exact, real
answer to "does this bundle actually work on the other base", which is
the entire point of asking; no name-remapping or compatibility shim is
attempted, because a bundle that only means one base should fail loudly
when asked to be something else, not quietly pretend to work.

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

## Publishing the build-status badge

`build` also writes `<workdir>/build-status.json` — a small, versioned
file meant to be fetched by a browser on every load of the Studio page, so
each catalogued bundle can carry a badge saying it actually built and is
safe to use. It is not the same file as `build-report.json` above (that
one is the full record of a run — every stage, every log tail, every
smoke-test detail — and is not meant for a browser to fetch) and not the
same file as `tools/build_matrix.py`'s own tracked
`bundle-catalog/build-status.yml` (that one is YAML, lives in this
repository, and records whether *this checkout's* bundle-catalog builds,
for a person reviewing a pull request; this one is JSON, lives only under
a conformance run's own `workdir`, and records whether the *published*
catalog builds, for a browser).

Shape (`tools/build_status.py`, schema_version 2)::

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

**Per base, not per entry (schema_version 2, item 111).** The Studio page
lets a person change an appliance's base *after* choosing it from the
catalog, so a badge that only ever covered the base the entry happens to
pin is a claim about something the person may not build. Every result
lives under `entries.<id>.bases.<base>` instead of directly under
`entries.<id>`; base names stay exactly `"ubuntu"` and `"debian"`, the
same two names the engine already uses (the page renders one small icon
per base with a tooltip). **An entry with no result for a base says
nothing for that base** — `bases` simply has no key for it, the same
"absence means never tested" rule applied one level deeper. A
schema_version 1 file (the shape this replaced) is migrated automatically
on load (`migrate_v1_to_v2()`): each entry's one result becomes that
entry's result for whichever base it was actually built against; an entry
that was only ever `queued`/`testing` (no real outcome, so no base was
ever confirmed) has nothing to migrate and is dropped, indistinguishable
from an entry that was never tested at all.

`state` is one of `"queued"`, `"testing"`, `"success"`, `"failed"` or
`"skipped"` (every finer distinction `build-report.json` records — which
stage, a timeout, an invalid launcher exit, or a build that succeeded but
whose boot check did not (`"smoke_failed"`, below) — folds into
`"failed"` here; a badge only needs to say not yet / safe / not safe, and
`boot_passed` on a `"success"` record still says whether the check itself
ran clean). The sixth state the owner listed, **"never tested"**, is
never written as a value: a (entry, base) pair the catalog could produce
but this file's `bases` has no key for is "never tested" by its absence.
The page side was told to render three things at a glance: not yet tested
(covers absent, `"queued"` and `"testing"` alike — none of those is a
claim about safety either way), succeeded, or failed since a date; the
finer `queued`/`testing` split exists so the file itself is never stale
mid-run, even though the badge collapses them.

`date` is when the current `state` was last (re-)confirmed; `since` is
when the *current* `state` began as an unbroken streak — a bundle failing
every night for a week shows the date the streak started, not last
night's unremarkable re-confirmation, and a bundle that has been green for
a month shows when it turned green. Both directions use the same rule
(compare against the most recent `history` entry's state), and a
`queued`/`testing` round-trip between two identical outcomes never
disturbs it. `error` is `null` except for `"failed"` or `"skipped"`, where
it is one sanitized line — see below.

`history` is bounded to the most recent 10 entries (`tools/build_status.py`'s
`HISTORY_LIMIT`), oldest first — a fixed count rather than a time window,
because it bounds the file's worst-case size exactly regardless of how
often builds run; the full, unbounded detail is what `build-report.json`
is for. Only a real completed attempt (`success`/`failed`/`skipped`) adds
a history entry — passing through `queued`/`testing` on the way there does
not.

`size`/`checksum` are the built ISO's, not the downloaded bundle
archive's, and are `null` whenever there is none. `boot_passed` (item
111's "boot result") is `null` when the smoke test did not run at all,
`true`/`false` once it actually did — and can be `false` even while
`state` is `"success"`, since a build succeeding and its boot check
certifying it are two different things (see "The boot check certifies the
image's name" below). `schema_version` lets the page refuse a shape it
does not understand instead of guessing.

### Live, not only final

`build` marks every selected entry `"queued"` before starting, `"testing"`
the moment its own build begins, and its real outcome once it finishes —
written and (if uploading is configured) sent immediately at each step,
not batched to the end, so a file fetched mid-run tells the truth about a
run in progress. Only the *live* `queued`/`testing` transitions and a
changed final outcome trigger an upload on their own; a bundle that
re-confirms the same outcome as last time still gets its `queued` and
`testing` moments uploaded (progress is always worth showing) but not a
redundant third upload for an unchanged result — item 73's original
"not every entry" still holds for the outcome itself.

A run that is killed mid-build leaves an entry stuck at `"testing"`
forever unless something resolves it. The next run, before queuing
anything new, resolves every such entry to `"failed"` with
`error: "the previous run was interrupted before this entry finished
testing"` (`tools/build_status.py`'s `resolve_stuck_testing()`) — **never**
guessed as `"success"`: a public "safe to use" badge must never claim an
outcome nobody actually observed. Which ids this touched is printed in
the run's own summary and recorded in `build-report.json`'s
`resolved_interrupted`.

It is written after every entry finishes, not only at the end — carrying
forward every id it is not replacing — so a run interrupted partway
through fifty-odd entries (the better part of a day) still leaves a valid
file on disk (`tools/build_status.py`'s `StatusUpdater`, atomic
write-then-rename: a reader never sees a half-written file).

### The error line

A failure's `error` (both the entry's own and each `history` entry's) is
one sanitized line: the stage that failed and the first real error line,
never the whole log tail. It is published on a public page, so it is
treated as untrusted output being published, not an internal log line
(`tools/build_status.py`'s `sanitize_error()`): control characters
stripped, this machine's own working directory and home directory (plus
the generic `/home/*` and `/root` patterns, as a backstop for some other
user's path) and this machine's own hostname replaced with `<path>`/
`<host>`, IPv4 addresses replaced with `<ip>`, anything shaped like
`password=`/`token=`/`secret=`/`api_key=` redacted, and the whole line
capped at 240 characters. Best-effort, not a guarantee — it catches the
specific classes item 96 named, not every conceivable leak, and is not a
substitute for keeping real secrets out of `build.sh`'s own log lines in
the first place.

### Getting it to the web server

The owner is willing to give the conformance service upload credentials
rather than move the file by hand. An optional `upload:` section in
`conformance.yml` (`tools/status_uploader.py`) drives that — see the
commented example in
`packaging/catalog-conformance/conformance.example.yml`. Filled in, `build`
uploads `build-status.json` once for every entry whose `state` changed
(not every entry — most re-builds of an already-`success` bundle do not
change anything a badge shows) plus once, unconditionally, when the whole
run finishes; each attempt retries a few times with a growing pause
between tries, and a failed upload after all retries is a warning printed
on stderr and listed in `build-report.json`'s own `upload_warnings`, never
a failed build.

Two transports are supported:

- **SSH** (`protocol: sftp` or `scp`) — shells out to the system `sftp`/
  `scp` binary (this project's own rule against building a shell command
  from a name read out of a file; the same reason
  `tools/devtools_browser.py` spawns Chrome directly). Key auth
  (`key_path`) needs nothing extra installed. Password auth additionally
  needs `sshpass` on `PATH` (`apt install sshpass`); the password reaches
  it only through the `SSHPASS` environment variable of that one short-
  lived process, never as a command-line argument another user on the
  machine could read with `ps`.
- **FTP over TLS** (`protocol: ftps`) — the standard library's own
  `ftplib.FTP_TLS`, nothing extra to install.

Plain, unencrypted FTP (`protocol: ftp`) is refused with an explanation
unless the config also sets `allow_insecure_ftp: true`: both the account's
credentials and the file itself would otherwise cross the network in the
clear. Set it only against a server you already trust for other reasons
and that genuinely offers nothing better — `ftps` costs nothing extra to
use wherever the server supports it.

No credential is ever written to `build-report.json`, printed by this
tool, or included in a warning: every message a transport can raise is
scrubbed of the configured password first
(`status_uploader.describe_destination()` is the only representation of a
destination anything here ever prints, and it never includes one).

### Checking a config, or that a file arrived

`tools/status_uploader.py` works standalone, without running a build, to
test an `upload:` section before trusting it to a timer::

    # Prints the destination and file size it would send — no network, no secrets:
    python3 tools/status_uploader.py --config /etc/synos/conformance.yml --file /var/lib/synos-conformance/build-status.json

    # Sends it for real, once, no retry, and says whether it worked:
    python3 tools/status_uploader.py --config /etc/synos/conformance.yml --file /var/lib/synos-conformance/build-status.json --send

To confirm a file that a real `build` run uploaded actually landed, use
whatever the server itself already offers — an HTTP `HEAD`/`GET` on the
deployed URL (`curl -I https://studio.example/data/build-status.json`) is
usually the simplest, since that is exactly what the page itself will do;
an `ls -la` over the same SSH credentials works too (`sftp` in
`packaging/catalog-conformance/conformance.example.yml`'s example one-off:
`sftp user@host <<< "ls -la /var/www/studio/data/build-status.json"`).

## Freeing disk as it goes

Fifty-odd catalogued bundles, each producing a 2 GB image plus an unpacked
bundle and a build tree, otherwise fill the disk long before a full pass
finishes. `build` frees a *successful* entry's heavy directories as soon
as that entry is done — never batched to the end, so a long run's peak
usage stays near one build's worth, not fifty (`tools/entry_cleanup.py`,
wired in by `tools/catalog_conformance.py`'s `cleanup_one()`).

Removed: the unpacked bundle (its own build tree and `dist/`, including
the ISO `build.sh` left there before this tool copied it out) and the
copied ISO itself. Kept: the build log (compressed if it is actually
large), the smoke test's own evidence (this project's own smoke test is
serial-console-only by design and has no screenshot — see `tools/
smoke_test.py`'s own module docstring — so its transcript is the
equivalent evidence), and the small `output` directory that held both.
The image's name, size and checksum are never lost even though the bytes
are: they are already in `result`/`build-report.json`/`build-status.json`
by the time cleanup runs. **Only on success** — a failed or skipped entry
keeps its entire working directory untouched, because that is exactly
what somebody will want to read to see why it failed; the run's own
summary says which entries were cleaned and which were kept, and why.

`cleanup: true` (the default) in `conformance.yml`, or `--no-cleanup` on
the `build` subcommand for one debugging run without editing the config —
the flag only ever keeps more than the config says, never less. Either
way, `run_build` refuses to run at all — not only to clean up — when
`workdir` looks like a filesystem root, the invoking user's home
directory, or a source checkout (a `.git`/`.hg`/`.svn` entry directly
inside it): a configuration mistake worth catching outright, since this
tool otherwise deletes things inside it without asking again.
`tools/entry_cleanup.py`'s own removal function additionally refuses
anything outside `workdir`, and never follows a symlink out of it — a
bundle's own `build.sh` runs arbitrary shell inside the tree it unpacked,
so nothing here assumes that tree stayed well-behaved.

When cleanup is enabled and this run owns the storage it used (`container_root`
was not set — a person who *did* set it manages that storage themselves,
so it and everything created inside it are left alone entirely), the end
of a run also removes every per-worker podman storage root the run itself
created (`workdir/podman-storage/worker-N`) and asks the container
runtime to prune the launcher's own `synos-cache-<base>-<suite>` volume
for every base/suite this run touched — with no force flag, so the
runtime's own refusal to remove a volume something still has mounted *is*
the "another build may be using it" check. Exactly what was pruned and
what was left alone (and why) is in the run's own summary and
`build-report.json`'s `storage_reclaimed`.

## One shared build folder per pass

This is machinery for the per-entry builds that must still happen — the
ones proving a bundle's own *configuration* (`software.files`,
`security.open_ports`, a service's `cap_add`/`env_file`/`exec`, a pinned
container image tag), which the saturation targets above cannot see. It is
a mode of `tools/catalog_conformance.py`'s own `build`, never of
`tools/bundle_launcher.sh`: the launcher a customer's own downloaded bundle
runs behaves exactly as it always has, with no new flags and no shared
directories of its own — it just happens, in this mode, to be invoked from
the same directory more than once in a row, which is all it needs for its
own existing cache to help.

**What is paid per entry today, and should not be.** Every entry gets a
fresh `workdir/work/<id>`, wiped before it starts. The launcher's own
engine-source download and extraction (`.build/engine-src/<id>`, already
content-addressed with a `.ok` completion marker,
`tools/bundle_launcher.sh`'s `engine_src_is_reusable()`) lives inside that
folder, so it is paid again every time even though the launcher's own
logic was built to avoid exactly that, given the chance. The builder
image and the per-base/suite cache volume (`synos-cache-<base>-<suite>`)
are *not* actually re-paid per entry today — the image lives in the
container runtime's own storage (`container_root`, already reused across
a worker's whole run — "Isolation and parallelism" above) and the cache
volume is a named runtime volume, not tied to any folder — so this mode
does not change either of those; it only fixes the one thing that was
actually being thrown away for no reason.

**`--shared-workdir`** (or `shared_workdir: true` in `conformance.yml`)
turns this on:

```bash
python3 tools/catalog_conformance.py build --config conformance.yml --shared-workdir
```

- **One working directory for the whole pass**, per worker (`jobs` still
  controls how many; a shared, single-threaded pass uses exactly one).
  Before each entry, `clear_shared_folder()` removes exactly what the
  *previous* occupant declared as its own — the bundle.json actually on
  disk right now, its own `files` list, plus bundle.json itself, plus its
  `dist/` build output — never a wildcard sweep, and never anything under
  `.build/`. That is on purpose: `.build/engine-src` is where the saving
  lives, and it is the one thing left exactly alone. `tests/unit/test_catalog_conformance.py`'s
  `SharedWorkdirTests` proves the risk this exists to prevent cannot
  happen: a second entry built in the same folder never sees a file the
  first one shipped.
- **A failure cannot poison the next entry.** `clear_shared_folder()` is
  called unconditionally, before every entry, and it decides what to
  remove from what is actually on disk, never from an assumption about how
  it got there — a failed, timed-out or errored entry's own files are
  simply the next thing cleared, exactly the same way a successful one's
  would be. Nothing about a bad entry makes the folder unsafe to reuse.
- **`dist/` is harvested first.** The existing per-entry harvest (the ISO,
  its checksum and size, the build log, the smoke test's evidence) runs
  exactly as it always has, into a `target_dir` that is *always* per-entry
  (`workdir/results/<id>`) even when the folder the build itself ran in is
  shared — never the shared folder itself, which the next entry is about
  to clear. "Freeing disk as it goes" above still runs on success too, in
  a shared-aware way: it removes `dist/` (the one genuinely heavy,
  genuinely per-entry thing) but never the shared folder itself, since
  that is exactly where `.build/engine-src` lives.
- **Order matters for the saving.** The cache volume and the engine-source
  cache are both per base/suite; a pass that mixes bases still works (every
  entry still builds correctly) but stops saving anything on a base/suite
  switch. `--shared-workdir` sorts the plan by (base, suite) first — read
  from the fetched catalog's own embedded files when there is no
  `--cover-bases` override, exact when there is one — so entries that would
  actually share a cache run back to back. The run's own report
  (`shared_workdir.order`, `.groups`) says what order it actually ran in
  and how many groups that came to, and `shared_workdir.engine_source_reused_count`
  per entry says, honestly, whether a warm `.build/engine-src` was actually
  there when that entry started — never assumed.
- **Nothing here is left running by itself.** The shared folder is not
  removed at the end of a pass (unlike a successful non-shared entry's own
  folder) — the next pass gets to reuse whatever is left, on purpose; there
  is deliberately no extra flag to force it away, since `--no-cleanup`
  already exists for "keep everything" and a stale shared folder is safe
  by construction (the next entry clears it before using it either way).

**Measured, honestly, on a fake runtime — no real container, no real
network, no real `build.sh`.** A real build is 40-plus minutes; timing
three real ones just to compare wall clocks was not worth it, so the
comparison instead used a stand-in `build.sh` that simulates exactly the
one mechanism this mode changes: it sleeps 0.4s the first time (standing
in for the real engine-source download+extract) unless `.build/engine-src`'s
own marker is already there, in which case it says so and skips the sleep,
then always sleeps 0.1s (standing in for the build itself, which every
entry pays regardless). Three entries, `tools/catalog_conformance.py`'s
own `build_one()` called directly, no mocking beyond the fake `build.sh`:

| | wall clock | cold engine-source downloads |
|---|---|---|
| today (per-entry `work_dir`) | 1.61s | 3 |
| `--shared-workdir` | 0.77s | 1 (2 reused) |

52% faster on this synthetic harness — but that ratio is an artifact of
making the simulated download (0.4s) comparable in size to the simulated
build (0.1s) so the effect would be visible at all; on a real ~40-minute
build against a real ~45 MB download, the same avoided-downloads saving is
a much smaller fraction of one pass's wall clock. The real value is not in
that percentage: it is 49 avoided 45 MB downloads and re-extractions on a
full 51-entry pass, and the disk churn that goes with them, for exactly
zero change to what each entry actually proves.

## Installing it as a service

`packaging/catalog-conformance/` ships four systemd units and an example
config, meant to be dropped onto a spare machine and left alone — not
packaged as a `.deb`: it is host-side tooling that runs *against* a SynOS
engine checkout, not something a built SynOS image installs, so it does
not fit this repository's `packages/` recipe pipeline (which builds
packages for images this engine produces, not for the machine that drives
the build).

`packaging/install-test-engine.sh` automates steps 2-5 below on a single
machine already holding a checkout (step 1): it detects the distribution
(Debian/Ubuntu, Fedora/RHEL, openSUSE, Arch or Alpine; anything else is
refused rather than guessed), installs whichever of podman, QEMU, xorriso,
tesseract, Pillow and a headless browser is actually missing, points
podman's rootless storage at whichever disk has room for a build (an
explicit `--storage-path`, or the largest suitable filesystem it finds),
writes `/etc/synos/conformance.yml` from the answers given on the command
line or interactively (never a template left to edit blind, and never with
a credential in it), installs the four units with a dedicated user, and
then runs the cheap checks below for real. `--dry-run` prints every step
without doing any of it; `--help` lists every option.

Building a machine to run this, rather than adding it to one that already
exists? `profiles/bundles.yml`'s `test-engine` group installs the same
list (minus the distribution-detection step, since a profile already pins
its base) into the SynOS image itself, so it comes up with everything but
this script; the bundle catalog's "Test Engine Build Machine" entry
(`bundle-catalog/test-engine/`) pairs it with the Yocto build host, the
combination this section assumes.

By hand:

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
