"""The configuration bundle contract: docs/BUNDLE.md, schema/bundle.schema.json,
tools/synos and tools/export_catalog.py."""
from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNOS = ROOT / "tools" / "synos"


def load_cli():
    spec = importlib.util.spec_from_loader("synos_cli", importlib.machinery.SourceFileLoader("synos_cli", str(SYNOS)))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run(*args: str, cwd: Path = ROOT) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SYNOS), "--json", *args], capture_output=True, text=True, check=False, cwd=cwd)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = {"raw": result.stdout, "stderr": result.stderr}
    return result.returncode, payload


def write_bundle(directory: Path, descriptor: dict | None, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text, encoding="utf-8")
    if descriptor is not None:
        (directory / "bundle.json").write_text(json.dumps(descriptor), encoding="utf-8")
    return directory


MANIFEST = (ROOT / "manifests" / "test-build.yml").read_text(encoding="utf-8").replace("brand: synos", "brand: example-acme")


class BundleValidationTests(unittest.TestCase):
    def test_engine_version_and_catalog_agree(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        code, payload = run("version")
        self.assertEqual(0, code)
        self.assertEqual(version, payload["version"])
        catalog = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                            capture_output=True, text=True, check=True).stdout)
        self.assertEqual(version, catalog["engine"])
        self.assertIn(catalog["bundle_format"], payload["bundle_formats"])
        self.assertIn("bundle", catalog["schemas"])
        self.assertEqual({"debian", "ubuntu"}, {b["id"] for b in catalog["bases"]})
        self.assertFalse(any("anduin" in r["package"] for r in catalog["roles"]))

    def test_a_minimal_bundle_is_valid_as_zip_and_as_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp) / "b", {"format": 1, "manifest": "manifests/unit-bundle.yml", "engine": {"min": "0.1.0"}},
                                  {"manifests/unit-bundle.yml": MANIFEST, "README.md": "doc"})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(0, code, payload)
            self.assertEqual([], payload["report"]["errors"])
            self.assertEqual("manifests/unit-bundle.yml", payload["report"]["manifest"])
            archive = Path(tmp) / "b.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for path in bundle.rglob("*"):
                    if path.is_file():
                        zf.write(path, str(path.relative_to(bundle)))
            code, payload = run("bundle", "validate", str(archive))
            self.assertEqual(0, code, payload)

    def test_channel_stable_and_development_validate_absent_defaults_and_others_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for channel in ("stable", "development"):
                bundle = write_bundle(Path(tmp) / channel, {"format": 1, "manifest": "manifests/x.yml",
                                                             "engine": {"min": "0.1.0"}, "channel": channel},
                                      {"manifests/x.yml": MANIFEST})
                code, payload = run("bundle", "validate", str(bundle))
                self.assertEqual(0, code, payload)
                self.assertEqual([], payload["report"]["errors"])
            # absent: still valid, exactly like every bundle generated before "channel" existed
            bundle = write_bundle(Path(tmp) / "absent", {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}},
                                  {"manifests/x.yml": MANIFEST})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(0, code, payload)
            self.assertEqual([], payload["report"]["errors"])
            # anything else is refused, not silently accepted
            bundle = write_bundle(Path(tmp) / "bogus", {"format": 1, "manifest": "manifests/x.yml",
                                                         "engine": {"min": "0.1.0"}, "channel": "nightly"},
                                  {"manifests/x.yml": MANIFEST})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(1, code)
            self.assertTrue(any("channel" in e for e in payload["report"]["errors"]), payload["report"]["errors"])

    def test_unsupported_format_future_engine_and_foreign_paths_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp), {"format": 2, "manifest": "manifests/x.yml", "engine": {"min": "99.0.0"}},
                                  {"manifests/x.yml": MANIFEST, "mods/evil.sh": "rm -rf /", "makefile": "x"})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(1, code)
            errors = "\n".join(payload["report"]["errors"])
            self.assertIn("format 2 is not supported", errors)
            self.assertIn("needs engine 99.0.0", errors)
            self.assertNotIn("mods/evil.sh", errors, "foreign files are ignored, never applied")
            warnings = "\n".join(payload["report"]["warnings"])
            self.assertIn("mods/evil.sh", warnings)
            self.assertIn("makefile", warnings)

    def test_missing_descriptor_bad_schema_and_unknown_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = run("bundle", "validate", str(write_bundle(Path(tmp) / "a", None, {"manifests/x.yml": MANIFEST})))
            self.assertEqual(1, code)
            self.assertIn("bundle.json is missing", payload["report"]["errors"][0])
            bad = MANIFEST.replace("profile: workstation", "profile: does-not-exist").replace("brand: example-acme", "brand: nobody")
            bad = bad.replace("regions: [fr, us]", "regions: [fr, atlantis]") if "regions: [fr, us]" in bad else bad
            bundle = write_bundle(Path(tmp) / "b", {"format": 1, "manifest": "manifests/x.yml", "surprise": True},
                                  {"manifests/x.yml": bad})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(1, code)
            self.assertTrue(any("surprise" in e for e in payload["report"]["errors"]), payload["report"]["errors"])
            (bundle / "bundle.json").write_text(json.dumps({"format": 1, "manifest": "manifests/x.yml"}), encoding="utf-8")
            code, payload = run("bundle", "validate", str(bundle))
            errors = "\n".join(payload["report"]["errors"])
            self.assertIn("profile 'does-not-exist'", errors)
            self.assertIn("brand 'nobody'", errors)
            self.assertIn("region 'atlantis'", errors)

    def test_traversal_is_rejected_inside_zip_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("bundle.json", json.dumps({"format": 1, "manifest": "manifests/x.yml"}))
                zf.writestr("manifests/x.yml", MANIFEST)
                zf.writestr("manifests/../../etc/passwd", "x")
            code, payload = run("bundle", "validate", str(archive))
            self.assertEqual(1, code)
            self.assertTrue(any("unsafe path" in e for e in payload["report"]["errors"]), payload["report"]["errors"])


