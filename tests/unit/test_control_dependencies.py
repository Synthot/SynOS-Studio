"""Every package/*/control Depends and Pre-Depends name must resolve on every
suite its base supports — a repeat of the class of bug where
packages/synos-core-system/control named linux-generic-hwe-26.04 and
libfuse3-4 unconditionally, which only exist on the newest release, so a
noble (24.04) or jammy (22.04) install of that package could never complete.

Two checks:

* ControlDependencyNamingTests (offline, always runs): a lone Depends or
  Pre-Depends alternative whose bare package name embeds a two-digit.two-digit
  release number (22.04, 24.04, 26.04, 25.10, ...) with no "|" fallback is
  almost certainly a release pinned into the name by mistake — that is
  exactly the shape of the original bug. This does not need the network and
  cannot be skipped.

* ControlDependencyArchiveTests (online, skips cleanly offline): downloads the
  real Packages index for every (base, suite, component) bases/*/base.env
  declares and checks that at least one alternative in every Depends /
  Pre-Depends group is a real package name or a virtual name something else
  Provides there. A network failure (offline dev machine, mirror outage)
  skips the test instead of failing it. A handful of gaps that are real,
  already-reported product decisions (not naming bugs — see docs/ARCHITECTURE.md
  "packaging: dependencies that differ by release") are allow-listed by name
  so the test still catches new regressions without re-flagging known ones.
"""
from __future__ import annotations

import gzip
import importlib.util
import re
import socket
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_packages = _load("build_packages", "tools/build_packages.py")
render_manifest = _load("render_manifest", "tools/render_manifest.py")

LOCAL_PACKAGES = {p.name for p in (ROOT / "packages").iterdir() if p.is_dir() and not p.name.startswith("_")}
# Built out-of-band by tools/render_brand.py (make brand), not under packages/.
LOCAL_PACKAGES.add("synos-branding")

DEPENDENCY_FIELDS = ("Depends", "Pre-Depends")
VERSION_RE = re.compile(r"\d{2}\.\d{2}")
CONSTRAINT_RE = re.compile(r"\s*\(.*\)")


def _bases() -> dict[str, dict[str, str]]:
    bases = {}
    for env in sorted((ROOT / "bases").glob("*/base.env")):
        base_id = env.parent.name
        if base_id.startswith("_"):
            continue
        bases[base_id] = render_manifest.load_env(env)
    return bases


def _dependency_groups(base_id: str, base: dict[str, str], suite: str):
    """Yield (control_path, field, [alternative names]) for one rendered suite."""
    brand = render_manifest.resolve_brand("synos")
    manifest = {"version": "1.0.0", "suite": suite, "arch": "amd64"}
    subs = build_packages.substitutions(manifest, brand, base)
    for control in sorted((ROOT / "packages").glob("*/control")):
        bases_file = control.parent / "bases.txt"
        if bases_file.is_file() and base_id not in bases_file.read_text(encoding="utf-8").split():
            continue
        text = control.read_text(encoding="utf-8")
        rendered = build_packages.apply_base_fields(build_packages.render_text(text, subs), subs["BASE"])
        for line in rendered.splitlines():
            match = re.match(r"^(" + "|".join(DEPENDENCY_FIELDS) + r"):\s*(.*)$", line)
            if not match:
                continue
            for group in match.group(2).split(","):
                alts = [a.strip() for a in group.split("|") if a.strip()]
                if alts:
                    yield control, match.group(1), alts


class ControlDependencyNamingTests(unittest.TestCase):
    """Fast, offline: a release number baked into the only name in a group."""

    def test_no_lone_dependency_name_embeds_a_release_number(self) -> None:
        offenders = []
        for base_id, base in _bases().items():
            suite = base.get("DEFAULT_SUITE", "")
            for control, field, alts in _dependency_groups(base_id, base, suite):
                if len(alts) != 1:
                    continue  # a "name-a | name-b" alternatives list is the sanctioned fix, not the bug
                name = CONSTRAINT_RE.sub("", alts[0]).strip()
                if VERSION_RE.search(name):
                    offenders.append(f"{control.relative_to(ROOT)} ({base_id}) {field}: {name!r}")
        self.assertEqual(
            [], offenders,
            "a dependency name with no '|' fallback embeds a release number — "
            "it will stop resolving the moment that release stops being the newest one "
            "(see packages/synos-core-system/control's linux-generic-hwe-26.04 history): "
            + "; ".join(offenders),
        )


