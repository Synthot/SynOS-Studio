#!/usr/bin/env python3
"""Build the SynOS packages under packages/ into a local APT repository.

    python3 tools/build_packages.py [--manifest manifest.yml] [--output .build/repo]
                                    [--only name ...] [--list] [--jobs auto|N]

Each packages/<name>/ holds a `control` template, an `assets/` tree installed
as-is, optional `templates/` rendered with the substitutions below, optional
maintainer `scripts/` and an optional `conffiles` list. Substitutions:
${VERSION} ${BASE} ${SUITE} ${ARCH} ${REPO_URL} ${REPO_HOST} ${REPO_ENABLED}
${BRAND_ID} ${BRAND_NAME} ${BRAND_VENDOR} ${BRAND_URL_HOME} ${BRAND_URL_SUPPORT}

The repository is a flat archive (`Packages`, `Release`, `InRelease`) under
the output directory, signed with the repository key. The key is taken from
SYNOS_SIGNING_KEY (armored, CI secret), SYNOS_SIGNING_KEY_FILE, or
keys/private/synos-archive-keyring.sec; when none exists a development key is
generated there. Its public half is written to keys/public/ and embedded into
synos-archive-keyring, so images trust the repository they were built with.

Product name: `synos` (package names, paths, dconf keys, rd.synos.* kernel
options, filesystem labels) is the fixed internal namespace, like `ubuntu` in an
Ubuntu derivative. The *displayed* name comes from the brand kit: every
user-visible "SynOS" in the staged package trees (window titles, .desktop
entries, translations, descriptions) is replaced by the brand's display_name at
build time, so a corporate brand kit renames the whole distribution. Literals
listed in packages/_lib/brand-keep.txt are left alone.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)
_TIMINGS_SPEC = importlib.util.spec_from_file_location("build_timings", ROOT / "tools" / "build_timings.py")
build_timings = importlib.util.module_from_spec(_TIMINGS_SPEC)
assert _TIMINGS_SPEC.loader is not None
_TIMINGS_SPEC.loader.exec_module(build_timings)
_HOST_RESOURCES_SPEC = importlib.util.spec_from_file_location("host_resources_for_build_packages", ROOT / "tools" / "host_resources.py")
host_resources = importlib.util.module_from_spec(_HOST_RESOURCES_SPEC)
assert _HOST_RESOURCES_SPEC.loader is not None
_HOST_RESOURCES_SPEC.loader.exec_module(host_resources)
ManifestError = render_manifest.ManifestError

# Package builds are plain dpkg-deb packaging (copy files, run a small
# prebuild script, run dpkg-deb) — nothing like the container-based OS image
# builds tools/host_resources.py's own defaults are sized for (a debootstrap
# chroot at MIN_FREE_GB=40, MIN_MEMORY_GB_PER_JOB=4). Same policy (disk,
# memory, CPUs; never a second one), different, much lighter, numbers for
# this workload; free_store_gb is fixed at None in resolve_jobs() below
# because building .debs never touches a container runtime's storage, so
# that constraint does not apply here (and would be actively wrong if podman
# happens to be installed and its unrelated store happens to be nearly full).
PACKAGE_MIN_FREE_GB = 2.0            # scratch space per concurrent build under .build/packages-work
# Most recipes are dpkg-deb, gpg and msgfmt, which need almost nothing — but a
# handful compile Rust (the four GTK applications) or a large C++ tree, and rustc
# alone can hold well over a gigabyte on a single crate. The measured speedup
# saturates once the worker count passes about twenty, because wall time then
# tracks the slowest single package rather than the total work, so budgeting two
# gigabytes a job costs nothing on a large machine and keeps a laptop from
# meeting the out-of-memory killer halfway through a build.
PACKAGE_MIN_MEMORY_GB_PER_JOB = 2.0
PACKAGE_CPUS_PER_JOB = 1             # a single package build rarely uses more than one core at a time
# These low, per-job numbers are also why --jobs auto is the *default* (not
# an opt-in): even a modest machine gets a sane handful of workers rather
# than being refused or throttled to 1, so nobody has to remember to pass
# the flag to get the speedup this module exists to provide.


def resolve_jobs(requested: str) -> tuple[int, list[str]]:
    """--jobs "auto" (the default) derives the safe worker count from this
    machine's own disk, memory and CPUs (tools/host_resources.py, the same
    formula tools/build_matrix.py and tools/catalog_conformance.py use),
    sized for plain package builds via PACKAGE_MIN_FREE_GB et al. above. An
    explicit count above what the machine can safely feed is refused with
    the reason, not silently capped; --jobs 1 always means today's plain
    serial loop, the explicit escape hatch when a build must force it."""
    return host_resources.resolve_jobs(requested, ROOT, min_free_gb=PACKAGE_MIN_FREE_GB,
                                       min_memory_gb_per_job=PACKAGE_MIN_MEMORY_GB_PER_JOB,
                                       cpus_per_job=PACKAGE_CPUS_PER_JOB, free_store_gb=None)


# ------------------------------------------------------- concurrency guards
# Packages build independently (separate work/<name> trees, separate
# fingerprint stamps, separate .deb outputs), so most of the loop below is
# safe to run concurrently with no coordination at all. A few things are
# not, because they are genuinely shared, mutable state a concurrent build
# can race another one over; each gets a lock keyed by the resource itself
# so unrelated packages never wait on each other:
#   (work/src/_lib, the one copy of packages/_lib every package's staged
#   tree reads through its lib -> ../_lib symlink, is not on this list: it
#   is not locked, it is never written while a build runs. main() stages it
#   once, before the first package starts, and nothing after that point
#   removes or rewrites it — see stage_shared_lib(). It used to be refreshed
#   in place from stage_recipe() under a "stage-lib" lock, which serialized
#   refresh against refresh but not against a prebuild.sh already reading
#   it: a real build lost gnome-shell-extension-accent-icons-theme to
#   "can't open file .../lib/resolve-gnome-ext.py" that way.)
#   - fetch_fork()'s .build/forks/<distro-suite-component-arch>.Packages.gz
#     index and cached .deb: two fork.json recipes for the same base/suite/
#     component/arch (e.g. synos-software-properties-common and
#     plymouth-synos, both following BASE=debian) share the same index file;
#   - fetch_git_commit() (packages/_lib/build-guards.sh): its cache key is
#     the pinned (url, commit), and it takes no lock of its own. Two recipes
#     that vendor the same upstream commit — synos-fluent-gtk-theme and
#     synos-gdm3-wallpaper both pin vinceliuice/Fluent-gtk-theme.git@
#     7a49a464b0188c340101c52965c18190b1c694cf — would otherwise have one's
#     `rm -rf "$cache"` racing the other's `git fetch` into the same
#     directory;
#   - resolve-gnome-ext.py (packages/_lib) --download: found by actually
#     running a concurrent build and watching several of the ~14
#     gnome-shell-extension-* recipes that call it fail with
#     zipfile.BadZipFile / EOFError / "invalid distance too far back" —
#     textbook corruption from two processes writing and reading the same
#     file, because it wrote every download to one hardcoded path,
#     /tmp/gnome-ext-poc.zip. Fixed at the source (it now uses
#     tempfile.mkstemp, a unique path per call, so it needs no lock of its
#     own), and the lock is kept anyway — belt and braces, since it also
#     covers anything else that reaches that call, known or not;
#   - $HOME/.rustup, the one rustup home every Rust recipe shares. rustup
#     names its download for the *file* and not the caller, so two recipes
#     fetching the same component both write the same
#     ~/.rustup/downloads/<hash>.partial and one loses the rename: a real
#     amd64 build of synos-swapcontrol-gtk and synos-yubikey-manager failed
#     with "could not rename downloaded file", at the identical hash in
#     both messages. Nothing in either recipe spells .rustup, so the
#     literal-path scan below cannot see this hazard; the signal is the
#     recipe invoking rustup or cargo at all (on these images cargo *is* a
#     rustup shim — see bases/*/Containerfile — so a plain `cargo build`
#     can trigger the download by itself, which is exactly what happened:
#     neither recipe called rustup directly). Dropping the unnecessary
#     targets = [...] from those recipes' rust-toolchain.toml files (and
#     check_rust_toolchain_pin refusing it from now on) takes away the only
#     reason two recipes collide here today, so this lock is belt and
#     braces like the gnome-ext one: one shared key for every Rust recipe,
#     because the hazard is one shared directory rather than one shared
#     file, kept so the next pair of Rust recipes cannot reintroduce it;
#   - any other absolute path under /tmp, /var/tmp or /dev/shm a recipe's
#     own scripts reference that source_cache_keys() does not otherwise
#     recognize. gnome-ext-poc.zip was found by running a build, not by
#     reading the code — the next one won't be caught that way twice. A
#     recipe that hardcodes a shared path outside its own work tree and
#     isn't one of the patterns above is unknown, not proven safe, so it is
#     locked by that exact path (see UNKNOWN_PATH_RE below) rather than let
#     through: a little speed given up, never correctness. The lock's key is
#     the path text, not the package, so two recipes that reference the
#     identical unknown path are the ones actually serialized against each
#     other; a recipe with its own distinct unknown path only waits on
#     another recipe that shares that same literal path, never on the rest
#     of the build. Either way, run_prebuild() prints a note naming the
#     recipe and the path, because the point of locking it is not to hide
#     the problem — it's to tell whoever added the recipe to fix it.
# source_cache_keys() finds all of this by a static scan of the recipe's own
# shell scripts, so a future recipe that repeats a commit, calls into
# resolve-gnome-ext.py, or hardcodes some other shared path, is covered too,
# not just today's instances of each.
_KEYED_LOCKS: dict[tuple, threading.Lock] = {}
_KEYED_LOCKS_GUARD = threading.Lock()


_PRINT_LOCK = threading.Lock()  # guards the one direct-from-a-worker-thread print below (unknown-path notes)


def _lock_for(key: tuple) -> threading.Lock:
    with _KEYED_LOCKS_GUARD:
        lock = _KEYED_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEYED_LOCKS[key] = lock
        return lock


_SHELL_VAR_ASSIGN_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=["\']?([^"\'\n]*)["\']?\s*(?:#.*)?$', re.MULTILINE)
_FETCH_GIT_COMMIT_RE = re.compile(r'^\s*fetch_git_commit\s+(\S+)\s+(\S+)\s+(\S+)', re.MULTILINE)
_GNOME_EXT_DOWNLOAD_RE = re.compile(r'resolve-gnome-ext\.py\b[^\n]*--download')
# rustup or cargo named as a word on a line that is not a comment: either one
# goes through the rustup proxy and can write $HOME/.rustup (`need_cmd cargo`
# counts — a recipe that checks for cargo is about to run it). Case-sensitive
# and lowercase, so CARGO_TARGET_*_LINKER and Cargo.toml do not match; the
# comment skip is what keeps prose about cargo from locking a recipe that
# never runs it (synos-whisper-worker's build-state-metrics.sh).
_RUSTUP_HOME_RE = re.compile(r'(?<![-\w/.])(?:rustup|cargo)(?![-\w.])')
# /tmp, /var/tmp, /dev/shm: the writable, host-wide scratch locations a
# script can hardcode a shared path under (every real instance found in this
# repository, including gnome-ext-poc.zip, was a literal /tmp/... path).
# Not preceded by a word character, '.', '/' or ':' so this does not fire
# inside a longer path (/opt/synos/tmp/x) or a URL's own path component.
UNKNOWN_PATH_RE = re.compile(r'(?<![\w./:])/(?:var/tmp|dev/shm|tmp)/[\w./+-]+')


def source_cache_keys(source: Path) -> set[tuple[str, ...]]:
    """Best-effort static scan of a recipe's own shell and Python scripts
    (prebuild.sh and anything under upstream/) for shared-resource hazards:
    `fetch_git_commit <url> <commit>` calls (resolving simple VAR="value"
    assignments made in the same file), any call into resolve-gnome-ext.py
    with --download, any invocation of rustup or cargo, and — catching
    whatever the first three do not — any other literal /tmp, /var/tmp or
    /dev/shm path (UNKNOWN_PATH_RE) that is not part of one of the two
    recognized call patterns.

    Two packages whose scan comes back with the same (url, commit) pin the
    identical upstream commit; any package that calls resolve-gnome-ext.py
    --download shares that call's one lock with every other one that does
    (the underlying file is unique per call now — see resolve-gnome-ext.py —
    but the lock stays, belt and braces). A "rustup-home" key works the same
    way and for the same reason: the shared resource is the whole of
    $HOME/.rustup, which no recipe names in so many words — rustup's
    toolchain and component downloads land there whichever recipe asked, and
    two recipes fetching one component race over a single .partial file — so
    every recipe that runs rustup, or runs cargo (the rustup proxy, which
    can fetch a component by itself), gets that one key and they are
    serialized against each other, not against the rest of the build. An
    "unknown-path" key is locked by its exact path text, so only recipes
    that reference the identical unrecognized path are serialized against
    each other, not the whole build; run_prebuild() prints a note when it
    acquires one, naming the recipe and the path, so this is visible rather
    than just silently safe.
    Everyone else's keys come back empty or disjoint and are never locked
    against one another."""
    scripts = []
    prebuild = source / "prebuild.sh"
    if prebuild.is_file():
        scripts.append(prebuild)
    upstream = source / "upstream"
    if upstream.is_dir():
        scripts.extend(p for p in upstream.rglob("*.sh") if p.is_file())
        scripts.extend(p for p in upstream.rglob("*.py") if p.is_file())
    keys: set[tuple[str, ...]] = set()
    for script in scripts:
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        assigns = dict(_SHELL_VAR_ASSIGN_RE.findall(text))

        def resolve(token: str) -> str:
            var = re.fullmatch(r'"?\$\{?(\w+)\}?"?', token)
            return assigns.get(var.group(1), token) if var else token.strip('"\'')

        recognized_dests = []  # fetch_git_commit's own <dest>, resolved: not just that call's line, the
        for match in _FETCH_GIT_COMMIT_RE.finditer(text):  # whole path it hands back (cd/rm-ed elsewhere
            url, commit, dest = match.group(1), match.group(2), match.group(3)  # in the same script) is its
            keys.add(("git-commit", url, resolve(commit)))                     # cache, not a new hazard.
            recognized_dests.append(resolve(dest))
        if _GNOME_EXT_DOWNLOAD_RE.search(text):
            keys.add(("gnome-ext-download",))
        if any(_RUSTUP_HOME_RE.search(line) for line in text.splitlines()
               if not line.lstrip().startswith("#")):
            keys.add(("rustup-home",))
        for match in UNKNOWN_PATH_RE.finditer(text):
            path = match.group(0)
            if not any(path == dest or path.startswith(dest + "/") for dest in recognized_dests):
                keys.add(("unknown-path", path))
    return keys

# Signing key location. SYNOS_KEYS_DIR overrides the default keys/ (tests, CI).
KEYS_DIR = Path(os.environ.get("SYNOS_KEYS_DIR", str(ROOT / "keys")))
PUBLIC_KEY = KEYS_DIR / "public/synos-archive-keyring.gpg"
PRIVATE_KEY = KEYS_DIR / "private/synos-archive-keyring.sec"


def ensure_signing_key(brand_name: str) -> str:
    """Make sure a repository signing key exists; returns how it was obtained.

    Order: SYNOS_SIGNING_KEY (armored secret key in the environment, for CI),
    SYNOS_SIGNING_KEY_FILE (path), the key already under keys/private, and
    finally a new key generated on this machine. A generated key is meant for
    development builds: images ship its public half, so keep using the same
    keys/ directory or installed systems will stop trusting the repository.
    """
    if shutil.which("gpg") is None:
        raise PackageError("gpg is required to sign the package repository")
    PRIVATE_KEY.parent.mkdir(parents=True, exist_ok=True)
    PRIVATE_KEY.parent.chmod(0o700)
    PUBLIC_KEY.parent.mkdir(parents=True, exist_ok=True)
    armored = os.environ.get("SYNOS_SIGNING_KEY")
    key_file = os.environ.get("SYNOS_SIGNING_KEY_FILE")
    source = "existing"
    if armored:
        PRIVATE_KEY.write_text(armored, encoding="utf-8")
        source = "environment"
    elif key_file:
        shutil.copy(key_file, PRIVATE_KEY)
        source = "file"
    elif not PRIVATE_KEY.is_file():
        source = "generated"
    PRIVATE_KEY.chmod(0o600) if PRIVATE_KEY.exists() else None

    import tempfile
    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, GNUPGHOME=home)
        if source == "generated":
            uid = f"{brand_name} Package Repository <packages@localhost>"
            subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-gen-key", uid, "ed25519", "sign", "0"],
                           env=env, check=True, capture_output=True)
            secret = subprocess.run(["gpg", "--batch", "--export-secret-keys", "--armor"], env=env, check=True, capture_output=True).stdout
            PRIVATE_KEY.write_bytes(secret)
            PRIVATE_KEY.chmod(0o600)
        else:
            subprocess.run(["gpg", "--batch", "--quiet", "--import", str(PRIVATE_KEY)], env=env, check=True, capture_output=True)
        public = subprocess.run(["gpg", "--batch", "--export"], env=env, check=True, capture_output=True).stdout
        if not public:
            raise PackageError(f"{PRIVATE_KEY} holds no usable secret key")
        PUBLIC_KEY.write_bytes(public)
    return source


class PackageError(Exception):
    pass


def substitutions(manifest: dict, brand: dict, base: dict) -> dict[str, str]:
    packages_cfg = manifest.get("packages") or {}
    repo_url = packages_cfg.get("repository", "") or ""
    urls = brand.get("urls") or {}
    from urllib.parse import urlparse
    check_display_name(brand["display_name"])
    effective_url = repo_url or "https://packages.synos.example/"
    if not effective_url.endswith("/"):
        effective_url += "/"
    return {
        "REPO_HOST": urlparse(effective_url).hostname or "packages.synos.example",
        "MIRROR": base.get("APT_MIRROR", ""),
        "VERSION": manifest["version"],
        "BASE": base["BASE_ID"],
        "SUITE": manifest["suite"],
        "ARCH": manifest["arch"],
        "REPO_URL": effective_url,
        "REPO_ENABLED": "yes" if repo_url else "no",
        "BRAND_ID": brand["id"],
        "BRAND_NAME": brand["display_name"],
        "BRAND_VENDOR": brand.get("vendor", brand["display_name"]),
        "BRAND_URL_HOME": urls.get("home", ""),
        "BRAND_URL_SUPPORT": urls.get("support", ""),
    }


def render_text(text: str, subs: dict[str, str], strict: bool = True) -> str:
    """Substitute the known ${KEY} placeholders. Vendored scripts and templates
    use shell syntax like ${DPKG_ROOT:-} which is not a placeholder, so only
    all-caps keys that look like ours are checked when strict."""
    for key, value in subs.items():
        text = text.replace("${" + key + "}", value)
    if strict:
        leftover = sorted({t.split("}")[0] for t in text.split("${")[1:] if "}" in t
                           and re.fullmatch(r"(VERSION|BASE|SUITE|ARCH|REPO_URL|REPO_HOST|REPO_ENABLED|BRAND_[A-Z_]+)", t.split("}")[0])})
        if leftover:
            raise PackageError("unresolved substitution(s): " + ", ".join("${" + t + "}" for t in leftover))
    return text


PRODUCT_NAME = "SynOS"
PRODUCT_RE = re.compile(r"(?<![A-Za-z0-9_])SynOS(?![A-Za-z0-9_])")
BRAND_KEEP = ROOT / "packages" / "_lib" / "brand-keep.txt"
BRAND_MAX_BYTES = 32 * 1024 * 1024


def check_display_name(name: str) -> None:
    """The display name is spliced into source strings, .desktop files and
    translations verbatim, so it must not carry quoting or line breaks."""
    if not name.strip():
        raise PackageError("brand display_name must not be empty")
    bad = [c for c in '"\\\n\r\t' if c in name]
    if bad:
        raise PackageError(f"brand display_name {name!r} must not contain quotes, backslashes or line breaks")


def brand_keep_list() -> list[str]:
    if not BRAND_KEEP.is_file():
        return []
    return [line.strip() for line in BRAND_KEEP.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def brand_text(text: str, subs: dict[str, str], po: bool = False) -> str:
    """Replace the displayed product name with the brand's display name.

    Word boundaries keep identifiers (SynOSBtrfsSnapshotsManager) and the lowercase
    namespace (synos-*, com.synos.*) intact; hyphenated compounds such as the German
    "SynOS-Installer" are display text and are rebranded. `po` escapes the name for
    gettext string literals."""
    display = subs["BRAND_NAME"]
    if display == PRODUCT_NAME or PRODUCT_NAME not in text:
        return text
    if po:
        display = display.replace("\\", "\\\\").replace('"', '\\"')
    keep = brand_keep_list()
    sentinels: list[tuple[str, str]] = []
    for index, literal in enumerate(keep):
        if literal in text:
            token = f"\x00KEEP{index}\x00"
            text = text.replace(literal, token)
            sentinels.append((token, literal))
    text = PRODUCT_RE.sub(lambda _match: display, text)
    for token, literal in sentinels:
        text = text.replace(token, literal)
    return text


def brand_mo(path: Path, subs: dict[str, str], log: list[str]) -> bool:
    """Compiled translations are binary: decompile, rebrand, recompile."""
    if not (shutil.which("msgunfmt") and shutil.which("msgfmt")):
        log.append(f"brand: gettext tools missing, {path.name} keeps the product name")
        return False
    dumped = subprocess.run(["msgunfmt", str(path)], capture_output=True, check=False)
    if dumped.returncode != 0:
        log.append(f"brand: msgunfmt failed on {path}: {dumped.stderr.decode(errors='replace')[-200:]}")
        return False
    original = dumped.stdout.decode("utf-8", errors="surrogateescape")
    branded = brand_text(original, subs, po=True)
    if branded == original:
        return False
    compiled = subprocess.run(["msgfmt", "-o", str(path), "-"], input=branded.encode("utf-8", errors="surrogateescape"),
                              capture_output=True, check=False)
    if compiled.returncode != 0:
        log.append(f"brand: msgfmt failed on {path}: {compiled.stderr.decode(errors='replace')[-200:]}")
        return False
    return True


def brand_tree(pkg: Path, subs: dict[str, str], log: list[str]) -> int:
    """Apply brand_text to every text file of a staged package tree; returns the
    number of files changed. Binaries other than .mo are left alone (bitmaps
    carrying a wordmark are replaced by the brand kit package instead)."""
    if subs["BRAND_NAME"] == PRODUCT_NAME:
        return 0
    changed = 0
    needle = PRODUCT_NAME.encode()
    for path in sorted(pkg.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > BRAND_MAX_BYTES:
            continue
        data = path.read_bytes()
        if needle not in data:
            continue
        if path.suffix == ".mo":
            changed += brand_mo(path, subs, log)
            continue
        if b"\x00" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        branded = brand_text(text, subs, po=path.suffix in (".po", ".pot"))
        if branded != text:
            mode = path.stat().st_mode
            path.write_text(branded, encoding="utf-8")
            path.chmod(stat.S_IMODE(mode))
            changed += 1
    return changed


def apply_base_fields(control: str, base: str) -> str:
    """`Depends[debian]: …` replaces `Depends:` when building for that base; other
    bases' bracketed lines are dropped. Lets one control carry per-base lists."""
    lines = control.split("\n")
    overrides: dict[str, str] = {}
    kept: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Za-z-]+)\[([a-z0-9-]+)\]:\s*(.*)$", line)
        if match:
            if match.group(2) == base:
                overrides[match.group(1)] = match.group(3)
            continue
        kept.append(line)
    out: list[str] = []
    seen: set[str] = set()
    for line in kept:
        match = re.match(r"^([A-Za-z-]+):", line)
        if match and match.group(1) in overrides:
            out.append(f"{match.group(1)}: {overrides[match.group(1)]}")
            seen.add(match.group(1))
        else:
            out.append(line)
    for field, value in overrides.items():
        if field not in seen:
            # add before Description, which must stay last
            index = next((i for i, l in enumerate(out) if l.startswith("Description:")), len(out))
            out.insert(index, f"{field}: {value}")
    return "\n".join(out)