class TarBundleTests(unittest.TestCase):
    def tar_with(self, path: Path, files: dict[str, str], extra=None) -> Path:
        with tarfile.open(path, "w:gz") as tf:
            for name, text in files.items():
                data = text.encode("utf-8")
                info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o755 if name.endswith(".sh") else 0o644
                tf.addfile(info, io.BytesIO(data))
            if extra:
                extra(tf)
        return path

    def test_a_tar_gz_bundle_validates_like_a_zip(self) -> None:
        files = {"bundle.json": json.dumps({"format": 1, "manifest": "manifests/x.yml"}), "manifests/x.yml": MANIFEST, "build.sh": "#!/bin/sh\n"}
        with tempfile.TemporaryDirectory() as tmp:
            archive = self.tar_with(Path(tmp) / "b.tar.gz", files)
            code, payload = run("bundle", "validate", str(archive))
            self.assertEqual(0, code, payload)
            self.assertEqual(sorted(files), payload["report"]["files"])

    def test_tar_traversal_links_and_devices_are_refused(self) -> None:
        files = {"bundle.json": json.dumps({"format": 1, "manifest": "manifests/x.yml"}), "manifests/x.yml": MANIFEST}
        with tempfile.TemporaryDirectory() as tmp:
            evil = self.tar_with(Path(tmp) / "evil.tar.gz", dict(files, **{"manifests/../../etc/passwd": "x"}))
            code, payload = run("bundle", "validate", str(evil))
            self.assertEqual(1, code)
            self.assertTrue(any("unsafe path" in e for e in payload["report"]["errors"]))

            def add_link(tf):
                info = tarfile.TarInfo("keys/link.asc"); info.type = tarfile.SYMTYPE; info.linkname = "/etc/passwd"
                tf.addfile(info)
            linked = self.tar_with(Path(tmp) / "link.tar.gz", files, add_link)
            code, payload = run("bundle", "validate", str(linked))
            self.assertNotEqual(0, code)
            self.assertIn("not a regular file", json.dumps(payload))


class IntegrityTests(unittest.TestCase):
    FILES = {"manifests/x.yml": "schema_version: 1\nname: x\nversion: 1.0.0\nbase: ubuntu\nsuite: resolute\narch: amd64\n"
                                "profile: workstation\nregions: [fr]\nbrand: example-acme\n", "README.md": "hello\n"}

    def descriptor(self, files: dict[str, str]) -> dict:
        digests = [{"path": n, "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest()} for n, t in sorted(files.items())]
        return {"format": 1, "name": "x", "manifest": "manifests/x.yml", "files": digests}

    def test_digests_report_changed_missing_and_unlisted_files_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory), self.descriptor(self.FILES), self.FILES)
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(0, code, payload)
            integrity = payload["report"]["integrity"]
            self.assertEqual({"declared": 2, "verified": 2, "modified": [], "missing": [], "undeclared": []}, integrity)
            self.assertEqual("none", payload["report"]["signature"]["status"])
            (bundle / "README.md").write_text("edited by hand\n", encoding="utf-8")
            (bundle / "answers" ).mkdir()
            (bundle / "answers" / "a.yml").write_text("a: 1\n", encoding="utf-8")
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(0, code, "a hand-edited bundle stays valid")
            integrity = payload["report"]["integrity"]
            self.assertEqual(["README.md"], integrity["modified"])
            self.assertEqual(["answers/a.yml"], integrity["undeclared"])
            self.assertTrue(any("changed after generation" in w for w in payload["report"]["warnings"]))

    def test_a_signature_is_verified_against_trusted_signers(self) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
        except ImportError:
            self.skipTest("python3-cryptography is not installed")
        cli = load_cli()
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        descriptor = self.descriptor(self.FILES)
        descriptor["signature"] = {"alg": "ed25519", "key_id": "test-1", "value": key.sign(cli.signed_message(descriptor)).hex()}
        with tempfile.TemporaryDirectory() as directory:
            bundle = write_bundle(Path(directory) / "b", descriptor, self.FILES)
            signers = Path(directory) / "signers.json"
            signers.write_text(json.dumps({"signers": {"test-1": {"name": "Test signer", "public_key": public}}}), encoding="utf-8")
            env = dict(os.environ, SYNOS_TRUSTED_SIGNERS=str(signers))

            def validate() -> dict:
                result = subprocess.run([sys.executable, str(SYNOS), "--json", "bundle", "validate", str(bundle)],
                                        capture_output=True, text=True, check=False, cwd=ROOT, env=env)
                return json.loads(result.stdout)["report"]

            self.assertEqual({"status": "valid", "key_id": "test-1", "signer": "Test signer"}, validate()["signature"])
            self.assertEqual("unknown-signer", run("bundle", "validate", str(bundle))[1]["report"]["signature"]["status"])
            descriptor["files"][0]["sha256"] = "0" * 64
            (bundle / "bundle.json").write_text(json.dumps(descriptor), encoding="utf-8")
            report = validate()
            self.assertEqual("invalid", report["signature"]["status"])
            self.assertTrue(any("signature invalid" in w for w in report["warnings"]))
            self.assertEqual([], report["errors"], "a bad signature is a warning; the content is validated as usual")


class BundleApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = ROOT / "manifests" / "unit-apply.yml"
        self.profile = ROOT / "profiles" / "unit-apply.yml"
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in (self.manifest, self.profile)])

    def test_apply_writes_validates_and_refuses_silent_overwrites(self) -> None:
        profile = "id: unit-apply\nextends: workstation\ndescription: unit test\nsoftware:\n  packages:\n    add: [keepassxc]\n"
        manifest = MANIFEST.replace("profile: workstation", "profile: unit-apply")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp), {"format": 1, "manifest": "manifests/unit-apply.yml"},
                                  {"manifests/unit-apply.yml": manifest, "profiles/unit-apply.yml": profile, "README.md": "doc"})
            code, payload = run("bundle", "apply", str(bundle))
            self.assertEqual(0, code, payload)
            self.assertEqual(["manifests/unit-apply.yml", "profiles/unit-apply.yml"], payload["applied"])
            self.assertIn("README.md", payload["skipped"])
            self.assertFalse((ROOT / "README.md").read_text(encoding="utf-8").startswith("doc"))
            self.assertIn("profile minimal > workstation > unit-apply", payload["validation"])
            # identical content: nothing to do, still valid
            code, payload = run("bundle", "apply", str(bundle))
            self.assertEqual(0, code)
            self.assertEqual([], payload["applied"])
            # changed content: refused without --force
            (bundle / "profiles/unit-apply.yml").write_text(profile + "    remove: [gnome-games]\n", encoding="utf-8")
            code, payload = run("bundle", "apply", str(bundle))
            self.assertEqual(1, code)
            self.assertEqual(["profiles/unit-apply.yml"], payload["conflicts"])
            self.assertNotIn("gnome-games", self.profile.read_text(encoding="utf-8"))
            code, payload = run("bundle", "apply", "--force", str(bundle))
            self.assertEqual(0, code, payload)
            self.assertIn("gnome-games", self.profile.read_text(encoding="utf-8"))