class ControlDependencyArchiveTests(unittest.TestCase):
    """Slow, online: every Depends/Pre-Depends group resolves against the real archive."""

    # Real, already-reported gaps (not naming bugs): the named package does not
    # exist at all pre-resolute/pre-26.04, and nothing else provides it. See the
    # "dependencies that differ by release" note in docs/ARCHITECTURE.md.
    KNOWN_GAPS = {
        ("ubuntu", "jammy", "libggml0"),
        ("ubuntu", "noble", "libggml0"),
        ("ubuntu", "jammy", "libggml0-backend-vulkan"),
        ("ubuntu", "noble", "libggml0-backend-vulkan"),
    }

    _index_cache: dict[tuple[str, str, str], set[str]] = {}

    @classmethod
    def _names_for(cls, base_url: str, pocket: str, component: str) -> set[str]:
        """Package + Provides names in one dists/<pocket>/<component> index, or
        an empty set when that pocket does not exist (e.g. a suite with no
        -backports yet) — matching apt, which just has nothing to offer from it."""
        key = (base_url, pocket, component)
        if key in cls._index_cache:
            return cls._index_cache[key]
        import tempfile
        names: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "Packages.gz"
            try:
                build_packages._fetch_index(base_url, pocket, component, "amd64", dest)
            except build_packages.SkipPackage:
                cls._index_cache[key] = names
                return names
            with gzip.open(dest, "rt", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Package:") or line.startswith("Provides:"):
                        for token in line.split(":", 1)[1].split(","):
                            token = token.strip().split(" ")[0]
                            if token:
                                names.add(token)
        cls._index_cache[key] = names
        return names

    @classmethod
    def _available_names(cls, base_url: str, security_url: str, suite: str, components: list[str]) -> set[str]:
        # Mirrors bases/*/sources.tmpl: SUITE, SUITE-updates and SUITE-backports
        # from the main mirror, SUITE-security from the security mirror — the
        # same pockets a real build's chroot has enabled.
        available: set[str] = set()
        for pocket, url in (
            (suite, base_url), (f"{suite}-updates", base_url), (f"{suite}-backports", base_url),
            (f"{suite}-security", security_url),
        ):
            for component in components:
                available |= cls._names_for(url, pocket, component)
        return available

    @classmethod
    def setUpClass(cls) -> None:
        # One cheap reachability probe before downloading anything: skip the
        # whole class if the network (or this specific mirror) is unreachable,
        # rather than reporting every package as unresolvable.
        try:
            socket.setdefaulttimeout(8)
            with urllib.request.urlopen("http://archive.ubuntu.com/ubuntu/dists/jammy/Release", timeout=8) as response:
                response.read(1)
        except OSError as exc:
            raise unittest.SkipTest(f"package archive is not reachable from this host: {exc}")

    def test_every_dependency_group_resolves_on_every_supported_suite(self) -> None:
        failures = []
        for base_id, base in _bases().items():
            base_url = base.get("APT_MIRROR", "").rstrip("/")
            security_url = base.get("SECURITY_MIRROR", "").rstrip("/") or base_url
            components = base.get("COMPONENTS", "main").split()
            for suite in base.get("SUPPORTED_SUITES", "").split():
                try:
                    available = self._available_names(base_url, security_url, suite, components)
                except OSError as exc:
                    self.skipTest(f"could not fetch the {base_id}/{suite} package index: {exc}")
                if not available:
                    self.skipTest(f"no package index came back for {base_id}/{suite} — treating as unreachable")
                for control, field, alts in _dependency_groups(base_id, base, suite):
                    names = [CONSTRAINT_RE.sub("", a).strip() for a in alts]
                    names = [n for n in names if n and not n.startswith("${") and n not in LOCAL_PACKAGES]
                    if not names:
                        continue  # every alternative is local (built from packages/) or a brand placeholder
                    if any((base_id, suite, n) in self.KNOWN_GAPS for n in names):
                        continue
                    if not any(n in available for n in names):
                        failures.append(
                            f"{control.relative_to(ROOT)} ({base_id}/{suite}) {field}: "
                            f"none of {names!r} exist there"
                        )
        self.assertEqual([], failures, "\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