RUST_TOOLCHAIN_VERSION_FILE = ROOT / "bases" / "rust-toolchain.txt"
RUST_TOOLCHAIN_CHANNEL_RE = re.compile(r'^\s*channel\s*=\s*"([^"]*)"\s*$', re.MULTILINE)
RUST_TOOLCHAIN_TARGETS_RE = re.compile(r'^\s*targets\s*=\s*(.*)$', re.MULTILINE)


def _relpath(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_rust_toolchain_pin(source: Path) -> None:
    """A vendored packages/<name>/upstream/rust-toolchain.toml must name the
    exact version bases/rust-toolchain.txt pins, never a floating channel like
    "stable": that channel is resolved by rustup on whatever day the build
    happens to run, needs a network fetch inside the package build, and can
    break the image without a single line of this repository changing (see
    bases/rust-toolchain.txt).

    It must not carry a `targets` list either, for the same reason: rustup
    acts on that list the moment any cargo call in the package reads the
    file, so one cross target named there is a rust-std download on every
    build on every architecture, before anything checks whether the build
    needs it. The target belongs in the branch of upstream/build.sh that
    actually cross-compiles, which is the only place it gets used."""
    toolchain_file = source / "upstream" / "rust-toolchain.toml"
    if not toolchain_file.is_file():
        return
    pinned = RUST_TOOLCHAIN_VERSION_FILE.read_text(encoding="utf-8").strip()
    text = toolchain_file.read_text(encoding="utf-8")
    match = RUST_TOOLCHAIN_CHANNEL_RE.search(text)
    channel = match.group(1) if match else "(no channel key)"
    if channel != pinned:
        raise PackageError(
            f"{_relpath(toolchain_file)} pins channel \"{channel}\", but "
            f"{_relpath(RUST_TOOLCHAIN_VERSION_FILE)} pins \"{pinned}\"; set "
            f"channel = \"{pinned}\" in {_relpath(toolchain_file)}"
        )
    targets = RUST_TOOLCHAIN_TARGETS_RE.search(text)
    if targets:
        raise PackageError(
            f"{_relpath(toolchain_file)} declares targets = "
            f"{targets.group(1).strip()}; rustup downloads every target listed "
            f"there on the first cargo call, on every architecture, before "
            f"anything checks whether the build needs it. Drop the targets key "
            f"and run `rustup target add <triple>` in the branch of "
            f"upstream/build.sh that cross-compiles"
        )


def copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        target = destination / path.relative_to(source)
        if path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(os.readlink(path))
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target)
            target.chmod(0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)



