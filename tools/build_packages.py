#!/usr/bin/env python3
"""Build the SynOS packages under packages/ into a local APT repository.

    python3 tools/build_packages.py [--manifest manifest.yml] [--output .build/repo]
                                    [--only name ...] [--list]

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
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)
ManifestError = render_manifest.ManifestError

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
    bases/rust-toolchain.txt)."""
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
        try:
            _fetch(f"{base_url}/{fields['Filename']}", deb)
        except Exception as exc:
            raise SkipPackage(f"cannot download {fields['Filename']}: {exc}") from exc
    subprocess.run(["dpkg-deb", "-x", str(deb), str(pkg)], check=True)
    if not spec.get("suppress_scripts"):
        subprocess.run(["dpkg-deb", "-e", str(deb), str(pkg / "DEBIAN")], check=True)
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


def stage_recipe(source: Path, work: Path) -> Path:
    """Copy a recipe (inputs only) into the work tree, where prebuild.sh runs.
    Keeps prebuild outputs and root-owned container leftovers out of packages/."""
    src_root = work / "src"
    lib = src_root / "_lib"
    if not lib.is_dir() or any(f.stat().st_mtime > lib.stat().st_mtime for f in (ROOT / "packages" / "_lib").rglob("*")):
        rmtree_tolerant(lib)
        copy_tree(ROOT / "packages" / "_lib", lib)
        lib.touch()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(ROOT / "manifest.yml"))
    parser.add_argument("--output", default=str(ROOT / ".build" / "repo"))
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--strict", action="store_true", help="fail when any package is skipped (CI, container build)")
    parser.add_argument("--rebuild", action="store_true", help="ignore the recipe fingerprints and rebuild everything")
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
    try:
        key_source = ensure_signing_key(brand["display_name"])
    except (PackageError, subprocess.CalledProcessError) as exc:
        print(f"signing key error: {exc}", file=sys.stderr)
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
    debs = []
    skipped = []
    not_for_base = []
    try:
        for source in sources:
            bases_file = source / "bases.txt"
            if bases_file.is_file() and subs["BASE"] not in bases_file.read_text(encoding="utf-8").split():
                not_for_base.append(source.name)
                continue
            try:
                fingerprint = recipe_fingerprint(source, subs)
                stamp = fingerprints / source.name
                cached = sorted(work.glob(f"{source.name}_*.deb"))
                if not args.rebuild and cached and stamp.is_file() and stamp.read_text() == fingerprint:
                    debs.append(cached[-1])
                    print(f"  reused {cached[-1].name}", file=sys.stderr)
                    continue
                for old_deb in cached:
                    old_deb.unlink()
                debs.append(build_package(source, work, subs))
                stamp.write_text(fingerprint)
                print(f"  built {debs[-1].name}", file=sys.stderr)
            except SkipPackage as why:
                skipped.append((source.name, str(why)))
                print(f"  skipped {source.name}: {str(why)[:160]}", file=sys.stderr)
        brand_dir = ROOT / ".build" / "branding" / brand["id"]
        for deb in sorted(brand_dir.glob(f"{brand['id']}-branding_*_all.deb")):
            debs.append(deb)          # rendered by tools/render_brand.py (make brand runs first)
        signed = build_repository(debs, output, subs)
    except PackageError as exc:
        print(f"package error: {exc}", file=sys.stderr)
        return 1
    rel = output.relative_to(ROOT) if output.is_relative_to(ROOT) else output
    if not_for_base:
        print(f"not for {subs['BASE']}: {' '.join(not_for_base)}", file=sys.stderr)
    for name, why in skipped:
        print(f"skipped {name}: {why}", file=sys.stderr)
    if skipped and args.strict:
        print(f"package error: {len(skipped)} package(s) could not be built (--strict)", file=sys.stderr)
        return 1
    (output / "SKIPPED").write_text("".join(f"{n}\t{w}\n" for n, w in skipped), encoding="utf-8")
    print(f"built {len(debs)} package(s) into {rel} ({'signed' if signed else 'unsigned'}; {len(skipped)} skipped on this host)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
