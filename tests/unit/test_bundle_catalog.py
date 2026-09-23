"""The bundle catalog: bundle-catalog/, docs/BUNDLE.md "The bundle catalog",
and the bundle_catalog key tools/export_catalog.py exports for it.

A catalogued bundle is an ordinary bundle (schema/bundle.schema.json,
schema/manifest.schema.json, schema/profile.schema.json) kept in this
repository as a ready-made appliance, almost always carrying its own
profiles/<id>.yml (docs/BUNDLE.md). bundle-catalog/_template/ is not a
catalog entry (it is not listed in index.yml) and is excluded everywhere
below by name.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = ROOT / "bundle-catalog"
SYNOS = ROOT / "tools" / "synos"
TEMPLATE_FOLDER = "_template"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


render_manifest = load_module("render_manifest", "tools/render_manifest.py")
export_catalog = load_module("export_catalog", "tools/export_catalog.py")


def run_synos(*args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SYNOS), "--json", *args], capture_output=True, text=True, check=False, cwd=ROOT)
    return result.returncode, json.loads(result.stdout)


def index_entries() -> list[dict]:
    data = render_manifest.load_yaml(CATALOG_DIR / "index.yml")
    return data.get("bundle_catalog", [])


def catalog_folders() -> list[Path]:
    """Every bundle-catalog folder that is a real entry, the template excluded."""
    return sorted(p for p in CATALOG_DIR.iterdir() if p.is_dir() and p.name != TEMPLATE_FOLDER)


# --------------------------------------------------------------------------
# Most catalog entries ship their own profiles/<id>.yml (unlike the machine
# kinds this catalog used to just copy): render_manifest.resolve_profile and
# `render_manifest.py --check` only look in this checkout's profiles/, so the
# whole module installs every catalog folder's profile there for the
# duration of the run, the same thing `tools/synos bundle apply` would do,
# and removes exactly what it added afterwards.
_INSTALLED: list[Path] = []


def setUpModule() -> None:
    for folder in catalog_folders() + [CATALOG_DIR / TEMPLATE_FOLDER]:
        for profile_path in sorted(folder.glob("profiles/*.yml")):
            target = ROOT / "profiles" / profile_path.name
            if target.exists():
                raise AssertionError(f"{target} already exists in this checkout; a catalog entry must not shadow an engine profile")
            target.write_bytes(profile_path.read_bytes())
            _INSTALLED.append(target)


def tearDownModule() -> None:
    for path in _INSTALLED:
        path.unlink(missing_ok=True)
    _INSTALLED.clear()


class IndexAndFoldersTests(unittest.TestCase):
    def test_index_and_folders_agree(self) -> None:
        entries = index_entries()
        self.assertTrue(entries, "bundle-catalog/index.yml lists no entries")
        indexed_folders = {e["folder"] for e in entries}
        on_disk = {p.name for p in catalog_folders()}
        self.assertEqual(on_disk, indexed_folders, "an orphan folder or a missing folder")
        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate id in bundle-catalog/index.yml")
        self.assertNotIn(TEMPLATE_FOLDER, indexed_folders, "the template is not a catalog entry")

    SUMMARY_MAX_LENGTH = 280
    NO_FIRST_BOOT_ENTRIES = {"yocto-builder", "yocto-builder-docs", "ai-workstation"}

    def test_every_entry_has_the_required_index_fields(self) -> None:
        for entry in index_entries():
            with self.subTest(entry=entry["id"]):
                for field in ("id", "name", "summary", "first_boot", "tags", "folder", "services", "ports"):
                    self.assertIn(field, entry)
                self.assertRegex(entry["id"], r"^[a-z0-9][a-z0-9-]*$")
                self.assertTrue(entry["tags"], "no tags")
                for tag in entry["tags"]:
                    self.assertEqual(tag, tag.lower(), f"tag {tag!r} is not lowercase")
                for port in entry["ports"]:
                    self.assertIsInstance(port, int, f"{entry['id']}: port {port!r} must be a bare number")
                self.assertIsInstance(entry.get("verified", False), bool)

    def test_summary_is_one_short_sentence_with_no_command_and_no_first_boot_clause(self) -> None:
        """A card is not a manual page: the long first-boot prose belongs in
        first_boot, not summary (owner feedback after building the page)."""
        for entry in index_entries():
            with self.subTest(entry=entry["id"]):
                summary = entry["summary"]
                self.assertLessEqual(len(summary), self.SUMMARY_MAX_LENGTH,
                                      f"{entry['id']}: summary is {len(summary)} chars, over {self.SUMMARY_MAX_LENGTH}")
                self.assertNotIn("`", summary, f"{entry['id']}: summary contains a shell command")
                self.assertNotIn("$(", summary, f"{entry['id']}: summary contains a shell command")
                self.assertNotRegex(summary.lower(), r"first boot", f"{entry['id']}: summary keeps a 'first boot' clause")

    def test_first_boot_is_a_list_of_short_strings_or_empty_by_design(self) -> None:
        for entry in index_entries():
            with self.subTest(entry=entry["id"]):
                first_boot = entry["first_boot"]
                self.assertIsInstance(first_boot, list)
                if entry["id"] in self.NO_FIRST_BOOT_ENTRIES:
                    self.assertEqual([], first_boot, f"{entry['id']} is expected to have no meaningful first step")
                else:
                    self.assertTrue(first_boot, f"{entry['id']}: empty first_boot must be a deliberate choice, not an oversight")
                for step in first_boot:
                    self.assertIsInstance(step, str)
                    self.assertLess(len(step), 400, f"{entry['id']}: a first_boot step reads like more than a line or two")

    def test_nothing_from_the_old_combined_summaries_was_quietly_dropped(self) -> None:
        """Everything that used to live in the long summary (before it was
        split) must still be reachable: an appliance with something to say
        about first boot still says it, just in first_boot now."""
        must_mention = {
            "container-registry": ["htpasswd"],
            "git-server": ["2222", "3000"],
            "media-server": ["8096"],
            "monitoring-server": ["admin/admin", "3000"],
            "database-server": ["superuser-password", "5432"],
            "kubernetes-server": ["kubeadm init", "cgroup"],
        }
        by_id = {e["id"]: e for e in index_entries()}
        for entry_id, needles in must_mention.items():
            combined = by_id[entry_id]["summary"] + " ".join(by_id[entry_id]["first_boot"])
            for needle in needles:
                with self.subTest(entry=entry_id, needle=needle):
                    self.assertIn(needle, combined)

    def test_appliances_are_not_plain_copies_of_a_machine_kind(self) -> None:
        """The catalog was rewritten around appliances: every entry either ships
        its own profile (an appliance) or is one of the two genuinely
        preconfigured machine kinds (the Yocto builders, the AI workstation)."""
        machine_kind_only = {"yocto-builder", "ai-workstation"}
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            with self.subTest(entry=entry["id"]):
                ships_profile = any(folder.glob("profiles/*.yml"))
                self.assertTrue(ships_profile or entry["id"] in machine_kind_only,
                                 f"{entry['id']} neither ships a profile nor is a known preconfigured machine kind")


class CatalogedBundleValidationTests(unittest.TestCase):
    """Every catalogued bundle validates exactly like a person's downloaded bundle."""

    def test_every_entry_validates_as_a_bundle(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            with self.subTest(entry=entry["id"]):
                code, payload = run_synos("bundle", "validate", str(folder))
                self.assertEqual(0, code, payload)
                self.assertEqual([], payload["report"]["errors"])

    def test_the_template_folder_also_validates(self) -> None:
        code, payload = run_synos("bundle", "validate", str(CATALOG_DIR / TEMPLATE_FOLDER))
        self.assertEqual(0, code, payload)
        self.assertEqual([], payload["report"]["errors"])

    def test_every_manifest_renders_on_its_declared_base_and_suite(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest_path = folder / bundle["manifest"]
            with self.subTest(entry=entry["id"]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / "render_manifest.py"), "--manifest", str(manifest_path), "--check"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_every_bundle_json_matches_its_schema(self) -> None:
        schema = json.loads((ROOT / "schema" / "bundle.schema.json").read_text(encoding="utf-8"))
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            descriptor = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            with self.subTest(entry=entry["id"]):
                jsonschema.Draft202012Validator(schema).validate(descriptor)

    def test_every_shipped_profile_matches_its_schema_and_resolves(self) -> None:
        """Every extends and every package group a catalogued bundle's profile chain
        names exists, and it resolves without error (profiles ship either in the
        bundle or, like the Yocto and AI workstation machine kinds, with the engine)."""
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            for profile_path in sorted(folder.glob("profiles/*.yml")):
                with self.subTest(entry=entry["id"], profile=profile_path.name):
                    data = render_manifest.load_yaml(profile_path)
                    render_manifest.validate_schema(data, "profile.schema.json")
            with self.subTest(entry=entry["id"], profile=manifest["profile"]):
                profile, chain = render_manifest.resolve_profile(manifest["profile"])
                self.assertGreaterEqual(len(chain), 1)


class PackageResolutionTests(unittest.TestCase):
    """Every package group and every repository a catalogued bundle's profile
    chain uses resolves to a non-empty concrete package list on both bases."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.bundles = render_manifest.load_bundles()
        cls.pkg_maps = {
            base_dir.name: render_manifest.load_package_map(base_dir / "packages.map")
            for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_"))
        }

    def test_bundle_groups_resolve_on_both_bases(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            profile, _ = render_manifest.resolve_profile(manifest["profile"])
            bundle_ids = list((profile.get("software") or {}).get("bundles", []))
            for bundle_id in bundle_ids:
                self.assertIn(bundle_id, self.bundles, f"{entry['id']}: unknown bundle {bundle_id!r}")
                for base_id, pkg_map in self.pkg_maps.items():
                    with self.subTest(entry=entry["id"], bundle=bundle_id, base=base_id):
                        concrete, _ = render_manifest.resolve_packages(self.bundles[bundle_id], pkg_map, "amd64", ["en"], base_id)
                        self.assertTrue(concrete, f"{bundle_id} resolves to nothing on {base_id}")

    def test_repositories_name_a_key_that_exists_and_is_a_public_key(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            profile, _ = render_manifest.resolve_profile(manifest["profile"])
            repos = (profile.get("software") or {}).get("repositories", [])
            for repo in repos:
                with self.subTest(entry=entry["id"], repository=repo["name"]):
                    key_path = ROOT / repo["key"]
                    self.assertTrue(key_path.is_file(), f"{repo['key']} does not exist")
                    self.assertTrue(key_path.read_text(encoding="utf-8").startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"))

    def test_yocto_build_groups_are_mapped_on_both_bases(self) -> None:
        for group in ("yocto-build", "yocto-build-docs"):
            self.assertIn(group, self.bundles)
        for base_id, pkg_map in self.pkg_maps.items():
            with self.subTest(base=base_id):
                concrete, unmapped = render_manifest.resolve_packages(self.bundles["yocto-build"], pkg_map, "amd64", ["en"], base_id)
                self.assertEqual([], unmapped)
                self.assertGreaterEqual(len(concrete), 30)
                self.assertIn("build-essential", concrete)
                self.assertIn("lz4", concrete)
                self.assertNotIn("liblz4-tool", concrete)
                docs_concrete, docs_unmapped = render_manifest.resolve_packages(self.bundles["yocto-build-docs"], pkg_map, "amd64", ["en"], base_id)
                self.assertEqual([], docs_unmapped)
                self.assertIn("texlive-latex-extra", docs_concrete)

    def test_yocto_manuals_required_set_is_a_subset_of_both_catalogued_machines(self) -> None:
        """Pins the Yocto Project reference manual's own "Ubuntu and Debian"
        apt-get line (https://docs.yoctoproject.org/ref-manual/system-requirements.html):
        a future edit that drops one of these from yocto-host-tools must fail
        here, on both the lean and the full bundle-catalog entries."""
        required = {
            "gawk", "wget", "git", "diffstat", "unzip", "texinfo", "build-essential", "chrpath", "socat",
            "cpio", "python3", "python3-pip", "python3-pexpect", "xz-utils", "debianutils", "iputils-ping",
            "python3-git", "python3-jinja2", "python3-subunit", "zstd", "locales", "libacl1", "libcrypt-dev", "gcc",
        }
        for entry_id, profile_id in (("yocto-builder", "yocto-builder"), ("yocto-builder-docs", "yocto-builder-docs")):
            with self.subTest(entry=entry_id):
                profile, _ = render_manifest.resolve_profile(profile_id)
                bundle_ids = profile["software"]["bundles"]
                abstract: list = []
                for bundle_id in bundle_ids:
                    abstract.extend(self.bundles[bundle_id])
                for base_id, pkg_map in self.pkg_maps.items():
                    with self.subTest(base=base_id):
                        concrete, _ = render_manifest.resolve_packages(abstract, pkg_map, "amd64", ["en"], base_id)
                        missing = required - set(concrete)
                        self.assertEqual(set(), missing, f"{entry_id}/{base_id} is missing required Yocto packages: {missing}")


class AppliancePolicyTests(unittest.TestCase):
    """Rules the owner set for every appliance: no GPU service, pinned image
    tags, no secret standing in for a real one."""

    SECRET_LOOKING = ("BEGIN RSA PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "BEGIN PGP PRIVATE KEY", "AKIA", "-----BEGIN PRIVATE KEY-----")

    def test_appliance_services_in_the_catalog_are_gpu_free_and_pinned(self) -> None:
        catalog = render_manifest.load_yaml(ROOT / "profiles" / "catalog.yml")
        appliance_ids = {
            "nginx", "apache", "caddy", "registry", "gitea", "jellyfin", "prometheus", "grafana", "postgres",
            "homeassistant", "pihole", "nextcloud", "vaultwarden", "portainer", "uptime-kuma", "syncthing",
            "minio", "n8n", "wg-easy",
        }
        appliance_ids = {"nginx", "apache", "caddy", "registry", "gitea", "jellyfin", "prometheus", "grafana", "postgres",
                         "kafka", "jobmanager", "taskmanager", "zookeeper", "keycloak", "openbao", "envoy", "pdns", "ray-head", "gitlab"}
        seen = set()
        for service in catalog["services"]:
            if service["id"] not in appliance_ids:
                continue
            seen.add(service["id"])
            with self.subTest(service=service["id"]):
                self.assertFalse(service.get("gpu", False), f"{service['id']} must not need a GPU")
                for image in service["images"].values():
                    self.assertIn(":", image, f"{service['id']}: {image} is not pinned to a tag")
                    tag = image.rsplit(":", 1)[1]
                    self.assertNotEqual("latest", tag, f"{service['id']}: {image} is pinned to latest")
        self.assertEqual(appliance_ids, seen, "an expected appliance service is missing from profiles/catalog.yml")

    def test_no_shipped_file_looks_like_a_real_secret(self) -> None:
        for folder in catalog_folders():
            for path in folder.rglob("*"):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for needle in self.SECRET_LOOKING:
                    self.assertNotIn(needle, text, f"{path} looks like it carries a real key")

    def test_no_appliance_ships_a_file_carrying_a_usable_credential(self) -> None:
        """Fail closed, not a placeholder credential: the database ships no
        password file at all (the service refuses to start until the person
        creates one), and the registry's htpasswd line is not a real bcrypt
        hash, so it cannot authenticate any password."""
        database_profile, _ = render_manifest.resolve_profile("database-server")
        files = database_profile.get("software", {}).get("files", [])
        self.assertFalse([f for f in files if "password" in f["path"].lower()],
                          "database-server must not ship a password file")
        registry_profile, _ = render_manifest.resolve_profile("container-registry")
        htpasswd = next(f for f in registry_profile["software"]["files"] if f["path"].endswith("htpasswd"))
        # a real htpasswd -B line looks like "user:$2y$05$..."; ship anything else
        for line in htpasswd["content"].splitlines():
            if not line.strip():
                continue
            with self.subTest(line=line):
                self.assertNotRegex(line, r":\$2[aby]\$\d\d\$", "this line is a real bcrypt hash and could authenticate someone")
        entry = next(e for e in index_entries() if e["id"] == "database-server")
        self.assertIn("fails to start", " ".join(entry["first_boot"]).lower())
        registry_entry = next(e for e in index_entries() if e["id"] == "container-registry")
        self.assertIn("htpasswd", " ".join(registry_entry["first_boot"]))

    def test_first_arrival_admin_claiming_ports_are_closed_by_default(self) -> None:
        """Gitea's web UI, Grafana, and Jellyfin's setup wizard each let whoever
        connects first claim the machine (register the first account, or sign
        in with a well-known default); those ports stay closed until the
        person has finished setup locally or over a tunnel."""
        closed_by_default = {
            "git-server": 3000,        # Gitea web UI: registers the first account as admin
            "monitoring-server": 3000,  # Grafana: admin/admin until changed
            "media-server": 8096,      # Jellyfin: its own setup wizard creates the admin account
            "home-automation": 8123,   # Home Assistant: onboarding wizard creates the admin account
            "personal-cloud": 80,      # Nextcloud: setup wizard creates the admin account
            "container-management": 9443,  # Portainer: setup token gates the admin account
            "status-monitoring": 3001,  # Uptime Kuma: setup screen creates the one admin account
            "automation-flows": 5678,  # n8n: setup screen creates the owner account
            "vpn-server": 51821,       # wg-easy: setup wizard creates the admin account
        }
        for entry_id, risky_port in closed_by_default.items():
            with self.subTest(entry=entry_id):
                entry = next(e for e in index_entries() if e["id"] == entry_id)
                self.assertNotIn(risky_port, entry["ports"], f"{entry_id}: port {risky_port} must stay closed by default")
                self.assertIn(22, entry["ports"], f"{entry_id}: SSH access must remain available")
                self.assertRegex(" ".join(entry["first_boot"]).lower(), r"tunnel|locally",
                                  f"{entry_id}: first_boot must say how to reach it safely first")

    def test_gitea_ssh_port_stays_open_because_nothing_is_reachable_before_an_account_exists(self) -> None:
        profile, _ = render_manifest.resolve_profile("git-server")
        open_ports = profile["security"]["open_ports"]
        self.assertIn("2222/tcp", open_ports)
        self.assertNotIn("3000/tcp", open_ports)

    def test_prometheus_stays_open_since_it_has_no_login_to_claim(self) -> None:
        """Only Grafana (an account-claiming login) is closed by default here;
        Prometheus has no authentication to race for, so leaving it reachable
        is a data-exposure question the owner did not ask to change."""
        profile, _ = render_manifest.resolve_profile("monitoring-server")
        self.assertIn("9090/tcp", profile["security"]["open_ports"])
        self.assertNotIn("3000/tcp", profile["security"]["open_ports"])

    def test_kubernetes_opens_only_the_api_server_and_kubelet_by_default(self) -> None:
        profile, _ = render_manifest.resolve_profile("kubernetes-server")
        self.assertEqual({"22/tcp", "6443/tcp", "10250/tcp"}, set(profile["security"]["open_ports"]))
        raw = render_manifest.load_yaml(CATALOG_DIR / "kubernetes-server" / "profiles" / "kubernetes-server.yml")
        self.assertRegex(raw["description"], r"2379-2380|etcd")
        self.assertRegex(raw["description"], r"30000-32767|NodePort")

    def test_kubernetes_repository_documents_the_signing_key_expiry(self) -> None:
        text = (CATALOG_DIR / "kubernetes-server" / "profiles" / "kubernetes-server.yml").read_text(encoding="utf-8")
        self.assertIn("2026-12-29", text)

    def test_engine_min_reflects_whether_a_bundle_uses_software_files(self) -> None:
        """docs/BUNDLE.md: engine.min tracks the oldest engine that understands
        the bundle. software.files is new in 0.2.0, so any catalogued bundle
        using it must require at least that; the rest stay honest at 0.1.0."""
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            profile, _ = render_manifest.resolve_profile(manifest["profile"])
            uses_files = bool((profile.get("software") or {}).get("files"))
            engine_min = bundle["engine"]["min"]
            with self.subTest(entry=entry["id"]):
                if uses_files:
                    self.assertGreaterEqual(tuple(map(int, engine_min.split("."))), (0, 2, 0),
                                             f"{entry['id']} uses software.files but engine.min is {engine_min}")

    def test_every_appliance_profile_re_opens_ssh_when_it_replaces_open_ports(self) -> None:
        """security.open_ports fully replaces the parent's list (it is not a
        merge key), so an appliance profile that sets it must re-list 22/tcp
        or lose SSH access that `server` granted."""
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            for profile_path in folder.glob("profiles/*.yml"):
                data = render_manifest.load_yaml(profile_path)
                if data.get("extends") != "server":
                    continue
                open_ports = (data.get("security") or {}).get("open_ports")
                if open_ports is not None:
                    with self.subTest(entry=entry["id"]):
                        self.assertIn("22/tcp", open_ports, f"{profile_path.name} drops SSH by replacing open_ports without 22/tcp")


class DesktopWorkstationBundleTests(unittest.TestCase):
    """The nine preconfigured workstation entries: each keeps a normal desktop
    (extends workstation/developer) unless it is deliberately a kiosk, and
    every capability or group that grants power is named where a person will
    actually see it (docs/BUNDLE.md: "nothing that weakens a machine
    silently")."""

    def _profile(self, entry_id: str) -> dict:
        profile, _ = render_manifest.resolve_profile(entry_id)
        return profile

    def _raw(self, folder: str) -> dict:
        return render_manifest.load_yaml(CATALOG_DIR / folder / "profiles" / f"{folder}.yml")

    def _manifest(self, folder: str) -> dict:
        return render_manifest.load_yaml(CATALOG_DIR / folder / "manifests" / f"{folder}.yml")

    def _resolved_bundle(self, bundle_id: str, base_id: str) -> list[str]:
        """The concrete package list bundle_id resolves to on base_id — the
        same resolution the real build does, so a test asserting a package
        is shipped checks the base the entry actually targets, not just
        that the abstract bundle name exists."""
        bundles = render_manifest.load_bundles()
        pkg_map = render_manifest.load_package_map(ROOT / "bases" / base_id / "packages.map")
        concrete, _ = render_manifest.resolve_packages(bundles[bundle_id], pkg_map, "amd64", ["en"], base_id)
        return concrete

    def test_workstations_keep_a_normal_desktop_unless_deliberately_a_kiosk(self) -> None:
        expected_parent = {
            "audio-workstation": "workstation",
            "video-editing-workstation": "workstation",
            "photography-workstation": "workstation",
            "cad-3d-printing-workstation": "workstation",
            "data-science-workstation": "developer",
            "security-research-workstation": "workstation",
            "classroom-workstation": "workstation",
            "digital-signage": "kiosk",
            "retro-gaming-console": "kiosk",
        }
        for entry_id, parent in expected_parent.items():
            with self.subTest(entry=entry_id):
                self.assertEqual(parent, self._raw(entry_id).get("extends"))

    def test_each_entrys_manifest_pins_the_base_and_suite_its_software_needs(self) -> None:
        """Each catalogued bundle pins one base and one suite in its own
        manifest (docs/BUNDLE.md), so a package only has to exist on the
        suite named here. Every entry stays on this engine's usual
        ubuntu/noble except cad-3d-printing-workstation, which moves to
        debian/trixie for a modern FreeCAD (see its profile description)."""
        expected = {
            "audio-workstation": ("ubuntu", "noble"),
            "video-editing-workstation": ("ubuntu", "noble"),
            "photography-workstation": ("ubuntu", "noble"),
            "cad-3d-printing-workstation": ("debian", "trixie"),
            "data-science-workstation": ("ubuntu", "noble"),
            "security-research-workstation": ("ubuntu", "noble"),
            "digital-signage": ("ubuntu", "noble"),
            "retro-gaming-console": ("ubuntu", "noble"),
            "classroom-workstation": ("ubuntu", "noble"),
        }
        for folder, (base, suite) in expected.items():
            with self.subTest(entry=folder):
                manifest = self._manifest(folder)
                self.assertEqual(base, manifest["base"])
                self.assertEqual(suite, manifest["suite"])

    def test_audio_workstation_grants_realtime_scheduling_to_the_audio_group(self) -> None:
        profile = self._profile("audio-workstation")
        files = profile["software"]["files"]
        limits = next(f for f in files if f["path"] == "/etc/security/limits.d/99-audio-realtime.conf")
        self.assertIn("@audio", limits["content"])
        self.assertIn("rtprio", limits["content"])
        self.assertIn("audio-production", profile["software"]["bundles"])
        entry = next(e for e in index_entries() if e["id"] == "audio-workstation")
        self.assertRegex(" ".join(entry["first_boot"]).lower(), r"audio group")

    def test_audio_workstation_uses_pipewire_jack_not_classic_jackd2(self) -> None:
        """Audio pins noble specifically because PipeWire's JACK
        compatibility layer (pipewire-jack, qpwgraph) is there and on
        resolute/trixie but missing from jammy; ardour still needs no
        separate jackd2/qjackctl daemon."""
        concrete = self._resolved_bundle("audio-production", "ubuntu")
        self.assertIn("pipewire-jack", concrete)
        self.assertIn("qpwgraph", concrete)
        self.assertIn("ardour", concrete)
        self.assertNotIn("jackd2", concrete)
        self.assertNotIn("qjackctl", concrete)
        # also resolves on debian (trixie carries pipewire-jack/qpwgraph too)
        debian_concrete = self._resolved_bundle("audio-production", "debian")
        self.assertIn("pipewire-jack", debian_concrete)
        self.assertIn("qpwgraph", debian_concrete)

    def test_cad_3d_printing_grants_dialout_access_by_udev_rule(self) -> None:
        profile = self._profile("cad-3d-printing-workstation")
        files = profile["software"]["files"]
        rules = next(f for f in files if f["path"] == "/etc/udev/rules.d/70-cad-3dprinting-boards.rules")
        self.assertIn('GROUP="dialout"', rules["content"])
        self.assertIn("SUBSYSTEM==\"tty\"", rules["content"])
        entry = next(e for e in index_entries() if e["id"] == "cad-3d-printing-workstation")
        self.assertRegex(" ".join(entry["first_boot"]).lower(), r"dialout group")

    def test_cad_3d_printing_ships_freecad_cura_and_openscad_on_debian_trixie(self) -> None:
        """FreeCAD and Cura together are missing from every Ubuntu suite
        this engine defaults to (noble, resolute) and jammy's FreeCAD is a
        very old 0.19.2, so this entry pins debian/trixie instead, which
        carries a modern FreeCAD 1.0, Cura and OpenSCAD together — checked
        with rmadison against trixie specifically, documented in the
        profile description rather than silently substituted."""
        raw = self._raw("cad-3d-printing-workstation")
        self.assertIn("FreeCAD", raw["description"])
        self.assertIn("Cura", raw["description"])
        self.assertIn("trixie", raw["description"])
        concrete = self._resolved_bundle("cad-3d-printing", "debian")
        self.assertIn("freecad", concrete)
        self.assertIn("cura", concrete)
        self.assertIn("openscad", concrete)
        # the ubuntu side of the same abstract bundle stays on the
        # OpenSCAD/PrusaSlicer set available on every ubuntu suite
        ubuntu_concrete = self._resolved_bundle("cad-3d-printing", "ubuntu")
        self.assertIn("openscad", ubuntu_concrete)
        self.assertIn("prusa-slicer", ubuntu_concrete)

    def test_security_research_grants_packet_capture_to_a_pcap_group(self) -> None:
        profile = self._profile("security-research-workstation")
        files = profile["software"]["files"]
        sysusers = next(f for f in files if f["path"] == "/etc/sysusers.d/pcap.conf")
        self.assertIn("g pcap", sysusers["content"])
        unit_paths = [f["path"] for f in files if f["path"].endswith("pcap-permissions.service")]
        self.assertEqual(2, len(unit_paths), "the unit must exist both in /etc/systemd/system and enabled under multi-user.target.wants")
        for f in files:
            if f["path"].endswith("pcap-permissions.service"):
                self.assertIn("cap_net_raw,cap_net_admin", f["content"])
                self.assertIn("dumpcap", f["content"])
        entry = next(e for e in index_entries() if e["id"] == "security-research-workstation")
        self.assertRegex(" ".join(entry["first_boot"]).lower(), r"pcap group")

    def test_security_research_summary_states_lawful_use(self) -> None:
        entry = next(e for e in index_entries() if e["id"] == "security-research-workstation")
        self.assertRegex(entry["summary"].lower(), r"lawful|authorised|authorized")
        raw = self._raw("security-research-workstation")
        self.assertRegex(raw["description"].lower(), r"lawful|authorised|authorized")

    def test_digital_signage_ships_the_url_in_one_file(self) -> None:
        profile = self._profile("digital-signage")
        files = profile["software"]["files"]
        policies = next(f for f in files if f["path"] == "/etc/firefox/policies/policies.json")
        payload = json.loads(policies["content"])
        self.assertIn("URL", payload["policies"]["Homepage"])
        self.assertEqual("browser", profile["policy"]["kiosk_app"])
        entry = next(e for e in index_entries() if e["id"] == "digital-signage")
        self.assertIn("policies.json", " ".join(entry["first_boot"]))

    def test_retro_gaming_console_boots_into_retroarch_with_controller_support(self) -> None:
        profile = self._profile("retro-gaming-console")
        self.assertEqual("retroarch", profile["policy"]["kiosk_app"])
        files = profile["software"]["files"]
        cfg = next(f for f in files if f["path"] == "/etc/retroarch.cfg")
        self.assertIn("input_joypad_driver", cfg["content"])
        entry = next(e for e in index_entries() if e["id"] == "retro-gaming-console")
        self.assertIn("controller-support", entry["tags"])

    def test_retro_gaming_console_ships_six_cores_on_noble_five_on_trixie(self) -> None:
        """Nestopia (NES) needs noble or newer, missing from jammy; Beetle
        PCE Fast (PC Engine) is on every Ubuntu suite this engine supports
        but missing from trixie — so the Ubuntu side of this bundle (what
        this entry actually ships) carries six cores and the Debian side
        five, checked against noble and trixie respectively."""
        ubuntu_concrete = self._resolved_bundle("retro-gaming", "ubuntu")
        for core in ("libretro-snes9x", "libretro-mgba", "libretro-gambatte", "libretro-desmume",
                     "libretro-nestopia", "libretro-beetle-pce-fast"):
            with self.subTest(core=core):
                self.assertIn(core, ubuntu_concrete)
        debian_concrete = self._resolved_bundle("retro-gaming", "debian")
        for core in ("libretro-snes9x", "libretro-mgba", "libretro-gambatte", "libretro-desmume", "libretro-nestopia"):
            with self.subTest(core=core):
                self.assertIn(core, debian_concrete)
        self.assertNotIn("libretro-beetle-pce-fast", debian_concrete)

    def test_video_editing_ships_mesa_and_vdpau_drivers_on_noble(self) -> None:
        """mesa-va-drivers and vdpau-driver-all are missing from resolute
        but present on noble (this entry's target suite), so they are
        shipped alongside va-driver-all rather than dropped."""
        concrete = self._resolved_bundle("video-editing", "ubuntu")
        for pkg in ("va-driver-all", "vainfo", "mesa-va-drivers", "vdpau-driver-all"):
            with self.subTest(package=pkg):
                self.assertIn(pkg, concrete)

    def test_photography_ships_displaycal_on_noble(self) -> None:
        """displaycal is missing from jammy but present on noble."""
        concrete = self._resolved_bundle("photography", "ubuntu")
        self.assertIn("displaycal", concrete)

    def test_classroom_workstation_locks_down_and_says_so(self) -> None:
        profile = self._profile("classroom-workstation")
        self.assertTrue(profile["security"]["usbguard"])
        dconf = profile["policy"]["dconf"]
        self.assertEqual("true", dconf["org/gnome/desktop/lockdown/disable-command-line"])
        self.assertEqual("true", dconf["org/gnome/desktop/lockdown/user-administration-disabled"])
        files = profile["software"]["files"]
        polkit_rule = next(f for f in files if f["path"] == "/etc/polkit-1/rules.d/50-classroom-lockdown.rules")
        self.assertIn("polkit.addRule", polkit_rule["content"])
        entry = next(e for e in index_entries() if e["id"] == "classroom-workstation")
        self.assertRegex(entry["summary"].lower(), r"usbguard|no command line|locked-down")

    def test_classroom_workstation_keeps_a_normal_desktop_not_a_kiosk(self) -> None:
        """Instruction: a shared-desk machine is a workstation, not a
        single-app kiosk, so it must not carry a kiosk_app."""
        profile = self._profile("classroom-workstation")
        self.assertNotIn("kiosk_app", profile.get("policy", {}))

    def test_dconf_policy_keys_use_slash_paths_not_dotted_gsettings_names(self) -> None:
        """ansible/collections/.../desktop_policy/tasks/main.yml splits a
        dconf key on its last '/' to get the [group] and the entry name; a
        dotted GSettings-style key (no '/') would index past the end of the
        one-element list that rsplit returns and crash the role at build
        time for any entry whose value is not the literal sentinel
        "locked". Every non-"locked" policy.dconf key added here must
        therefore already be in path/key form."""
        for folder_name in ("classroom-workstation",):
            raw = self._raw(folder_name)
            dconf = (raw.get("policy") or {}).get("dconf") or {}
            for key, value in dconf.items():
                with self.subTest(entry=folder_name, key=key):
                    if value == "locked":
                        continue
                    self.assertIn("/", key, f"{key!r} must be slash-separated (path/key), not a dotted GSettings name")

    def test_every_new_workstation_bundle_resolves_to_a_non_empty_package_list_on_both_bases(self) -> None:
        bundles = render_manifest.load_bundles()
        pkg_maps = {
            base_dir.name: render_manifest.load_package_map(base_dir / "packages.map")
            for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_"))
        }
        new_bundles = ("audio-production", "video-editing", "photography", "cad-3d-printing", "data-science", "security-research", "retro-gaming")
        for bundle_id in new_bundles:
            self.assertIn(bundle_id, bundles)
            for base_id, pkg_map in pkg_maps.items():
                with self.subTest(bundle=bundle_id, base=base_id):
                    concrete, unmapped = render_manifest.resolve_packages(bundles[bundle_id], pkg_map, "amd64", ["en"], base_id)
                    self.assertTrue(concrete, f"{bundle_id} resolves to nothing on {base_id}")
                    self.assertEqual([], unmapped, f"{bundle_id} has an unmapped abstract name on {base_id}")

    def test_new_workstation_files_stay_within_the_allowed_directories(self) -> None:
        """render_manifest.validate_profile_files re-checks path, mode and
        size at render time; call it directly here too so a future edit
        that breaks one of these files fails fast in this file's own test,
        not only in the generic per-entry loop above."""
        for entry_id in (
            "audio-workstation", "video-editing-workstation", "photography-workstation",
            "cad-3d-printing-workstation", "data-science-workstation", "security-research-workstation",
            "digital-signage", "retro-gaming-console", "classroom-workstation",
        ):
            with self.subTest(entry=entry_id):
                profile = self._profile(entry_id)
                files = (profile.get("software") or {}).get("files", [])
                render_manifest.validate_profile_files(files)
class SelfHostingApplianceTests(unittest.TestCase):
    """Checks specific to the second round of appliances (home automation,
    ad blocking, personal cloud, password manager, container management,
    status monitoring, file sync, object storage, automation flows, VPN):
    the resolver collision Pi-hole needs solved, the UDP-only ports the
    schema had to learn, and the fail-closed shape each of these takes."""

    def test_ad_blocking_disables_the_systemd_resolved_stub_before_binding_port_53(self) -> None:
        profile, _ = render_manifest.resolve_profile("ad-blocking")
        files = profile["software"]["files"]
        override = next(f for f in files if f["path"] == "/etc/systemd/resolved.conf.d/pihole-no-stub.conf")
        self.assertIn("DNSStubListener=no", override["content"])
        open_ports = profile["security"]["open_ports"]
        self.assertIn("53/tcp", open_ports)
        self.assertIn("53/udp", open_ports)
        self.assertNotIn("80/tcp", open_ports, "Pi-hole's web UI must stay closed by default")
        entry = next(e for e in index_entries() if e["id"] == "ad-blocking")
        combined = entry["summary"] + " ".join(entry["first_boot"])
        self.assertIn("resolv.conf", combined)
        self.assertIn("DNSStubListener", combined)

    def test_profile_schema_accepts_a_udp_only_service_port(self) -> None:
        """software.services[].ports used to accept only bare "host:container"
        strings; WireGuard-style appliances need a UDP-only port too."""
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((ROOT / "schema" / "profile.schema.json").read_text(encoding="utf-8"))
        sample = {
            "id": "udp-port-check",
            "software": {"services": [{"name": "example", "image": "docker.io/library/example:1.0.0",
                                        "ports": ["51820:51820/udp", "80:80"]}]},
        }
        jsonschema.Draft202012Validator(schema).validate(sample)
        bad = {
            "id": "udp-port-check-bad",
            "software": {"services": [{"name": "example", "image": "docker.io/library/example:1.0.0",
                                        "ports": ["51820:51820/sctp"]}]},
        }
        with self.assertRaises(jsonschema.exceptions.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(bad)

    def test_vpn_server_publishes_wireguard_over_udp_and_only_that_capability(self) -> None:
        profile, _ = render_manifest.resolve_profile("vpn-server")
        service = next(s for s in profile["software"]["services"] if s["name"] == "wg-easy")
        self.assertIn("51820:51820/udp", service["ports"])
        self.assertEqual(["NET_ADMIN"], service.get("cap_add"))
        open_ports = profile["security"]["open_ports"]
        self.assertIn("51820/udp", open_ports)
        self.assertNotIn("51821/tcp", open_ports, "wg-easy's admin-claiming web UI must stay closed by default")
        # WG_HOST ships as a documentation-only placeholder address (RFC 5737),
        # never a real routable address.
        self.assertEqual("192.0.2.1", service["env"]["WG_HOST"])

    def test_password_manager_warns_about_tls_and_ships_no_open_signups(self) -> None:
        profile, _ = render_manifest.resolve_profile("password-manager")
        service = next(s for s in profile["software"]["services"] if s["name"] == "vaultwarden")
        self.assertEqual("false", service["env"]["SIGNUPS_ALLOWED"])
        self.assertNotIn("80/tcp", profile["security"]["open_ports"])
        entry = next(e for e in index_entries() if e["id"] == "password-manager")
        combined = (entry["summary"] + " ".join(entry["first_boot"])).lower()
        self.assertIn("tls", combined)

    def test_object_storage_fails_closed_like_the_database_appliance(self) -> None:
        """MinIO itself has no "refuse to start without a secret" mode (it
        falls back to minioadmin:minioadmin), so this profile forces that
        shape with the quadlet's own EnvironmentFile=, pointed at a file the
        profile deliberately never ships — the same "credential the
        service needs is read from a file this profile does not ship, so
        it fails to start" rule database-server uses, just via env_file
        instead of a mounted secret."""
        profile, _ = render_manifest.resolve_profile("object-storage")
        service = next(s for s in profile["software"]["services"] if s["name"] == "minio")
        self.assertEqual("/etc/minio/credentials", service.get("env_file"))
        env = service.get("env", {})
        self.assertNotIn("MINIO_ROOT_USER", env)
        self.assertNotIn("MINIO_ROOT_PASSWORD", env)
        files = profile.get("software", {}).get("files", [])
        self.assertFalse([f for f in files if f["path"] == "/etc/minio/credentials"],
                          "object-storage must not ship the credentials file its own env_file points at")
        self.assertNotIn("9000/tcp", profile["security"]["open_ports"])
        self.assertNotIn("9001/tcp", profile["security"]["open_ports"])
        entry = next(e for e in index_entries() if e["id"] == "object-storage")
        self.assertIn("fails to start", " ".join(entry["first_boot"]).lower())

    def test_cap_add_is_a_closed_list_not_free_form(self) -> None:
        """A free-form cap_add would let a contributed bundle ask for
        SYS_ADMIN or ALL unnoticed; only capabilities an appliance in this
        catalog actually needs are enumerated."""
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((ROOT / "schema" / "profile.schema.json").read_text(encoding="utf-8"))
        good = {"id": "cap-add-check", "software": {"services": [
            {"name": "example", "image": "docker.io/library/example:1.0.0", "cap_add": ["NET_ADMIN"]}]}}
        jsonschema.Draft202012Validator(schema).validate(good)
        for refused in ("SYS_ADMIN", "ALL", "SYS_PTRACE"):
            bad = {"id": "cap-add-check-bad", "software": {"services": [
                {"name": "example", "image": "docker.io/library/example:1.0.0", "cap_add": [refused]}]}}
            with self.subTest(cap=refused):
                with self.assertRaises(jsonschema.exceptions.ValidationError):
                    jsonschema.Draft202012Validator(schema).validate(bad)

    def test_file_sync_opens_the_sync_protocol_but_not_the_unauthenticated_gui(self) -> None:
        profile, _ = render_manifest.resolve_profile("file-sync")
        open_ports = profile["security"]["open_ports"]
        self.assertIn("22000/tcp", open_ports)
        self.assertIn("22000/udp", open_ports)
        self.assertIn("21027/udp", open_ports)
        self.assertNotIn("8384/tcp", open_ports, "Syncthing's unauthenticated GUI must stay closed by default")


class YoctoProfileTests(unittest.TestCase):
    def test_yocto_builder_extends_developer_and_mentions_locale_and_disk(self) -> None:
        data = render_manifest.load_yaml(ROOT / "profiles" / "yocto-builder.yml")
        self.assertEqual("developer", data.get("extends"))
        description = data.get("description", "")
        self.assertIn("UTF-8", description)
        self.assertRegex(description.lower(), r"disk|gb|space")
        profile, chain = render_manifest.resolve_profile("yocto-builder")
        self.assertEqual(["minimal", "workstation", "developer", "yocto-builder"], chain)
        self.assertIn("yocto-build", profile["software"]["bundles"])

    def test_yocto_builder_docs_extends_yocto_builder_and_is_much_larger(self) -> None:
        profile, chain = render_manifest.resolve_profile("yocto-builder-docs")
        self.assertEqual(["minimal", "workstation", "developer", "yocto-builder", "yocto-builder-docs"], chain)
        self.assertIn("yocto-build", profile["software"]["bundles"])
        self.assertIn("yocto-build-docs", profile["software"]["bundles"])
        lean_entry = next(e for e in index_entries() if e["id"] == "yocto-builder")
        full_entry = next(e for e in index_entries() if e["id"] == "yocto-builder-docs")
        self.assertNotEqual(lean_entry["summary"], full_entry["summary"])
        # the two summaries must tell the entries apart at a glance: different first clause
        lean_first_clause = lean_entry["summary"].split(":")[0]
        full_first_clause = full_entry["summary"].split(":")[0]
        self.assertNotEqual(lean_first_clause, full_first_clause)
        self.assertRegex(lean_first_clause.lower(), r"everyday")
        self.assertRegex(full_first_clause.lower(), r"larger")
        self.assertRegex(full_entry["summary"].lower(), r"documentation")
        self.assertRegex(full_entry["summary"].lower(), r"pdf")

    def test_yocto_builder_is_exported_as_an_archetype(self) -> None:
        data = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                          capture_output=True, text=True, check=True).stdout)
        by_id = {p["id"]: p for p in data["profiles"]}
        self.assertIn("yocto-builder", by_id)
        self.assertFalse(by_id["yocto-builder"]["derived"])


class ExportedCatalogTests(unittest.TestCase):
    """tools/export_catalog.py's bundle_catalog key: the contract the Studio's
    own half of this feature is built against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = export_catalog.collect()

    def test_bundle_catalog_key_has_the_documented_shape(self) -> None:
        catalog = self.data["bundle_catalog"]
        entries = index_entries()
        self.assertEqual(len(entries), len(catalog))
        self.assertEqual([e["id"] for e in entries], [c["id"] for c in catalog], "export order must follow index.yml")
        for entry in catalog:
            with self.subTest(entry=entry["id"]):
                for field in ("id", "name", "summary", "first_boot", "tags", "kind", "files", "services", "ports", "verified"):
                    self.assertIn(field, entry)
                self.assertIsInstance(entry["tags"], list)
                self.assertIsInstance(entry["first_boot"], list)
                for step in entry["first_boot"]:
                    self.assertIsInstance(step, str)
                self.assertIsInstance(entry["services"], list)
                self.assertIsInstance(entry["ports"], list)
                self.assertIsInstance(entry["verified"], bool)
                self.assertIsInstance(entry["files"], dict)
                self.assertIn("bundle.json", entry["files"])
                manifest_rel = json.loads(entry["files"]["bundle.json"])["manifest"]
                self.assertIn(manifest_rel, entry["files"])

    def test_empty_first_boot_round_trips_as_an_empty_list_not_dropped(self) -> None:
        by_id = {e["id"]: e for e in self.data["bundle_catalog"]}
        for entry_id in ("yocto-builder", "yocto-builder-docs", "ai-workstation"):
            with self.subTest(entry=entry_id):
                self.assertIn("first_boot", by_id[entry_id])
                self.assertEqual([], by_id[entry_id]["first_boot"])
        self.assertTrue(by_id["kubernetes-server"]["first_boot"])

    def test_template_is_not_in_the_export(self) -> None:
        ids = {e["id"] for e in self.data["bundle_catalog"]}
        self.assertNotIn("template-appliance", ids)

    def test_kind_is_the_profile_the_manifest_names(self) -> None:
        by_id = {e["id"]: e for e in self.data["bundle_catalog"]}
        self.assertEqual("yocto-builder", by_id["yocto-builder"]["kind"])
        self.assertEqual("yocto-builder-docs", by_id["yocto-builder-docs"]["kind"])
        self.assertEqual("container-registry", by_id["container-registry"]["kind"])
        self.assertEqual("kubernetes-server", by_id["kubernetes-server"]["kind"])

    def test_services_and_ports_match_the_index(self) -> None:
        by_id = {e["id"]: e for e in self.data["bundle_catalog"]}
        self.assertEqual(["nginx"], by_id["web-server-nginx"]["services"])
        self.assertEqual([22, 80, 443], by_id["web-server-nginx"]["ports"])
        self.assertEqual(["prometheus", "grafana"], by_id["monitoring-server"]["services"])
        self.assertEqual([], by_id["kodi-media-centre"]["services"])
        self.assertEqual([], by_id["kodi-media-centre"]["ports"])

    def test_files_are_byte_identical_to_the_folder(self) -> None:
        index_by_id = {e["id"]: e for e in index_entries()}
        for entry in self.data["bundle_catalog"]:
            folder = CATALOG_DIR / index_by_id[entry["id"]]["folder"]
            on_disk = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
            with self.subTest(entry=entry["id"]):
                self.assertEqual(set(on_disk), set(entry["files"]), "files map does not list exactly the folder's files")
                for path, raw in on_disk.items():
                    self.assertEqual(raw.decode("utf-8"), entry["files"][path], path)

    def test_no_starter_wording_leaks_into_the_export_or_the_docs(self) -> None:
        text = json.dumps(self.data["bundle_catalog"])
        self.assertNotIn("starter", text.lower())
        bundle_doc = (ROOT / "docs" / "BUNDLE.md").read_text(encoding="utf-8")
        self.assertNotIn("starter", bundle_doc.lower())


class DataStoreApplianceTests(unittest.TestCase):
    """The nine enterprise data-store appliances (ClickHouse, InfluxDB,
    Cassandra, CockroachDB, CouchDB, Valkey, MongoDB, Neo4j, Milvus): every
    one of them ships with no working credential and a closed port, on top
    of the generic catalog checks above."""

    IDS = {
        "clickhouse-server": "clickhouse",
        "influxdb-server": "influxdb",
        "cassandra-server": "cassandra",
        "cockroachdb-server": "cockroach",
        "couchdb-server": "couchdb",
        "valkey-server": "valkey",
        "mongodb-server": "mongodb",
        "neo4j-server": "neo4j",
        "milvus-server": "milvus",
    }

    def test_all_nine_are_indexed_and_verified(self) -> None:
        by_id = {e["id"]: e for e in index_entries()}
        for entry_id, service_name in self.IDS.items():
            with self.subTest(entry=entry_id):
                self.assertIn(entry_id, by_id, f"{entry_id} is missing from bundle-catalog/index.yml")
                entry = by_id[entry_id]
                self.assertTrue(entry.get("verified"), f"{entry_id} must be verified: true")
                self.assertEqual([service_name], entry["services"])

    def test_every_one_closes_its_data_port_by_default(self) -> None:
        """None of the nine ships a working credential, so none of them opens
        anything but SSH by default (docs/BUNDLE.md, "Fail closed")."""
        by_id = {e["id"]: e for e in index_entries()}
        for entry_id in self.IDS:
            with self.subTest(entry=entry_id):
                profile, _ = render_manifest.resolve_profile(entry_id)
                self.assertEqual(["22/tcp"], profile["security"]["open_ports"],
                                  f"{entry_id} must close every port but SSH until a real credential exists")
                self.assertEqual([22], by_id[entry_id]["ports"])

    def test_every_service_is_gpu_free_and_pinned_to_a_real_tag(self) -> None:
        catalog = render_manifest.load_yaml(ROOT / "profiles" / "catalog.yml")
        by_service_id = {s["id"]: s for s in catalog["services"]}
        for entry_id, service_name in self.IDS.items():
            with self.subTest(service=service_name):
                self.assertIn(service_name, by_service_id, f"{service_name} is missing from profiles/catalog.yml")
                service = by_service_id[service_name]
                self.assertFalse(service.get("gpu", False), f"{service_name} must not need a GPU")
                for image in service["images"].values():
                    self.assertIn(":", image, f"{service_name}: {image} is not pinned to a tag")
                    tag = image.rsplit(":", 1)[1]
                    self.assertNotEqual("latest", tag, f"{service_name}: {image} is pinned to latest")

    def test_profile_and_catalog_service_agree_on_the_image(self) -> None:
        """The image a profile's software.services entry runs must be the exact
        same pinned tag profiles/catalog.yml advertises for that service."""
        catalog = render_manifest.load_yaml(ROOT / "profiles" / "catalog.yml")
        by_service_id = {s["id"]: s for s in catalog["services"]}
        for entry_id, service_name in self.IDS.items():
            with self.subTest(entry=entry_id):
                profile, _ = render_manifest.resolve_profile(entry_id)
                services = profile["software"]["services"]
                self.assertEqual(1, len(services))
                self.assertEqual(service_name, services[0]["name"])
                self.assertEqual(by_service_id[service_name]["images"]["default"], services[0]["image"])

    def test_mongodb_and_neo4j_are_community_never_enterprise(self) -> None:
        mongo_profile, _ = render_manifest.resolve_profile("mongodb-server")
        mongo_image = mongo_profile["software"]["services"][0]["image"]
        self.assertNotIn("enterprise", mongo_image.lower())
        self.assertEqual("docker.io/library/mongo", mongo_image.rsplit(":", 1)[0])

        neo4j_profile, _ = render_manifest.resolve_profile("neo4j-server")
        neo4j_image = neo4j_profile["software"]["services"][0]["image"]
        self.assertNotIn("enterprise", neo4j_image.lower())
        self.assertEqual("docker.io/library/neo4j", neo4j_image.rsplit(":", 1)[0])

    def test_valkey_is_not_redis(self) -> None:
        """docs/BUNDLE.md: ship the BSD-licensed fork, never the software it
        forked from, and the image must actually come from the fork's own
        publisher rather than a same-named impostor."""
        profile, _ = render_manifest.resolve_profile("valkey-server")
        image = profile["software"]["services"][0]["image"]
        self.assertTrue(image.startswith("docker.io/valkey/valkey:"), image)
        entry = next(e for e in index_entries() if e["id"] == "valkey-server")
        self.assertIn("protocol", entry["summary"].lower())

    def test_none_of_the_nine_ships_a_working_password_file(self) -> None:
        """Each of these fails closed by pointing a *_FILE / *_PATH secret env
        var at a host path this profile deliberately does not ship, exactly
        like database-server's postgres superuser password. None of the
        mounted "secret" targets may be shipped as a software.files entry
        (which would make it a real, checked-in credential)."""
        secret_targets = {
            "clickhouse-server": "/run/secrets/clickhouse-password",
            "influxdb-server": "/run/secrets/influxdb-admin-password",
            "mongodb-server": "/run/secrets/mongo-root-password",
            "neo4j-server": "/run/secrets/neo4j-auth",
            "couchdb-server": "/opt/couchdb/etc/local.d/admins.ini",
            "valkey-server": "/etc/valkey/users.acl",
        }
        for entry_id, target in secret_targets.items():
            with self.subTest(entry=entry_id):
                profile, _ = render_manifest.resolve_profile(entry_id)
                service = profile["software"]["services"][0]
                mounted = [v for v in service.get("volumes", []) if v.endswith(f":{target}:ro")]
                self.assertTrue(mounted, f"{entry_id} must bind-mount a not-shipped secret at {target}")
                shipped_paths = {f["path"] for f in profile.get("software", {}).get("files", [])}
                self.assertFalse(shipped_paths, f"{entry_id} must not ship any of its own files as a credential")

    def test_milvus_is_the_only_one_shipping_files_and_needs_the_newer_engine(self) -> None:
        for entry_id in self.IDS:
            with self.subTest(entry=entry_id):
                folder = CATALOG_DIR / entry_id
                bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
                profile, _ = render_manifest.resolve_profile(entry_id)
                ships_files = bool(profile.get("software", {}).get("files"))
                if entry_id == "milvus-server":
                    self.assertTrue(ships_files)
                    self.assertEqual("0.2.0", bundle["engine"]["min"])
                else:
                    self.assertFalse(ships_files)
                    self.assertEqual("0.1.0", bundle["engine"]["min"])

    def test_milvus_authorization_is_turned_on_since_it_is_off_upstream_by_default(self) -> None:
        profile, _ = render_manifest.resolve_profile("milvus-server")
        files = {f["path"]: f["content"] for f in profile["software"]["files"]}
        self.assertIn("/etc/milvus/user.yaml", files)
        self.assertIn("authorizationEnabled: true", files["/etc/milvus/user.yaml"])
class PlatformAndNetworkingApplianceTests(unittest.TestCase):
    """The nine enterprise platform, identity and networking appliances:
    event-streaming, stream-processing, cluster-coordination, identity-sso,
    secret-manager, edge-proxy, authoritative-dns, distributed-compute and
    devops-platform. Every one of them has no built-in login or access
    control on its data-plane or admin surface out of the box, so this
    catalog's fail-closed rule (docs/BUNDLE.md) closes every port on all of
    them except 22/tcp, the one exception being authoritative-dns's plain
    DNS port, which is meant to be publicly reachable the way any
    authoritative nameserver is."""

    CLOSED_BY_DEFAULT = {
        "event-streaming": "kafka",
        "stream-processing": "jobmanager",
        "cluster-coordination": "zookeeper",
        "identity-sso": "keycloak",
        "secret-manager": "openbao",
        "distributed-compute": "ray-head",
    }

    def test_unauthenticated_services_close_every_port_but_ssh(self) -> None:
        for entry_id in self.CLOSED_BY_DEFAULT:
            with self.subTest(entry=entry_id):
                profile, _ = render_manifest.resolve_profile(entry_id)
                self.assertEqual(["22/tcp"], profile["security"]["open_ports"],
                                  f"{entry_id} must open nothing but SSH by default")

    def test_secret_manager_ships_sealed_and_uninitialised(self) -> None:
        """OpenBao must never carry an unseal key or root token, and its API
        port must stay closed until an operator initialises it locally."""
        profile, _ = render_manifest.resolve_profile("secret-manager")
        self.assertNotIn("8200/tcp", profile["security"]["open_ports"])
        service = profile["software"]["services"][0]
        self.assertEqual("openbao", service["name"])
        blob = json.dumps(service)
        for needle in ("unseal", "root_token", "recovery_key"):
            self.assertNotIn(needle, blob.lower(), f"secret-manager must not ship a {needle}")
        # HashiCorp Vault's licence changed to a Business Source Licence; this
        # catalog ships the open-source fork instead.
        self.assertNotRegex(blob.lower(), r"hashicorp|vault:")
        raw = (CATALOG_DIR / "secret-manager" / "profiles" / "secret-manager.yml").read_text(encoding="utf-8")
        self.assertRegex(raw, r"[Bb]usiness [Ss]ource")

    def test_identity_provider_ships_no_bootstrap_admin_credential(self) -> None:
        profile, _ = render_manifest.resolve_profile("identity-sso")
        service = profile["software"]["services"][0]
        env = service.get("env", {})
        self.assertNotIn("KC_BOOTSTRAP_ADMIN_USERNAME", env)
        self.assertNotIn("KC_BOOTSTRAP_ADMIN_PASSWORD", env)
        self.assertNotIn("8080/tcp", profile["security"]["open_ports"])

    def test_event_streaming_is_single_broker_kraft_with_no_zookeeper(self) -> None:
        profile, _ = render_manifest.resolve_profile("event-streaming")
        service = profile["software"]["services"][0]
        self.assertEqual("kafka", service["name"])
        env = service["env"]
        self.assertIn("controller", env["KAFKA_PROCESS_ROLES"])
        blob = json.dumps(profile).lower()
        self.assertNotIn("zookeeper_connect", blob)

    def test_cluster_coordination_summary_says_kafka_no_longer_needs_it(self) -> None:
        entry = next(e for e in index_entries() if e["id"] == "cluster-coordination")
        combined = (entry["summary"] + " ".join(entry["first_boot"])).lower()
        self.assertRegex(combined, r"kafka")
        self.assertRegex(combined, r"not needed|no longer|has not needed")

    def test_stream_processing_task_manager_reaches_the_job_manager(self) -> None:
        profile, _ = render_manifest.resolve_profile("stream-processing")
        by_name = {s["name"]: s for s in profile["software"]["services"]}
        self.assertEqual({"jobmanager", "taskmanager"}, set(by_name))
        for service in by_name.values():
            self.assertEqual("host.containers.internal", service["env"]["JOB_MANAGER_RPC_ADDRESS"])
        self.assertNotIn("8081/tcp", profile["security"]["open_ports"])

    def test_edge_proxy_never_publishes_the_admin_interface(self) -> None:
        profile, _ = render_manifest.resolve_profile("edge-proxy")
        service = profile["software"]["services"][0]
        for port in service.get("ports", []):
            self.assertNotIn("9901", port, "Envoy's admin interface must never be published to the host")
        config = next(f["content"] for f in profile["software"]["files"] if f["path"] == "/etc/envoy/envoy.yaml")
        self.assertIn("127.0.0.1", config)
        self.assertIn("9901", config)
        self.assertIn("10000/tcp", profile["security"]["open_ports"])

    def test_authoritative_dns_ships_its_api_turned_off_and_opens_only_dns(self) -> None:
        profile, _ = render_manifest.resolve_profile("authoritative-dns")
        pdns_conf = next(f["content"] for f in profile["software"]["files"] if f["path"] == "/etc/powerdns/pdns.conf")
        self.assertIn("api=no", pdns_conf)
        self.assertEqual({"22/tcp", "53/tcp", "53/udp"}, set(profile["security"]["open_ports"]))
        service = profile["software"]["services"][0]
        self.assertIn("53:53/tcp", service["ports"])
        self.assertIn("53:53/udp", service["ports"])
        # RFC 5737 documentation range only, never a real address.
        zone = next(f["content"] for f in profile["software"]["files"] if f["path"].endswith(".zone"))
        self.assertIn("192.0.2.1", zone)

    def test_distributed_compute_closes_dashboard_client_and_gcs_ports(self) -> None:
        profile, _ = render_manifest.resolve_profile("distributed-compute")
        service = profile["software"]["services"][0]
        self.assertEqual({"6379:6379", "8265:8265", "10001:10001"}, set(service["ports"]))
        self.assertEqual(["22/tcp"], profile["security"]["open_ports"])

    def test_devops_platform_is_community_edition_never_enterprise(self) -> None:
        profile, _ = render_manifest.resolve_profile("devops-platform")
        service = profile["software"]["services"][0]
        self.assertIn("gitlab-ce", service["image"])
        self.assertNotIn("gitlab-ee", service["image"])
        entry = next(e for e in index_entries() if e["id"] == "devops-platform")
        self.assertIn("Enterprise Edition", entry["summary"])
        self.assertRegex(entry["summary"], r"8 GB")
        self.assertRegex(entry["summary"], r"16 GB")
        self.assertRegex(entry["summary"], r"40 GB")

    def test_devops_platform_ssh_stays_open_web_ui_stays_closed(self) -> None:
        profile, _ = render_manifest.resolve_profile("devops-platform")
        self.assertEqual({"22/tcp", "2222/tcp"}, set(profile["security"]["open_ports"]))
        entry = next(e for e in index_entries() if e["id"] == "devops-platform")
        self.assertIn("initial_root_password", " ".join(entry["first_boot"]))

    def test_devops_platform_ships_no_root_password(self) -> None:
        profile, _ = render_manifest.resolve_profile("devops-platform")
        gitlab_rb = next(f["content"] for f in profile["software"]["files"] if f["path"] == "/etc/gitlab/gitlab.rb")
        self.assertNotIn("initial_root_password", gitlab_rb)
        self.assertNotIn("GITLAB_ROOT_PASSWORD", gitlab_rb)

    def test_new_platform_entries_are_present_and_verified(self) -> None:
        ids = {"event-streaming", "stream-processing", "cluster-coordination", "identity-sso", "secret-manager",
               "edge-proxy", "authoritative-dns", "distributed-compute", "devops-platform"}
        by_id = {e["id"]: e for e in index_entries()}
        self.assertTrue(ids.issubset(by_id), ids - set(by_id))
        for entry_id in ids:
            with self.subTest(entry=entry_id):
                self.assertTrue(by_id[entry_id]["verified"])


if __name__ == "__main__":
    unittest.main()