# ------------------------------------------------------------- build steps
class SkipPackage(Exception):
    """The package cannot be built on this host (missing tool, no network); not an error."""


def run_prebuild(source: Path, subs: dict[str, str], log: list[str]) -> None:
    script = source / "prebuild.sh"
    if not script.is_file():
        return
    if os.environ.get("SYNOS_SKIP_PREBUILD"):
        raise SkipPackage("SYNOS_SKIP_PREBUILD is set")
    # Upstream scripts compare sorted lists; glob order must not depend on the host locale.
    env = dict(os.environ, ARCH=subs["ARCH"], SUITE=subs["SUITE"], BASE=subs["BASE"], LC_ALL="C.UTF-8", LANG="C.UTF-8")
    env.setdefault("SYNOS_SOURCE_CACHE", str(ROOT / ".build" / "sources"))
    # Hold a lock per shared resource this script's own scan turned up (see
    # source_cache_keys and the module-level comment near _lock_for): known
    # patterns get their specific lock silently, same as always; an
    # unrecognized /tmp, /var/tmp or /dev/shm path gets a note naming the
    # recipe and the path, because the lock keeps the build correct but the
    # note is what gets the recipe itself fixed.
    keys = sorted(source_cache_keys(source))
    unknown_paths = [key[1] for key in keys if key[0] == "unknown-path"]
    if unknown_paths:
        with _PRINT_LOCK:
            for path in unknown_paths:
                print(f"note: {source.name} references {path} outside its own work tree; not a recognized "
                      "shared-resource pattern, so it is built serially against any other recipe that "
                      "references the same path — fix the recipe to use a per-invocation temp path instead",
                      file=sys.stderr)
    cache_locks = [_lock_for(("source-cache",) + key) for key in keys]
    with contextlib.ExitStack() as stack:
        for lock in cache_locks:
            stack.enter_context(lock)
        result = subprocess.run(["bash", str(script)], cwd=source, env=env, capture_output=True, text=True, check=False)
    log.append(result.stdout[-4000:] + result.stderr[-4000:])
    if result.returncode != 0:
        tail = [line for line in (result.stderr or result.stdout).strip().splitlines() if line.strip()][-8:]
        # A failing prebuild is a host problem (missing tool or library, no
        # network) far more often than a package problem: record it as skipped
        # so the rest of the repository still builds; --strict turns it fatal.
        raise SkipPackage("prebuild.sh failed: " + " | ".join(tail))