class HostAndBuildTests(unittest.TestCase):
    def test_check_reports_the_host_in_json(self) -> None:
        code, payload = run("check")
        self.assertIn(code, (0, 2))
        self.assertEqual(code == 0, payload["host"]["ok"])
        self.assertIn("free_gb", payload["host"])

    def test_build_plans_pull_or_local_image_and_the_container_command(self) -> None:
        cli = load_cli()
        calls: list[list[str]] = []

        class Result:
            returncode = 0

        def fake_run(step, **kwargs):
            calls.append(list(step))
            return Result()

        original = cli.subprocess.run
        cli.subprocess.run = fake_run
        cli.sys.stderr = open(os.devnull, 'w')
        cli.host_report = lambda: {"problems": [], "engine": "/usr/bin/podman"}
        try:
            result = cli.container_build(ROOT / "manifests" / "test-build.yml", None, True, None)
            self.assertEqual("ghcr.io/synthot/synos-builder:ubuntu-resolute", result["image"])
            self.assertEqual(["/usr/bin/podman", "pull", "ghcr.io/synthot/synos-builder:ubuntu-resolute"], calls[0])
            self.assertIn("--privileged", calls[1])
            self.assertIn("MANIFEST=manifests/test-build.yml", calls[1])
            self.assertIn("PACKAGES_FLAGS=--strict", calls[1])
            calls.clear()
            result = cli.container_build(ROOT / "manifests" / "debian-workstation.yml", None, False, None)
            self.assertEqual("synos-builder-debian:trixie", result["image"])
            self.assertEqual("build", calls[0][1])
            self.assertIn("bases/debian/Containerfile", calls[0])
            calls.clear()
            result = cli.container_build(ROOT / "manifests" / "test-build.yml", "registry.acme/builder:x", False, None)
            self.assertEqual(1, len(calls), "a named image is neither pulled nor built")
            self.assertIn("registry.acme/builder:x", calls[0])
        finally:
            cli.subprocess.run = original
            cli.sys.stderr.close()
            cli.sys.stderr = sys.stderr

    def test_container_root_is_passed_to_podman_before_every_subcommand(self) -> None:
        cli = load_cli()
        calls: list[list[str]] = []

        class Result:
            returncode = 0

        def fake_run(step, **kwargs):
            calls.append(list(step))
            return Result()

        original = cli.subprocess.run
        cli.subprocess.run = fake_run
        cli.sys.stderr = open(os.devnull, 'w')
        cli.host_report = lambda: {"problems": [], "engine": "/usr/bin/podman"}
        try:
            result = cli.container_build(ROOT / "manifests" / "test-build.yml", None, True, None,
                                         container_root="/mnt/big/synos-storage", container_runroot="/mnt/big/synos-runroot")
            self.assertEqual("ghcr.io/synthot/synos-builder:ubuntu-resolute", result["image"])
            self.assertEqual(["/usr/bin/podman", "--root", "/mnt/big/synos-storage", "--runroot", "/mnt/big/synos-runroot",
                              "pull", "ghcr.io/synthot/synos-builder:ubuntu-resolute"], calls[0])
            self.assertEqual("--root", calls[1][1])
            self.assertIn("--privileged", calls[1])
        finally:
            cli.subprocess.run = original
            cli.sys.stderr.close()
            cli.sys.stderr = sys.stderr

    def test_container_root_is_refused_for_docker_before_any_command_runs(self) -> None:
        cli = load_cli()
        calls: list[list[str]] = []

        def fake_run(step, **kwargs):
            calls.append(list(step))
            raise AssertionError("docker must never be invoked once a location was asked for and refused")

        original = cli.subprocess.run
        cli.subprocess.run = fake_run
        cli.host_report = lambda: {"problems": [], "engine": "/usr/bin/docker"}
        try:
            with self.assertRaises(cli.Failure) as raised:
                cli.container_build(ROOT / "manifests" / "test-build.yml", None, True, None, container_root="/mnt/big/synos-storage")
            self.assertEqual([], calls)
            message = str(raised.exception)
            self.assertIn("daemon-wide", message)
            self.assertIn("data-root", message)
            self.assertIn("/etc/docker/daemon.json", message)
            self.assertIn("/var/snap/docker/current/config/daemon.json", message)
            self.assertIn("snap connect docker:removable-media", message)
        finally:
            cli.subprocess.run = original

    def test_an_unset_container_root_changes_nothing(self) -> None:
        cli = load_cli()
        calls: list[list[str]] = []

        class Result:
            returncode = 0

        def fake_run(step, **kwargs):
            calls.append(list(step))
            return Result()

        original = cli.subprocess.run
        cli.subprocess.run = fake_run
        cli.sys.stderr = open(os.devnull, 'w')
        cli.host_report = lambda: {"problems": [], "engine": "/usr/bin/podman"}
        try:
            cli.container_build(ROOT / "manifests" / "test-build.yml", None, True, None)
            self.assertEqual(["/usr/bin/podman", "pull", "ghcr.io/synthot/synos-builder:ubuntu-resolute"], calls[0])
            self.assertNotIn("--root", calls[0])
        finally:
            cli.subprocess.run = original
            cli.sys.stderr.close()
            cli.sys.stderr = sys.stderr

    def test_makefile_routes_container_build_through_the_tool(self) -> None:
        makefile = (ROOT / "makefile").read_text(encoding="utf-8")
        self.assertIn("tools/synos build", makefile)
        self.assertNotIn("configurator", makefile)
        self.assertFalse((ROOT / "configurator").exists())
        workflow = (ROOT / ".github" / "workflows" / "builder-image.yml").read_text(encoding="utf-8")
        for base, suite in (("ubuntu", "resolute"), ("debian", "trixie")):
            self.assertIn(f"{{base: {base}, suite: {suite}}}", workflow)
        self.assertIn("runs-on: ubuntu-24.04-arm", workflow)
        self.assertNotIn("if: matrix.", workflow, "the matrix context is not available in a job-level if")
        self.assertIn("podman manifest create", workflow)


if __name__ == "__main__":
    unittest.main()