def apply_includes(source: Path, pkg: Path, subs: dict[str, str]) -> None:
    """Copy the build-time includes (includes.txt) from upstream/ or obj/<arch>/ into the package tree."""
    table = source / "includes.txt"
    if not table.is_file():
        return
    for raw in table.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("#"):
            continue
        kind, src, dst, mode = raw.split("\t")
        src = (src.replace("$(Arch)", subs["ARCH"]).replace("$(Suite)", subs["SUITE"] + "-addon")
               .replace("${SUITE}", subs["SUITE"]).replace("obj/*", "obj/" + subs["ARCH"]))
        origin = source / "upstream" / src
        if src.startswith("../"):
            # a file from the upstream repository root (LICENSE): kept in packages/_lib
            origin = ROOT / "packages" / "_lib" / src[3:]
        target = pkg / dst.lstrip("/")
        if kind == "folder":
            if not origin.is_dir():
                raise PackageError(f"{source.name}: include folder {src} was not produced by prebuild.sh")
            copy_tree(origin, target)      # keeps symlinks as symlinks (icon themes link heavily)
        else:
            if not origin.is_file():
                raise PackageError(f"{source.name}: include file {src} was not produced by prebuild.sh")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(origin, target)
            target.chmod(int(mode, 8))


def _fetch(url: str, dest: Path) -> None:
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "Debian APT-HTTP/1.3 (synos build)"})
    with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _fetch_index(base_url: str, suite: str, component: str, arch: str, dest: Path) -> None:
    """Packages.gz, then Packages.xz, then plain Packages; stored gzip-compressed."""
    import gzip
    import lzma
    last = None
    for name in ("Packages.gz", "Packages.xz", "Packages"):
        raw = dest.with_suffix(".tmp")
        try:
            _fetch(f"{base_url}/dists/{suite}/{component}/binary-{arch}/{name}", raw)
        except Exception as exc:
            last = exc
            continue
        data = raw.read_bytes()
        raw.unlink()
        if name.endswith(".xz"):
            data = gzip.compress(lzma.decompress(data))
        elif not name.endswith(".gz"):
            data = gzip.compress(data)
        dest.write_bytes(data)
        return
    raise SkipPackage(f"cannot fetch the package index {base_url}/dists/{suite}/{component}/binary-{arch}: {last}")


def fetch_fork(source: Path, subs: dict[str, str], pkg: Path) -> dict[str, str]:
    """Download the upstream binary package named in fork.json, unpack it into pkg/, return its control fields."""
    import gzip
    import json as _json
    spec = _json.loads((source / "fork.json").read_text(encoding="utf-8"))
    suite = spec.get("suite", "$(Suite)").replace("$(Suite)", subs["SUITE"] + "-addon")
    for pair in (spec.get("suite_mapping") or "").replace(",,", ",").split(","):
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            if k == suite:
                suite = v
    arch = spec.get("arch", "$(Arch)").replace("$(Arch)", subs["ARCH"])
    index_arch = subs["ARCH"] if arch == "all" else arch     # arch-all packages are listed in the arch index
    component = spec.get("component", "main").replace("$(Component)", "main")
    base_url = spec["url"].rstrip("/")
    if spec.get("distro") in ("ubuntu", "debian"):
        # A fork of a base package: taken from the archive of the base being
        # built, with per-base overrides (package or component names differ).
        override = (spec.get("by_base") or {}).get(subs["BASE"], {})
        spec = dict(spec, **override)
        base_url = os.environ.get("SYNOS_FORK_MIRROR", subs.get("MIRROR", "")).rstrip("/")
        suite = subs["SUITE"]
        component = override.get("component", component if subs["BASE"] == spec.get("distro") else "main")
        spec["distro"] = subs["BASE"]
    if os.environ.get("SYNOS_SKIP_PREBUILD"):
        raise SkipPackage("SYNOS_SKIP_PREBUILD is set (forks need network)")
    cache = ROOT / ".build" / "forks"
    index = cache / f"{spec['distro']}-{suite}-{component}-{index_arch}.Packages.gz"
    if not index.is_file():
        # Two fork.json recipes following the same base/suite/component/arch
        # (e.g. synos-software-properties-common and plymouth-synos, both
        # BASE=debian) share this exact index file; without a lock, two
        # concurrent builds racing the same "not there yet, fetch it" check
        # would both fetch and one's write could interleave with the other's.
        with _lock_for(("fork-index", str(index))):
            if not index.is_file():
                _fetch_index(base_url, suite, component, index_arch, index)
    stanza = None
    with gzip.open(index, "rt", encoding="utf-8", errors="replace") as handle:
        for block in handle.read().split("\n\n"):
            if block.startswith(f"Package: {spec['package']}\n") or f"\nPackage: {spec['package']}\n" in "\n" + block:
                stanza = block
    if stanza is None:
        raise PackageError(f"{source.name}: upstream package {spec['package']} not in {suite}/{component}/{arch}")
    fields = {}
    for line in stanza.splitlines():
        if line and not line.startswith(" ") and ":" in line:
            k, v = line.split(":", 1)
            fields[k] = v.strip()
    deb = cache / Path(fields["Filename"]).name
    if not deb.is_file():
        with _lock_for(("fork-deb", str(deb))):
            if not deb.is_file():
                try:
                    _fetch(f"{base_url}/{fields['Filename']}", deb)
                except Exception as exc:
                    raise SkipPackage(f"cannot download {fields['Filename']}: {exc}") from exc
    subprocess.run(["dpkg-deb", "-x", str(deb), str(pkg)], check=True, capture_output=True)
    if not spec.get("suppress_scripts"):
        subprocess.run(["dpkg-deb", "-e", str(deb), str(pkg / "DEBIAN")], check=True, capture_output=True)
    return fields