class LauncherTests(unittest.TestCase):
    def test_launchers_are_exported_and_allowed_in_bundles(self) -> None:
        catalog = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                            capture_output=True, text=True, check=True).stdout)
        self.assertEqual({"build.sh", "build.ps1", "build.cmd", "build.command"}, set(catalog["launchers"]))
        self.assertIn("exec sh ./build.sh", catalog["launchers"]["build.command"])
        self.assertEqual((ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8"), catalog["launchers"]["build.sh"])
        self.assertIn("synos build /bundle --output /bundle/dist", catalog["launchers"]["build.sh"])
        # build.cmd is CRLF in the repo; the export must keep it byte for byte
        # (read_text would translate CRLF to LF, making every downloaded
        # bundle.cmd look permanently stale to the update check).
        self.assertIn("\r\n", catalog["launchers"]["build.cmd"])
        self.assertEqual((ROOT / "tools" / "bundle_launcher.cmd").read_bytes().decode("utf-8"), catalog["launchers"]["build.cmd"])
        self.assertIn("synos build /bundle --output /bundle/dist", catalog["launchers"]["build.ps1"])
        self.assertIn("build.ps1", catalog["launchers"]["build.cmd"])
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp), {"format": 1, "manifest": "manifests/x.yml"},
                                  {"manifests/x.yml": MANIFEST, **catalog["launchers"], "README.md": "doc"})
            code, payload = run("bundle", "validate", str(bundle))
            self.assertEqual(0, code, payload)
            code, payload = run("bundle", "apply", str(bundle))
            self.assertEqual(0, code, payload)
            self.assertEqual(["manifests/x.yml"], payload["applied"])
            for name in ("build.sh", "build.ps1", "build.cmd", "build.command"):
                self.assertIn(name, payload["skipped"])
            # the engine's own build.sh is untouched by a bundle launcher of the same name
            self.assertTrue((ROOT / "build.sh").read_text(encoding="utf-8").startswith("#!/bin/bash"))
            self.assertFalse((ROOT / "build.ps1").exists())
            (ROOT / "manifests" / "x.yml").unlink()

    def test_launcher_script_is_posix_sh(self) -> None:
        result = subprocess.run(["sh", "-n", str(ROOT / "tools" / "bundle_launcher.sh")], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        text = (ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8")
        for needle in ("apt-get install -y podman", "dnf install -y podman", "zypper install -y podman", "pacman -S --noconfirm podman",
                       "--yes", "SYNOS_YES", "synos-cache-$base-$suite", "-v /opt/synos/new_building_os -v /opt/synos/image",
                       "--log /bundle/dist/build.log", "dd if=", "podman machine init --rootful", "brew install podman",
                       '--platform "linux/$arch"', "Use Rosetta", "archive/refs/heads/main.tar.gz", "SYNOS_ENGINE_SOURCE",
                       "synos-builder:$base-$suite-$source_id", "dist/image-build.log"):
            self.assertIn(needle, text, needle)
        windows = (ROOT / "tools" / "bundle_launcher.ps1").read_text(encoding="utf-8")
        for needle in ("winget install -e --id Docker.DockerDesktop", "SYNOS_YES", "--log /bundle/dist/build.log", "Rufus",
                       "archive/refs/heads/main.zip", "Expand-Archive", "Build-EngineImage", "SYNOS_ENGINE_SOURCE"):
            self.assertIn(needle, windows, needle)

    def test_launcher_without_a_runtime_explains_and_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = write_bundle(Path(tmp), {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}},
                                  {"manifests/x.yml": MANIFEST})
            shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
            fake = Path(tmp) / "bin"
            fake.mkdir()
            for tool in ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "tr", "dirname", "printf", "apt-get", "sudo"):
                found = shutil.which(tool)
                if found:
                    (fake / tool).symlink_to(found)
            env = {"PATH": str(fake), "HOME": tmp, "SYNOS_NO_UPDATE_CHECK": "1"}
            check = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, check.returncode, check.stderr)
            self.assertIn("podman or docker is required", check.stderr)
            build = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, build.returncode)
            self.assertIn("install podman", build.stdout + build.stderr)
            self.assertNotIn("Traceback", build.stderr)
        for base, suite in (("ubuntu", "resolute"), ("debian", "trixie")):
            text = (ROOT / "bases" / base / "Containerfile").read_text(encoding="utf-8")
            self.assertIn("COPY . /opt/synos", text)
            self.assertIn("/usr/local/bin/synos", text)
        self.assertEqual((ROOT / ".containerignore").read_text(), (ROOT / ".dockerignore").read_text())

    def test_launcher_builds_the_engine_image_when_none_can_be_pulled(self) -> None:
        """A registry that refuses (or has no image) never stops a build: the launcher
        builds the image from the engine source and runs the build with it."""
        work = ROOT / ".build" / "launcher-test"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        if shutil.disk_usage(work).free < 41 * 1024 ** 3:
            self.skipTest("the launcher needs 40 GB free next to the bundle")
        try:
            bundle = write_bundle(work / "bundle", {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}},
                                  {"manifests/x.yml": MANIFEST})
            shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
            fake = work / "bin"
            fake.mkdir()
            for tool in ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "cp", "tr", "dirname", "printf", "mkdir", "rm", "tar", "date", "tee", "mv", "chmod", "cmp", "cksum"):
                found = shutil.which(tool)
                if found:
                    (fake / tool).symlink_to(found)
            log = work / "runtime.log"
            (fake / "docker").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$RUNTIME_LOG\"\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n", encoding="utf-8")
            (fake / "docker").chmod(0o755)
            env = {"PATH": str(fake), "HOME": str(work), "RUNTIME_LOG": str(log), "SYNOS_ENGINE_SOURCE": str(ROOT), "SYNOS_YES": "1",
                   "SYNOS_NO_UPDATE_CHECK": "1"}
            check = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, check.returncode, check.stderr + check.stdout)
            self.assertIn("built here at the first build", check.stdout)
            build = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, build.returncode, build.stderr + build.stdout)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(c.startswith("build --platform linux/amd64 --build-arg SUITE=resolute -t synos-builder:ubuntu-resolute-local -f bases/ubuntu/Containerfile .") for c in calls), calls)
            self.assertTrue(any(c.startswith("run ") and "synos-builder:ubuntu-resolute-local synos build /bundle" in c for c in calls), calls)
            self.assertIn("built here instead", build.stdout)
            self.assertTrue((bundle / "dist" / "image-build.log").is_file())
            # A stale launcher (a bundle downloaded before a fix) is replaced by the
            # engine's current one and the build starts again with it.
            stale = (ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8") + "\n# an older release of this script\n"
            (bundle / "build.sh").write_text(stale, encoding="utf-8")
            log.write_text("", encoding="utf-8")
            again = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, again.returncode, again.stderr + again.stdout)
            self.assertIn("was updated to the engine's current launcher", again.stdout)
            self.assertEqual((ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8"), (bundle / "build.sh").read_text(encoding="utf-8"))
            self.assertTrue(any(c.startswith("run ") for c in log.read_text(encoding="utf-8").splitlines()), "the build ran after the refresh")
            self.assertFalse((bundle / "dist" / "build.pid").exists(), "the pid file goes with the run")
            # Two builds of one bundle never run at once: while the first one's process
            # is alive the second explains and exits 2; once it is gone the build starts.
            other = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
            try:
                (bundle / "dist" / "build.pid").write_text(f"{other.pid}\n", encoding="utf-8")
                log.write_text("", encoding="utf-8")
                busy = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertEqual(2, busy.returncode, busy.stderr + busy.stdout)
                self.assertIn(f"already running (process {other.pid}", busy.stderr)
                self.assertFalse(any(c.startswith("run ") for c in log.read_text(encoding="utf-8").splitlines()), "no second build")
            finally:
                other.kill()
                other.wait()
            after = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, after.returncode, after.stderr + after.stdout)
            self.assertTrue(any(c.startswith("run ") for c in log.read_text(encoding="utf-8").splitlines()), "a stale pid file never blocks")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_inside_the_image_build_runs_make_directly_and_collects_outputs(self) -> None:
        cli = load_cli()
        calls: list[list[str]] = []

        class Result:
            returncode = 0

        def fake_run(step, **kwargs):
            calls.append(list(step))
            return Result()

        original_run, original_env = cli.subprocess.run, dict(os.environ)
        cli.subprocess.run = fake_run
        os.environ["SYNOS_IN_CONTAINER"] = "1"
        cli.sys.stderr = open(os.devnull, "w")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = cli.container_build(ROOT / "manifests" / "test-build.yml", None, True, None, Path(tmp) / "out")
                self.assertEqual([["make", "MANIFEST=manifests/test-build.yml", "PACKAGES_FLAGS=--strict"]], calls)
                self.assertEqual("local", result["engine"])
                self.assertIn("iso", result)
            host = cli.host_report()
            self.assertNotIn("podman or docker is required", " ".join(host["problems"]))
        finally:
            cli.subprocess.run = original_run
            cli.sys.stderr.close()
            cli.sys.stderr = sys.stderr
            os.environ.clear()
            os.environ.update(original_env)


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class ContainerStorageTests(unittest.TestCase):
    """SYNOS_CONTAINER_ROOT/SYNOS_CONTAINER_RUNROOT (and --container-root=/--container-runroot=):
    podman takes a per-build storage location with no root; docker's is
    daemon-wide and is refused plainly instead of silently ignored or built
    into a failure. Every runtime here is a fake that logs its argv and exits
    0; no real build is ever started."""

    TOOLS = ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "tr",
             "dirname", "printf", "stat", "mkdir")
    # A full (non-"check") build walks further than the storage checks: the
    # self-refresh probe, dist/ bookkeeping, the run itself.
    BUILD_TOOLS = TOOLS + ("cp", "chmod", "tar", "date", "tee", "rm", "mv")

    def make_fake_bin(self, tmp: Path, tools: tuple[str, ...] | None = None) -> Path:
        fake = tmp / "bin"
        fake.mkdir()
        for tool in (tools or self.TOOLS):
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
        return fake

    def write_root_id(self, fake: Path) -> None:
        """A fake `id` reporting uid/gid 0, so the podman branch (which runs
        through sudo for a real, non-root user) needs neither real root nor
        an interactive sudo prompt to exercise here."""
        real_id = shutil.which("id")
        (fake / "id").unlink()
        write_executable(fake / "id", "#!/bin/sh\ncase \"$1\" in\n  -u) echo 0 ;;\n  -g) echo 0 ;;\n"
                                       f"  *) exec '{real_id}' \"$@\" ;;\nesac\n")

    def write_logging_runtime(self, fake: Path, name: str, log: Path) -> None:
        """A fake podman/docker that records every invocation's argv, one
        line per call, and always succeeds."""
        write_executable(fake / name, f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")

    def write_docker_reporting_store(self, fake: Path, log: Path, store: Path) -> None:
        write_executable(fake / "docker", (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then\n"
            "    case \"$3\" in\n"
            f"        *DockerRootDir*) printf '%s\\n' '{store}' ;;\n"
            "        *) printf '\\n' ;;\n"
            "    esac\n"
            "fi\n"
            "exit 0\n"
        ))

    def write_podman_reporting_store(self, fake: Path, log: Path, store: Path) -> None:
        """A fake podman answering `info --format {{.Store.GraphRoot}}` with
        `store` (and recording every call); used for the storage question,
        which asks this before deciding whether there is a real question."""
        write_executable(fake / "podman", (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then\n"
            "    case \"$3\" in\n"
            f"        *GraphRoot*) printf '%s\\n' '{store}' ;;\n"
            "        *) printf '\\n' ;;\n"
            "    esac\n"
            "fi\n"
            "exit 0\n"
        ))

    def write_df(self, fake: Path, marker_path: str, marker_avail_kb: int, default_avail_kb: int = 100 * 1024 * 1024) -> None:
        """A fake `df -Pk PATH` that reports marker_avail_kb for marker_path
        and a large default for everything else (in particular ".", the
        bundle's own 40 GB check), so a test controls exactly one number
        without depending on the real disk this runs on."""
        (fake / "df").unlink()
        write_executable(fake / "df", (
            "#!/bin/sh\n"
            "path=$2\n"
            f"if [ \"$path\" = '{marker_path}' ]; then avail={marker_avail_kb}; else avail={default_avail_kb}; fi\n"
            "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            "printf '/dev/fake 1 1 %s 1%% %s\\n' \"$avail\" \"$path\"\n"
        ))

    def make_bundle(self, tmp: Path) -> Path:
        bundle = write_bundle(tmp / "bundle", {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}},
                              {"manifests/x.yml": MANIFEST})
        shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
        return bundle

    def test_podman_receives_root_and_runroot_docker_never_would(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            container_root = tmp / "storage"
            container_runroot = tmp / "runroot"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(container_root), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root), "SYNOS_CONTAINER_RUNROOT": str(container_runroot)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(calls, "podman must have been invoked")
            for call in calls:
                self.assertIn(f"--root {container_root} --runroot {container_runroot}", call, call)
            self.assertTrue(container_root.is_dir())
            self.assertTrue(container_runroot.is_dir())
            self.assertIn(f"--root {container_root} --runroot {container_runroot}", result.stdout)

    def test_an_unset_container_root_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(tmp / "never-matched"), 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(calls, "podman must have been invoked")
            for call in calls:
                self.assertNotIn("--root", call)
                self.assertNotIn("--runroot", call)
            self.assertNotIn("--root", result.stdout)

    def test_docker_refuses_a_requested_location_before_any_command_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            container_root = tmp / "storage"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "docker", log)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "docker must never be invoked")
            self.assertIn("daemon-wide location", result.stderr)
            self.assertIn("data-root", result.stderr)
            self.assertIn("/etc/docker/daemon.json", result.stderr)
            self.assertIn("/var/snap/docker/current/config/daemon.json", result.stderr)
            self.assertIn("snap connect docker:removable-media", result.stderr)
            self.assertIn("install podman", result.stderr)

    def test_podman_low_space_refusal_names_the_numbers_and_puts_the_rootless_option_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            container_root = tmp / "storage"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(container_root), 8 * 1024 * 1024)  # 8 GB, below the 30 GB threshold
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            message = result.stderr
            self.assertIn("needs 30 GB free", message)
            self.assertIn("only 8 GB is free", message)
            self.assertIn(str(container_root), message)
            self.assertIn("no root needed", message)
            self.assertLess(message.index("no root needed"), message.index("move podman's own default storage"),
                            "the option that needs no root must come first")
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_docker_low_space_refusal_names_the_numbers_and_both_daemon_json_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            store_dir = tmp / "docker-storage"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_docker_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)  # 5 GB, below the 30 GB threshold
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            message = result.stderr
            self.assertIn("needs 30 GB free", message)
            self.assertIn("only 5 GB is free", message)
            self.assertIn(str(store_dir), message)
            self.assertIn("install podman", message)
            self.assertIn("plain install", message)
            self.assertIn("/etc/docker/daemon.json", message)
            self.assertIn("snap install", message)
            self.assertIn("/var/snap/docker/current/config/daemon.json", message)
            self.assertIn("snap connect docker:removable-media", message)

    def test_prompt_wording_and_enter_accepts_the_offered_default(self) -> None:
        """The launcher proposes a location up front instead of only refusing
        after the fact: asked once, with the facts (where, how much free
        there, how much is needed), the obvious alternative offered as the
        default answer Enter accepts."""
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)  # 5 GB there; the bundle disk gets the 100 GB default
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            alt_dir = bundle / ".build" / "container-storage"
            output = run_under_pty(bundle, env, ["sh", "build.sh"], send_when_seen="[Y/n]", send=b"\n")
            self.assertIn(f"podman would keep this build under {store_dir} (5 GB free); it needs 30 GB.", output)
            self.assertIn(f"Use {alt_dir} next to this bundle instead (100 GB free there)? [Y/n]", output)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {alt_dir}" in c for c in calls), calls)
            self.assertEqual(str(alt_dir), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_declining_the_prompt_remembers_default_and_uses_no_root_flag(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            output = run_under_pty(bundle, env, ["sh", "build.sh"], send_when_seen="[Y/n]", send=b"n\n")
            self.assertIn("[Y/n]", output)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("--root" in c for c in calls), calls)
            self.assertEqual("default", (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_remembered_choice_is_reused_without_asking_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            remembered = tmp / "remembered-storage"
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(remembered), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("[Y/n]", result.stdout)
            self.assertIn(f"using the remembered build storage: {remembered}", result.stdout)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {remembered}" in c for c in calls), calls)

    def test_a_flag_overrides_the_remembered_value_and_updates_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            remembered = tmp / "old-storage"
            new_root = tmp / "new-storage"
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(new_root), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", f"--container-root={new_root}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {new_root}" in c for c in calls), calls)
            self.assertFalse(any(str(remembered) in c for c in calls), calls)
            self.assertEqual(str(new_root), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_no_prompt_without_a_terminal_even_when_storage_is_tight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertNotIn("[Y/n]", result.stdout + result.stderr)
            self.assertNotIn("podman would keep", result.stdout + result.stderr)
            # unchanged: the existing hard refusal still applies, exactly as before
            self.assertEqual(2, result.returncode)
            self.assertIn("this build needs 30 GB free", result.stderr)
            self.assertFalse((bundle / ".build" / "container-root").exists())

    def test_no_prompt_with_yes_even_though_storage_is_tight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertNotIn("[Y/n]", result.stdout + result.stderr)
            self.assertNotIn("podman would keep", result.stdout + result.stderr)
            self.assertEqual(2, result.returncode)
            self.assertIn("this build needs 30 GB free", result.stderr)

    def test_no_prompt_in_check_mode_even_with_a_terminal(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            # nothing ever prompts here, so wait on the line "check" always prints instead
            output = run_under_pty(bundle, env, ["sh", "build.sh", "check"], send_when_seen="GB free in", send=b"\n", timeout=8)
            self.assertNotIn("[Y/n]", output)
            self.assertNotIn("podman would keep", output)

    def test_no_prompt_when_the_default_storage_clearly_has_room(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 70 * 1024 * 1024)  # 70 GB: comfortably above the 60 GB "why ask" line
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            output = run_under_pty(bundle, env, ["sh", "build.sh"], send_when_seen="pulling the build engine", send=b"\n")
            self.assertNotIn("[Y/n]", output)
            self.assertNotIn("podman would keep", output)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("--root" in c for c in calls), calls)

    def test_a_location_on_a_filesystem_that_cannot_back_an_overlay_is_refused_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            # A fake `stat -f -c %T` reporting a filesystem podman cannot use for an overlay.
            (fake / "stat").unlink()
            write_executable(fake / "stat", "#!/bin/sh\necho vfat\n")
            container_root = tmp / "storage"
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("vfat", result.stderr)
            self.assertIn("cannot back a container's overlay filesystem", result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_launcher_script_documents_the_new_flags(self) -> None:
        text = (ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8")
        for needle in ("SYNOS_CONTAINER_ROOT", "SYNOS_CONTAINER_RUNROOT", "--container-root=", "--container-runroot="):
            self.assertIn(needle, text, needle)


LAUNCHER_UPDATE_TOOLS = ("sh", "sed", "head", "awk", "grep", "cat", "cp", "tr", "dirname", "printf",
                          "mkdir", "rm", "mv", "chmod", "cmp", "cksum", "curl", "wget", "stat", "ls", "uname")
FULL_BUILD_TOOLS = LAUNCHER_UPDATE_TOOLS + ("df", "id", "tar", "date", "tee")


def make_fake_path(directory: Path, tools: tuple[str, ...] = LAUNCHER_UPDATE_TOOLS) -> Path:
    """A PATH with only the given tools symlinked in - in particular, no docker
    or podman, so ./build.sh update is exercised without a container runtime."""
    directory.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        found = shutil.which(tool)
        if found:
            (directory / tool).symlink_to(found)
    return directory


class LauncherUpdateTests(unittest.TestCase):
    """./build.sh update (and .\\build.ps1 update): pull the four current
    launchers and replace the bundle's copies, without building."""

    LAUNCHERS = {"build.sh": "bundle_launcher.sh", "build.ps1": "bundle_launcher.ps1",
                 "build.cmd": "bundle_launcher.cmd", "build.command": "bundle_launcher.command"}

    def make_bundle(self, directory: Path) -> Path:
        (directory / "manifests").mkdir(parents=True, exist_ok=True)
        (directory / "manifests" / "x.yml").write_text(MANIFEST, encoding="utf-8")
        (directory / "bundle.json").write_text(
            json.dumps({"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}}), encoding="utf-8")
        for target, source in self.LAUNCHERS.items():
            (directory / target).write_bytes((ROOT / "tools" / source).read_bytes())
            os.chmod(directory / target, 0o755 if target in ("build.sh", "build.command") else 0o644)
        return directory

    def make_engine(self, directory: Path, mutate=lambda name, data: data) -> Path:
        """A pretend engine checkout: a tools/ folder with the four current
        launchers, each optionally mutated (a newer release, or a broken one)."""
        tools = directory / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        for source in self.LAUNCHERS.values():
            (tools / source).write_bytes(mutate(source, (ROOT / "tools" / source).read_bytes()))
        return directory

    def run_update(self, bundle: Path, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "build.sh", "update"], cwd=bundle, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)

    def test_update_from_engine_source_replaces_all_four_launchers(self) -> None:
        newer = lambda name, data: data + b"\n# a newer release\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_update(bundle, env)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for target in self.LAUNCHERS:
                self.assertIn(f"{target}: updated", result.stdout)
            for target, source in self.LAUNCHERS.items():
                self.assertEqual((engine / "tools" / source).read_bytes(), (bundle / target).read_bytes(), target)
            self.assertEqual(0o755, os.stat(bundle / "build.sh").st_mode & 0o777)
            self.assertEqual(0o755, os.stat(bundle / "build.command").st_mode & 0o777)
            self.assertIn(b"\r\n", (bundle / "build.cmd").read_bytes(), "the .cmd CRLF is kept")
            # a second run finds nothing to do
            again = self.run_update(bundle, env)
            self.assertEqual(0, again.returncode, again.stdout + again.stderr)
            for target in self.LAUNCHERS:
                self.assertIn(f"{target}: already current", again.stdout)
            self.assertIn("up to date", again.stdout)

    def test_update_via_a_file_url(self) -> None:
        if not (shutil.which("curl") or shutil.which("wget")):
            self.skipTest("neither curl nor wget is installed")
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.sh" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp),
                   "SYNOS_LAUNCHER_URL": f"file://{engine}/tools"}
            result = self.run_update(bundle, env)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for target, source in self.LAUNCHERS.items():
                self.assertEqual((engine / "tools" / source).read_bytes(), (bundle / target).read_bytes(), target)

    def test_update_refuses_an_html_source_and_changes_nothing(self) -> None:
        html = lambda name, data: b"<html><body>captive portal</body></html>" if name == "bundle_launcher.cmd" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", html)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            before = {target: (bundle / target).read_bytes() for target in self.LAUNCHERS}
            result = self.run_update(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("build.cmd", result.stderr)
            self.assertIn("HTML", result.stderr)
            for target in self.LAUNCHERS:
                self.assertEqual(before[target], (bundle / target).read_bytes(), f"{target} was changed")

    def test_update_with_an_unreachable_url_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            # loopback, closed port: refused immediately, never a real host
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp),
                   "SYNOS_LAUNCHER_URL": "http://127.0.0.1:1/tools"}
            before = {target: (bundle / target).read_bytes() for target in self.LAUNCHERS}
            result = self.run_update(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            for target in self.LAUNCHERS:
                self.assertEqual(before[target], (bundle / target).read_bytes(), f"{target} was changed")

    def test_update_needs_no_container_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine")
            fake = make_fake_path(tmp / "bin")
            self.assertIsNone(shutil.which("docker", path=str(fake)))
            self.assertIsNone(shutil.which("podman", path=str(fake)))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_update(bundle, env)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_ps1_update_is_handled_before_the_runtime_checks(self) -> None:
        text = (ROOT / "tools" / "bundle_launcher.ps1").read_text(encoding="utf-8")
        update_pos = text.index('$Command -eq "update"')
        runtime_pos = min(p for p in (text.find("docker", 200), text.find("podman", 200)) if p != -1)
        self.assertLess(update_pos, runtime_pos, "update must be handled before the first docker/podman reference")

    def test_ps1_update_via_pwsh(self) -> None:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not installed on this machine")
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.ps1" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = dict(os.environ, SYNOS_ENGINE_SOURCE=str(engine))
            result = subprocess.run([pwsh, "-NoProfile", "-File", "build.ps1", "update"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual((engine / "tools" / "bundle_launcher.ps1").read_bytes(), (bundle / "build.ps1").read_bytes())

    def test_update_ignores_a_cr_only_difference_in_build_cmd(self) -> None:
        """A bundle applied on a system that turns build.cmd's CRLF into LF
        (export_catalog.py used to do this with read_text) must not be
        reported as stale forever over the line endings alone."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine")  # byte-identical to the bundle, CRLF build.cmd
            lf_cmd = (bundle / "build.cmd").read_bytes().replace(b"\r\n", b"\n")
            self.assertNotEqual(lf_cmd, (bundle / "build.cmd").read_bytes(), "the fixture must actually differ by CR")
            (bundle / "build.cmd").write_bytes(lf_cmd)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_update(bundle, env)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("build.cmd: already current", result.stdout)
            self.assertNotIn("build.cmd: updated", result.stdout)
            # left exactly as it was: a CR-only "difference" is not a reason to touch it
            self.assertEqual(lf_cmd, (bundle / "build.cmd").read_bytes())

    def test_update_failure_message_lists_already_replaced_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            newer = lambda name, data: data + b"\n# a newer release\n"
            engine = self.make_engine(tmp / "engine", newer)
            fake = make_fake_path(tmp / "bin")
            real_chmod = shutil.which("chmod")
            (fake / "chmod").unlink()
            (fake / "chmod").write_text(
                "#!/bin/sh\ncase \"$*\" in\n  *build.cmd.new*) exit 1 ;;\n  *) exec '" + real_chmod + "' \"$@\" ;;\nesac\n",
                encoding="utf-8")
            (fake / "chmod").chmod(0o755)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_update(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("could not replace build.cmd", result.stderr)
            self.assertIn("already replaced: build.sh build.ps1", result.stderr)
            self.assertIn("run update again", result.stderr)
            self.assertEqual((engine / "tools" / "bundle_launcher.sh").read_bytes(), (bundle / "build.sh").read_bytes())
            self.assertEqual((engine / "tools" / "bundle_launcher.ps1").read_bytes(), (bundle / "build.ps1").read_bytes())
            self.assertNotEqual((engine / "tools" / "bundle_launcher.cmd").read_bytes(), (bundle / "build.cmd").read_bytes())


class LauncherUpdateCheckTests(LauncherUpdateTests):
    """The check for a newer launcher at the start of check/build (never for
    update itself): tools/bundle_launcher.sh (dynamic) and .ps1 (static, plus
    pwsh when installed). Every run here has no docker/podman on PATH and is
    read up to the point where the missing runtime stops it (exit 2), which
    is always after the check has had its say."""

    def run_check(self, bundle: Path, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)

    def test_newer_remote_with_yes_updates_and_restarts_once(self) -> None:
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.sh" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp),
                   "SYNOS_ENGINE_SOURCE": str(engine), "SYNOS_YES": "1"}
            result = self.run_check(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("podman or docker is required", result.stderr)
            self.assertEqual(1, result.stdout.count("the launchers were updated; starting again."),
                             "the update-and-restart happens exactly once, never a loop")
            self.assertEqual((engine / "tools" / "bundle_launcher.sh").read_bytes(), (bundle / "build.sh").read_bytes())

    def test_newer_remote_without_a_tty_leaves_files_unchanged(self) -> None:
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.sh" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            before = (bundle / "build.sh").read_bytes()
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_check(bundle, env)  # stdin is DEVNULL: never a tty
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("a newer launcher is available", result.stdout)
            self.assertIn("build.sh update", result.stdout)
            self.assertEqual(before, (bundle / "build.sh").read_bytes())

    def test_same_remote_prompts_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine")
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_check(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("a newer launcher is available", result.stdout)
            self.assertNotIn("Update now", result.stdout)

    def test_unreachable_remote_notes_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp),
                   "SYNOS_LAUNCHER_URL": "http://127.0.0.1:1/tools"}
            result = self.run_check(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("could not check for a launcher update", result.stdout)
            self.assertIn("continuing", result.stdout)

    def test_no_update_check_env_var_skips_the_fetch_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            # points at an address that would fail the check; if the check ran
            # anyway, it would report "could not check" below.
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp),
                   "SYNOS_LAUNCHER_URL": "http://127.0.0.1:1/tools", "SYNOS_NO_UPDATE_CHECK": "1"}
            result = self.run_check(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("could not check", result.stdout)

    def test_interactive_default_yes_prompt_via_pty(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.sh" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            output = run_under_pty(bundle, env, ["sh", "build.sh", "check"], send_when_seen="[Y/n]", send=b"\n")
            self.assertIn("[Y/n]", output)
            self.assertIn("the launchers were updated", output)
            self.assertEqual((engine / "tools" / "bundle_launcher.sh").read_bytes(), (bundle / "build.sh").read_bytes())

    def test_ps1_update_check_is_handled_before_the_runtime_checks(self) -> None:
        text = (ROOT / "tools" / "bundle_launcher.ps1").read_text(encoding="utf-8")
        check_pos = text.index("Test-ForLauncherUpdate")
        runtime_pos = min(p for p in (text.find("docker", 500), text.find("podman", 500)) if p != -1)
        self.assertLess(check_pos, runtime_pos, "the update check must run before the first docker/podman reference")

    def test_ps1_update_check_via_pwsh(self) -> None:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not installed on this machine")
        newer = lambda name, data: data + b"\n# a newer release\n" if name == "bundle_launcher.ps1" else data
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine", newer)
            env = dict(os.environ, SYNOS_ENGINE_SOURCE=str(engine), SYNOS_YES="1")
            result = subprocess.run([pwsh, "-NoProfile", "-File", "build.ps1", "check"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
            # no docker/podman on this machine either: the check must still have run and restarted first.
            self.assertIn("the launchers were updated", result.stdout, result.stdout + result.stderr)
            self.assertEqual((engine / "tools" / "bundle_launcher.ps1").read_bytes(), (bundle / "build.ps1").read_bytes())

    def test_check_ignores_a_cr_only_difference_in_build_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            engine = self.make_engine(tmp / "engine")  # byte-identical to the bundle, CRLF build.cmd
            lf_cmd = (bundle / "build.cmd").read_bytes().replace(b"\r\n", b"\n")
            self.assertNotEqual(lf_cmd, (bundle / "build.cmd").read_bytes(), "the fixture must actually differ by CR")
            (bundle / "build.cmd").write_bytes(lf_cmd)
            env = {"PATH": str(make_fake_path(tmp / "bin")), "HOME": str(tmp), "SYNOS_ENGINE_SOURCE": str(engine)}
            result = self.run_check(bundle, env)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)  # stops at the missing runtime
            self.assertNotIn("a newer launcher is available", result.stdout)
            self.assertNotIn("Update now", result.stdout)
            self.assertEqual(lf_cmd, (bundle / "build.cmd").read_bytes())

    def test_late_refresh_is_skipped_once_the_check_has_reached_its_source(self) -> None:
        """The check compares against SYNOS_LAUNCHER_URL/SYNOS_ENGINE_SOURCE; the
        late, mid-build self-refresh compares against the pulled image's own
        copy - a different source. When the check found the launchers already
        current, the late step must not still swap build.sh for an older
        image copy and set up an update-then-downgrade loop on the next run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp / "bundle")
            # the check's source: byte-identical to what the bundle already has
            engine = self.make_engine(tmp / "engine")
            # the "image"'s baked-in copy: older than both, and different
            older_sh = tmp / "older-build.sh"
            older_sh.write_bytes((ROOT / "tools" / "bundle_launcher.sh").read_bytes()
                                  + b"\n# an older release baked into the image\n")

            fake = make_fake_path(tmp / "bin", tools=FULL_BUILD_TOOLS)
            docker = fake / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  pull) exit 0 ;;\n"
                "  info) exit 0 ;;\n"
                "  run)\n"
                "    case \"$*\" in\n"
                "      *'cat /opt/synos/tools/bundle_launcher.sh'*) cat \"$OLDER_SH\" ;;\n"
                "      *) exit 0 ;;\n"
                "    esac\n"
                "    ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8")
            docker.chmod(0o755)

            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_LAUNCHER_URL": f"file://{engine}/tools",
                   "OLDER_SH": str(older_sh), "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("was updated to the engine's current launcher", result.stdout)
            self.assertEqual((ROOT / "tools" / "bundle_launcher.sh").read_bytes(), (bundle / "build.sh").read_bytes())


def run_under_pty(bundle: Path, env: dict, argv: list[str], send_when_seen: str, send: bytes, timeout: float = 15) -> str:
    """Runs argv under a pseudo-terminal (so [Console]::IsInputRedirected /
    `[ -t 0 ]` see a real tty), types `send` once `send_when_seen` appears,
    and returns everything the child printed."""
    import pty
    import select
    import time

    out = b""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(bundle)
        os.execvpe(argv[0], argv, env)
    else:
        needle = send_when_seen.encode()
        start = time.time()
        while needle not in out and time.time() - start < timeout:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                try:
                    out += os.read(fd, 4096)
                except OSError:
                    break
        try:
            os.write(fd, send)
        except OSError:
            pass
        start = time.time()
        while time.time() - start < timeout:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            elif not os.path.exists(f"/proc/{pid}"):
                break
        os.waitpid(pid, 0)
    return out.decode(errors="replace")