UPSTREAM_OUTPUTS = {"deploy", "obj", "target", "locale"}     # written by prebuild.sh under upstream/
SCRATCH_DIRS = {"__pycache__", ".git", "node_modules"}


def is_recipe_output(rel: Path) -> bool:
    """True for paths a prebuild produces (not recipe inputs): upstream/deploy,
    upstream/obj, upstream/target, upstream/locale and scratch directories."""
    parts = rel.parts
    if any(part in SCRATCH_DIRS for part in parts):
        return True
    return len(parts) > 1 and parts[0] == "upstream" and parts[1] in UPSTREAM_OUTPUTS


def rmtree_tolerant(path: Path) -> None:
    """Remove a work directory; files a container build left behind as root
    cannot be removed by the local user and are reported as a SkipPackage."""
    if not path.exists() and not path.is_symlink():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise SkipPackage(f"cannot reset {path} ({exc.strerror}); it was probably created by a container build as root "
                          "— run 'make clean' (uses sudo) and retry") from exc


def stage_shared_lib(work: Path) -> Path:
    """Stage packages/_lib as work/src/_lib, replacing whatever is there.

    Called exactly once per run, by main(), before any package is built —
    on the serial path and before the thread pool exists on the --jobs
    path — and never again during the run. That ordering is the whole
    guarantee: every prebuild.sh reads _lib through its lib -> ../_lib
    symlink, and from the moment the first package starts until the run
    ends nothing removes, rewrites or partially copies it, so no reader can
    ever see it absent or half-present. No lock is needed or taken.

    It is replaced unconditionally rather than "refreshed if stale": the
    old check compared source mtimes against the staged directory's mtime,
    which a resumed .build cache volume or an mtime-preserving rsync can
    fool in either direction (a stale copy that looks new, or a source in
    the host's future that makes every call look stale and re-copy). The
    directory is a handful of small files, so copying it every run is cheaper
    than any check honest enough to trust, and the staged copy is always
    exactly the current packages/_lib — no leftovers from an earlier run."""
    lib = work / "src" / "_lib"
    rmtree_tolerant(lib)
    copy_tree(ROOT / "packages" / "_lib", lib)
    lib.mkdir(parents=True, exist_ok=True)  # an empty packages/_lib still yields the directory
    return lib


def stage_recipe(source: Path, work: Path) -> Path:
    """Copy a recipe (inputs only) into the work tree, where prebuild.sh runs.
    Keeps prebuild outputs and root-owned container leftovers out of packages/.

    Touches only work/src/<recipe>. The shared work/src/_lib its lib symlink
    points at is staged once by main() before any package starts (see
    stage_shared_lib) and is deliberately never written here: concurrent
    packages all call this, and other packages' prebuild.sh may be reading
    _lib at that moment. A missing _lib means the caller skipped that step,
    which is a bug in the caller, so it is refused rather than papered over
    by staging it here, mid-build."""
    src_root = work / "src"
    lib = src_root / "_lib"
    if not lib.is_dir():
        raise PackageError(f"{lib} is not staged; stage_shared_lib() must run once before any package is built")
    staged = src_root / source.name
    rmtree_tolerant(staged)
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if is_recipe_output(rel):
            continue
        target = staged / rel
        if path.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(path))
        elif path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, target)
            target.chmod(0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644)
    return staged


def finish_control(control: str, subs: dict[str, str]) -> str:
    """The rendered control file as dpkg accepts it. A brand kit may leave its
    URLs empty; a field with no value (`Homepage: `) makes dpkg's status
    database unparsable and every later package fails to configure, so such
    fields are dropped, an empty maintainer address gets a placeholder, and the
    file ends with exactly one newline."""
    lines = []
    for line in control.splitlines():
        if re.match(r"^[A-Za-z][A-Za-z0-9-]*:\s*$", line):
            continue
        if line.startswith("Maintainer:") and re.search(r"<\s*>", line):
            owner = subs.get("BRAND_ID") or "synos"
            line = re.sub(r"<\s*>", f"<{owner}@localhost>", line)
        lines.append(line.rstrip())
    return "\n".join(lines).rstrip("\n") + "\n"


def recipe_fingerprint(source: Path, subs: dict[str, str]) -> str:
    """Hash of everything that influences a package's build: the recipe files
    (not the build outputs under upstream/), the substitutions, the brand kit
    inputs it depends on, and the builder itself."""
    import hashlib
    digest = hashlib.sha256()
    digest.update(json.dumps(subs, sort_keys=True).encode())
    digest.update(Path(__file__).read_bytes())
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if is_recipe_output(rel) or path.is_dir():
            continue
        digest.update(str(rel).encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        else:
            digest.update(path.read_bytes())
    for shared in sorted((ROOT / "packages" / "_lib").rglob("*")):
        if shared.is_file():
            digest.update(shared.read_bytes())
    if PUBLIC_KEY.is_file():
        digest.update(PUBLIC_KEY.read_bytes())
    return digest.hexdigest()

def build_package(source: Path, work: Path, subs: dict[str, str]) -> Path:
    name = source.name
    control_path = source / "control"
    if not control_path.is_file():
        raise PackageError(f"packages/{name}/control is missing")
    control = brand_text(apply_base_fields(render_text(control_path.read_text(encoding="utf-8"), subs), subs["BASE"]), subs)
    if f"Package: {name}\n" not in control:
        raise PackageError(f"packages/{name}/control must declare Package: {name}")
    version = next((line.split(":", 1)[1].strip() for line in control.splitlines() if line.startswith("Version:")), None)
    arch = next((line.split(":", 1)[1].strip() for line in control.splitlines() if line.startswith("Architecture:")), "all")
    if not version:
        raise PackageError(f"packages/{name}/control has no Version")
    check_rust_toolchain_pin(source)

    pkg = work / name
    rmtree_tolerant(pkg)
    pkg.mkdir(parents=True)
    source = stage_recipe(source, work)       # prebuild.sh and fork downloads run on the copy, never in packages/
    log: list[str] = []
    upstream_fields: dict[str, str] = {}
    if (source / "fork.json").is_file():
        stage = (source / "upstream" / "obj" / subs["ARCH"]) if (source / "prebuild.sh").is_file() else pkg
        if stage != pkg:
            stage.mkdir(parents=True)
        upstream_fields = fetch_fork(source, subs, stage)
        version = version.replace("${UPSTREAM_VERSION}", upstream_fields.get("Version", "0"))
        control = control.replace("${UPSTREAM_VERSION}", upstream_fields.get("Version", "0"))
        # keep the upstream dependency fields the fork did not restate
        for field in ("Depends", "Pre-Depends", "Recommends", "Suggests", "Breaks", "Enhances"):
            if field in upstream_fields and f"\n{field}:" not in "\n" + control:
                control = control.replace("\nMaintainer:", f"\n{field}: {upstream_fields[field]}\nMaintainer:", 1)
    run_prebuild(source, subs, log)
    if (source / "prebuild.sh").is_file() and (source / "upstream" / "obj").is_dir():
        # commands operate on obj/<arch>/ as the future package root (fork recipes)
        obj = source / "upstream" / "obj" / subs["ARCH"]
        if obj.is_dir():
            copy_tree(obj, pkg)
    if (source / "assets").is_dir():
        copy_tree(source / "assets", pkg)
    apply_includes(source, pkg, subs)
    if (source / "templates").is_dir():
        for path in (source / "templates").rglob("*"):
            if path.is_file():
                target = pkg / path.relative_to(source / "templates")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_text(path.read_text(encoding="utf-8"), subs), encoding="utf-8")
                target.chmod(0o644)

    rebranded = brand_tree(pkg, subs, log)
    if rebranded:
        log.append(f"brand: {rebranded} file(s) now show {subs['BRAND_NAME']!r} instead of {PRODUCT_NAME!r}")

    # The keyring package embeds the public key when the project has one.
    if name == "synos-archive-keyring":
        keyring = pkg / "usr/share/keyrings/synos-archive-keyring.gpg"
        keyring.parent.mkdir(parents=True, exist_ok=True)
        if PUBLIC_KEY.is_file():
            shutil.copy(PUBLIC_KEY, keyring)
        else:
            keyring.write_bytes(b"")
            print("note: keys/public/synos-archive-keyring.gpg is missing; synos-archive-keyring ships an empty keyring "
                  "(run 'make repo-key')", file=sys.stderr)

    debian = pkg / "DEBIAN"
    debian.mkdir(exist_ok=True)
    (debian / "control").write_text(finish_control(control, subs), encoding="utf-8")
    if (source / "conffiles").is_file():
        shutil.copy(source / "conffiles", debian / "conffiles")
    if (source / "triggers").is_file():
        shutil.copy(source / "triggers", debian / "triggers")
    if (source / "scripts").is_dir():
        for script in ("preinst", "postinst", "prerm", "postrm"):
            candidate = source / "scripts" / script
            if candidate.is_file():
                (debian / script).write_text(brand_text(render_text(candidate.read_text(encoding="utf-8"), subs), subs),
                                             encoding="utf-8")
                (debian / script).chmod(0o755)
    for path in pkg.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)

    deb = work / f"{name}_{version}_{arch}.deb"
    builder = ["fakeroot"] if shutil.which("fakeroot") else []
    result = subprocess.run(builder + ["dpkg-deb", "--root-owner-group", "--build", str(pkg), str(deb)],
                            check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise PackageError(f"dpkg-deb failed for {name}: {result.stderr.strip()}")
    return deb


def build_repository(debs: list[Path], output: Path, subs: dict[str, str]) -> bool:
    """Flat repository: Packages, Packages.gz, Release (+ InRelease when a key exists)."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for deb in debs:
        shutil.copy(deb, output / deb.name)
    scan = subprocess.run(["dpkg-scanpackages", "--multiversion", "."], cwd=output, check=True, capture_output=True, text=True)
    (output / "Packages").write_text(scan.stdout, encoding="utf-8")
    subprocess.run(["gzip", "-9", "-k", "-f", "Packages"], cwd=output, check=True)
    release = subprocess.run(
        ["apt-ftparchive",
         "-o", f"APT::FTPArchive::Release::Origin={subs['BRAND_NAME']}",
         "-o", f"APT::FTPArchive::Release::Label={subs['BRAND_NAME']} local",
         "-o", "APT::FTPArchive::Release::Suite=local",
         "-o", f"APT::FTPArchive::Release::Codename={subs['SUITE']}",
         "-o", f"APT::FTPArchive::Release::Architectures={subs['ARCH']} all",
         "-o", "APT::FTPArchive::Release::Components=main",
         "-o", f"APT::FTPArchive::Release::Description={subs['BRAND_NAME']} packages built from packages/",
         "release", "."],
        cwd=output, check=True, capture_output=True, text=True)
    (output / "Release").write_text(release.stdout, encoding="utf-8")
    signed = False
    if PRIVATE_KEY.is_file() and shutil.which("gpg"):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, GNUPGHOME=home)
            subprocess.run(["gpg", "--batch", "--quiet", "--import", str(PRIVATE_KEY)], env=env, check=False, capture_output=True)
            result = subprocess.run(["gpg", "--batch", "--yes", "--clearsign", "-o", "InRelease", "Release"], cwd=output, env=env,
                                    check=False, capture_output=True, text=True)
            signed = result.returncode == 0
            if signed:
                subprocess.run(["gpg", "--batch", "--yes", "--detach-sign", "--armor", "-o", "Release.gpg", "Release"], cwd=output,
                               env=env, check=False, capture_output=True)
            else:
                print("note: signing the local repository failed: " + result.stderr.strip(), file=sys.stderr)
    (output / "SIGNED").write_text("yes\n" if signed else "no\n", encoding="utf-8")
    return signed


# Outcome tuples _build_source returns, handled by the caller (never printed
# or appended from inside a worker thread, so output stays in the same
# sorted-by-name order --jobs 1 always printed in, however many packages
# actually finish out of that order):
#   ("reused", deb_path)
#   ("built", deb_path)
#   ("skipped", name, reason)
# A PackageError is not caught here; it propagates to the caller (see main)
# exactly as it did from the serial loop, where it was always immediately
# fatal regardless of --strict.
def _build_source(source: Path, work: Path, fingerprints: Path, subs: dict[str, str], rebuild: bool):
    try:
        fingerprint = recipe_fingerprint(source, subs)
        stamp = fingerprints / source.name
        cached = sorted(work.glob(f"{source.name}_*.deb"))
        if not rebuild and cached and stamp.is_file() and stamp.read_text() == fingerprint:
            return ("reused", cached[-1])
        for old_deb in cached:
            old_deb.unlink()
        deb = build_package(source, work, subs)
        stamp.write_text(fingerprint)
        return ("built", deb)
    except SkipPackage as why:
        return ("skipped", source.name, str(why))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(ROOT / "manifest.yml"))
    parser.add_argument("--output", default=str(ROOT / ".build" / "repo"))
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail when any package is skipped (CI, container build)")
    parser.add_argument("--rebuild", action="store_true", help="ignore the recipe fingerprints and rebuild everything")
    parser.add_argument("--jobs", default="auto",
                        help="packages to build at once: a number, or \"auto\" (default) to derive the safe count "
                             "from this machine's disk, memory and CPUs (tools/host_resources.py, sized for this "
                             "workload — see PACKAGE_MIN_FREE_GB et al.). --jobs 1 is today's plain serial loop, "
                             "unchanged, and the explicit escape hatch if a build ever needs to force it")
    args = parser.parse_args(argv)
    try:
        manifest = render_manifest.load_yaml(Path(args.manifest).resolve())
        render_manifest.validate_schema(manifest, "manifest.schema.json")
        base = render_manifest.load_env(ROOT / "bases" / manifest["base"] / "base.env")
        brand = render_manifest.resolve_brand(manifest["brand"])
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1
    subs = substitutions(manifest, brand, base)
    sources = sorted(p for p in (ROOT / "packages").iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "control").is_file())
    if args.only:
        sources = [p for p in sources if p.name in set(args.only)]
    if args.list:
        for source in sources:
            print(source.name)
        return 0
    jobs, problems = resolve_jobs(args.jobs)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    # The package-building step is always the first phase of a real build
    # (the makefile's `current` target runs packages before ./build.sh), so
    # this is where the shared ledger starts fresh; build.sh's own phases
    # just append to it from here on.
    timings_path = Path(args.output).resolve().parent / "timings.jsonl"
    build_timings.reset_timings(timings_path)
    phase_start = time.monotonic()
    try:
        key_source = ensure_signing_key(brand["display_name"])
    except (PackageError, subprocess.CalledProcessError) as exc:
        print(f"signing key error: {exc}", file=sys.stderr)
        build_timings.append_phase(timings_path, "Build packages", time.monotonic() - phase_start)
        return 1
    if key_source == "generated":
        print(f"note: generated a new repository signing key in {PRIVATE_KEY.parent} (development key; "
              "keep this directory, images ship its public half). For releases, inject the real key with "
              "SYNOS_SIGNING_KEY or SYNOS_SIGNING_KEY_FILE.", file=sys.stderr)
    output = Path(args.output).resolve()
    work = output.parent / "packages-work"
    work.mkdir(parents=True, exist_ok=True)
    fingerprints = work / "fingerprints"
    fingerprints.mkdir(exist_ok=True)
    # The one shared copy of packages/_lib, staged here, before any package
    # (serial or pooled) starts, and never touched again this run: see
    # stage_shared_lib for why that ordering, not a lock, is what keeps a
    # running prebuild.sh from ever seeing it absent or partial.
    try:
        stage_shared_lib(work)
    except SkipPackage as exc:
        print(f"package error: {exc}", file=sys.stderr)
        build_timings.append_phase(timings_path, "Build packages", time.monotonic() - phase_start)
        return 1
    debs = []
    skipped = []
    not_for_base = []
    # bases.txt is metadata (no build side effect), so this filter runs
    # up front, sequentially, exactly as it did inline in the serial loop;
    # only sources that actually build are handed to workers below.
    build_sources = []
    for source in sources:
        bases_file = source / "bases.txt"
        if bases_file.is_file() and subs["BASE"] not in bases_file.read_text(encoding="utf-8").split():
            not_for_base.append(source.name)
        else:
            build_sources.append(source)

    def handle_outcome(outcome) -> None:
        if outcome[0] in ("reused", "built"):
            deb = outcome[1]
            debs.append(deb)
            print(f"  {outcome[0]} {deb.name}", file=sys.stderr)
        else:
            _, name, why = outcome
            skipped.append((name, why))
            print(f"  skipped {name}: {why[:160]}", file=sys.stderr)

    try:
        if jobs == 1:
            # Unchanged: one source at a time, in this same thread, in the
            # order sources was sorted in. A PackageError here propagates
            # immediately, exactly as it always has — no source after the
            # failing one is ever attempted.
            for source in build_sources:
                handle_outcome(_build_source(source, work, fingerprints, subs, args.rebuild))
        else:
            with cf.ThreadPoolExecutor(max_workers=jobs) as executor:
                futures = {source: executor.submit(_build_source, source, work, fingerprints, subs, args.rebuild)
                           for source in build_sources}
                first_error: PackageError | None = None
                # Collected, and printed, in the same sorted-by-name order as
                # --jobs 1 regardless of which finished first, so the
                # repository index and the printed log both stay independent
                # of real completion order.
                for source in build_sources:
                    future = futures[source]
                    if first_error is not None:
                        future.cancel()  # no-op once it has already started; it still runs to completion
                        continue
                    try:
                        handle_outcome(future.result())
                    except PackageError as exc:
                        first_error = exc
                # Exiting the `with` waits for whatever was already running
                # to finish (subprocess builds are not killed mid-build);
                # only then is the first failure, with its own message,
                # raised — matching "one package's own error fails the run".
            if first_error is not None:
                raise first_error
        brand_dir = ROOT / ".build" / "branding" / brand["id"]
        for deb in sorted(brand_dir.glob(f"{brand['id']}-branding_*_all.deb")):
            debs.append(deb)          # rendered by tools/render_brand.py (make brand runs first)
        # debs was assembled in build_sources' sorted order when serial, and
        # in that same order when concurrent (see the loop above) — sorted
        # again here so the repository's package order never depends on
        # which build happened to finish first, belt and suspenders.
        debs.sort(key=lambda d: d.name)
        signed = build_repository(debs, output, subs)
    except PackageError as exc:
        print(f"package error: {exc}", file=sys.stderr)
        build_timings.append_phase(timings_path, "Build packages", time.monotonic() - phase_start)
        return 1
    rel = output.relative_to(ROOT) if output.is_relative_to(ROOT) else output
    if not_for_base:
        print(f"not for {subs['BASE']}: {' '.join(not_for_base)}", file=sys.stderr)
    for name, why in skipped:
        print(f"skipped {name}: {why}", file=sys.stderr)
    if skipped and args.strict:
        print(f"package error: {len(skipped)} package(s) could not be built (--strict)", file=sys.stderr)
        build_timings.append_phase(timings_path, "Build packages", time.monotonic() - phase_start)
        return 1
    (output / "SKIPPED").write_text("".join(f"{n}\t{w}\n" for n, w in skipped), encoding="utf-8")
    build_timings.append_phase(timings_path, "Build packages", time.monotonic() - phase_start)
    print(f"built {len(debs)} package(s) into {rel} ({'signed' if signed else 'unsigned'}; {len(skipped)} skipped on this host)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
