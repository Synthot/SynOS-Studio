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


CHANNEL_TOOLS = ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "cp", "tr",
                 "dirname", "printf", "mkdir", "rm", "tar", "date", "tee", "mv", "chmod", "cmp", "cksum", "sort")


def make_channel_bundle(tmp: Path, channel: str | None, engine_min: str = "0.2.0") -> Path:
    """A bundle whose bundle.json carries `channel` (omitted when None), for the
    stable/development resolution tests below."""
    descriptor = {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": engine_min}}
    if channel is not None:
        descriptor["channel"] = channel
    bundle = write_bundle(tmp / "bundle", descriptor, {"manifests/x.yml": MANIFEST})
    shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
    return bundle


def fake_df_always(fake: Path, avail_kb: int = 100 * 1024 * 1024) -> None:
    """A fake `df -Pk PATH` reporting avail_kb free for every path, so these
    tests never depend on the real disk of the machine they run on."""
    (fake / "df").unlink()
    write_executable(fake / "df", (
        "#!/bin/sh\n"
        f"printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
        f"printf '/dev/fake 1 1 {avail_kb} 1%% %s\\n' \"$2\"\n"
    ))


class StableChannelTests(unittest.TestCase):
    """docs/BUNDLE.md "Channels": the bundle's own bundle.json channel (default
    stable), resolved from GitHub releases, refusing rather than guessing, and
    never silently touching the development branch. Every runtime and network
    call here is a fake that logs its argv; no real build or fetch ever runs."""

    def make_fake(self, tmp: Path) -> Path:
        fake = tmp / "bin"
        fake.mkdir()
        for tool in CHANNEL_TOOLS:
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
        fake_df_always(fake)
        return fake

    def test_stable_is_the_default_and_never_fetches_the_branch_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            write_executable(fake / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n".format(log))
            curl_log = tmp / "curl.log"
            # Both the release-list API and any archive download fail (no real
            # network): this only has to prove which URL stable asks for.
            write_executable(fake / "curl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexit 1\n".format(curl_log))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("channel: stable (default; bundle.json names no channel)", result.stdout)
            calls = curl_log.read_text(encoding="utf-8").splitlines() if curl_log.exists() else []
            self.assertTrue(any("api.github.com/repos/Synthot/SynOS-Studio/tags" in c for c in calls), calls)
            self.assertTrue(any("archive/refs/tags/v0.2.0.tar.gz" in c for c in calls), calls)
            self.assertFalse(any("archive/refs/heads/main" in c for c in calls), "stable must never fetch the branch archive")

    def test_development_channel_from_bundle_json_falls_back_to_the_branch_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel="development", engine_min="0.2.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            write_executable(fake / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n".format(log))
            curl_log = tmp / "curl.log"
            write_executable(fake / "curl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexit 1\n".format(curl_log))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("channel: development (this bundle was made by a development instance of the page)", result.stdout)
            self.assertIn("note: the development channel builds against the unreleased tip", result.stdout)
            calls = curl_log.read_text(encoding="utf-8").splitlines() if curl_log.exists() else []
            self.assertTrue(any("archive/refs/heads/main.tar.gz" in c for c in calls), calls)
            self.assertFalse(any("api.github.com" in c for c in calls), "development never calls the release API")

    def test_an_override_wins_over_the_bundle_either_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            write_executable(fake / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n".format(log))
            write_executable(fake / "curl", "#!/bin/sh\nexit 1\n")
            env_base = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}

            bundle = make_channel_bundle(tmp, channel="development")
            result = subprocess.run(["sh", "build.sh", "--channel=stable"], cwd=bundle, env=env_base,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertIn("channel: stable (override: the --channel flag)", result.stdout)

            bundle2 = make_channel_bundle(tmp, channel="stable")
            env2 = dict(env_base, SYNOS_CHANNEL="development")
            result2 = subprocess.run(["sh", "build.sh"], cwd=bundle2, env=env2,
                                     capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertIn("channel: development (override: the SYNOS_CHANNEL environment variable)", result2.stdout)

    def test_refusal_when_no_release_satisfies_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="9.9.9")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            write_executable(fake / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n".format(log))
            tags_json = tmp / "tags.json"
            tags_json.write_text('[{"name": "v0.1.0"}, {"name": "v0.2.0"}]', encoding="utf-8")
            curl_script = (
                "#!/bin/sh\n"
                "out=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    if [ \"$prev\" = \"-o\" ]; then out=$a; fi\n"
                "    prev=$a\n"
                "done\n"
                "case \"$*\" in\n"
                f"    *'api.github.com/repos/Synthot/SynOS-Studio/tags'*) cp '{tags_json}' \"$out\" ;;\n"
                "    *) exit 1 ;;\n"
                "esac\n"
            )
            write_executable(fake / "curl", curl_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("this bundle needs engine 9.9.9; the newest release is 0.2.0.", result.stderr)

    def test_stale_pulled_image_is_detected_and_rebuilt_from_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            docker_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                "case \"$1\" in\n"
                "  pull) exit 0 ;;\n"
                "  run)\n"
                "    case \"$*\" in\n"
                "      *'cat /opt/synos/VERSION'*) printf '0.1.0\\n'; exit 0 ;;\n"
                "      *) exit 0 ;;\n"
                "    esac\n"
                "    ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            write_executable(fake / "docker", docker_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(ROOT)}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("carries engine 0.1.0, older than this bundle needs (0.2.0); rebuilding it from the matching source instead of using a stale image.",
                          result.stdout)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(c.startswith("build --platform") and "-t synos-builder:ubuntu-resolute-local" in c for c in calls), calls)

    def test_stale_explicit_builder_image_is_refused_not_silently_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            docker_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                "case \"$1\" in\n"
                "  run)\n"
                "    case \"$*\" in\n"
                "      *'cat /opt/synos/VERSION'*) printf '0.1.0\\n'; exit 0 ;;\n"
                "      *) exit 0 ;;\n"
                "    esac\n"
                "    ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            write_executable(fake / "docker", docker_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_BUILDER_IMAGE": "my-registry/synos-builder:old"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("SYNOS_BUILDER_IMAGE=my-registry/synos-builder:old carries engine 0.1.0, older than this bundle needs (0.2.0)", result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any(c.startswith("build ") for c in calls), "an explicit image is never silently replaced")

    def test_channel_and_engine_recorded_in_a_catalog_export(self) -> None:
        default = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                            capture_output=True, text=True, check=True).stdout)
        self.assertEqual("stable", default["channel"])
        dev = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py"), "--channel", "development"],
                                        capture_output=True, text=True, check=True).stdout)
        self.assertEqual("development", dev["channel"])
        self.assertEqual(default["engine"], dev["engine"])
        via_env = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                            capture_output=True, text=True, check=True,
                                            env=dict(os.environ, SYNOS_CHANNEL="development")).stdout)
        self.assertEqual("development", via_env["channel"])


ENGINE_CACHE_TOOLS = CHANNEL_TOOLS + ("gzip", "sha256sum", "tail", "cut")


def make_engine_archive_bytes(containerfile_body: bytes = b"FROM scratch\n") -> bytes:
    """A minimal, valid engine-checkout tarball: one top-level directory
    (stripped on extraction, so its name is never checked) holding just
    enough (bases/ubuntu/Containerfile) to pass build_engine_image()'s own
    sanity check."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("SynOS-Studio-x/VERSION", b"0.2.0\n"),
                           ("SynOS-Studio-x/bases/ubuntu/Containerfile", containerfile_body)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class EngineSourceCacheTests(unittest.TestCase):
    """items 175-178: the archive and its extraction are each reused rather
    than refetched/re-extracted whenever it is safe to, and the launcher
    says which it is doing. Every runtime and network call here is a fake
    that logs its argv; no real build, fetch or extraction of a real engine
    checkout ever runs (the tarball fixtures are synthetic and tiny)."""

    def make_fake(self, tmp: Path) -> Path:
        fake = tmp / "bin"
        fake.mkdir()
        for tool in ENGINE_CACHE_TOOLS:
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
        fake_df_always(fake)
        return fake

    def write_docker_never_has_the_published_image(self, fake: Path, log: Path, image_inspect_hits: tuple[str, ...] = ()) -> None:
        hits = " ".join(image_inspect_hits)
        write_executable(fake / "docker", (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "case \"$1 $2\" in\n"
            "  'image inspect')\n"
            f"    for hit in {hits or '__none__'}; do case \"$*\" in *\"$hit\"*) exit 0 ;; esac; done\n"
            "    exit 1 ;;\n"
            "  *) case \"$1\" in pull) exit 1 ;; *) exit 0 ;; esac ;;\n"
            "esac\n"
        ))

    def test_stable_channel_image_already_built_needs_no_archive_download(self) -> None:
        """Item 175: on stable, the release tag is the identity, known
        before any download; when an image already carries that tag, the
        archive is never fetched - only the release lookup itself, already
        needed to name the tag in the first place, touches the network."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            self.write_docker_never_has_the_published_image(fake, log, ("ubuntu-resolute-v0.2.0",))
            tags_json = tmp / "tags.json"
            tags_json.write_text('[{"name": "v0.1.0"}, {"name": "v0.2.0"}]', encoding="utf-8")
            curl_log = tmp / "curl.log"
            curl_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{curl_log}'\n"
                "out=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    if [ \"$prev\" = \"-o\" ]; then out=$a; fi\n"
                "    prev=$a\n"
                "done\n"
                "case \"$*\" in\n"
                f"    *'api.github.com/repos/Synthot/SynOS-Studio/tags'*) cp '{tags_json}' \"$out\" ;;\n"
                "    *'archive/refs/tags'*) echo 'must not be fetched: the image already exists' >&2; exit 1 ;;\n"
                "    *) exit 1 ;;\n"
                "esac\n"
            )
            write_executable(fake / "curl", curl_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("using the engine image already built for v0.2.0: synos-builder:ubuntu-resolute-v0.2.0", result.stdout)
            calls = curl_log.read_text(encoding="utf-8").splitlines() if curl_log.exists() else []
            self.assertTrue(any("tags" in c for c in calls), calls)
            self.assertFalse(any("archive/refs/tags" in c for c in calls),
                             "the archive must never be fetched once the image for its tag already exists")
            self.assertFalse((bundle / ".build" / "engine-src.tar.gz").exists())

    def test_development_archive_unchanged_upstream_is_reused_not_redownloaded(self) -> None:
        """Item 176/178: an unchanged development archive costs a 304, not
        45 MB - and the launcher says a reuse happened, not a download."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel="development", engine_min="0.1.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            self.write_docker_never_has_the_published_image(fake, log)
            archive_bytes = make_engine_archive_bytes()
            archive_file = tmp / "archive.tar.gz"
            archive_file.write_bytes(archive_bytes)
            curl_log = tmp / "curl.log"
            etag = '"fixed-etag"'
            curl_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{curl_log}'\n"
                "out=\"\"; headerfile=\"\"; inm=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    case \"$prev\" in\n"
                "        -o) out=$a ;;\n"
                "        -D) headerfile=$a ;;\n"
                f"        -H) case \"$a\" in If-None-Match:*) inm=$(printf '%s' \"$a\" | sed 's/^If-None-Match: *//') ;; esac ;;\n"
                "    esac\n"
                "    prev=$a\n"
                "done\n"
                f"if [ \"$inm\" = '{etag}' ]; then\n"
                "    [ -n \"$headerfile\" ] && : > \"$headerfile\"\n"
                "    printf '304'\n"
                "    exit 0\n"
                "fi\n"
                f"[ -n \"$headerfile\" ] && printf 'ETag: {etag}\\r\\n' > \"$headerfile\"\n"
                f"[ -n \"$out\" ] && cp '{archive_file}' \"$out\"\n"
                "printf '200'\n"
            )
            write_executable(fake / "curl", curl_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}

            first = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            self.assertIn("downloading the engine source from", first.stdout)
            source_id = hashlib.sha256(archive_bytes).hexdigest()[:12]
            extracted = bundle / ".build" / "engine-src" / source_id
            marker = bundle / ".build" / "engine-src" / f"{source_id}.ok"
            self.assertTrue(marker.exists(), "a completed extraction leaves its marker behind")
            first_marker_mtime = marker.stat().st_mtime_ns

            curl_log.write_text("", encoding="utf-8")
            second = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertNotIn("downloading the engine source from", second.stdout)
            self.assertIn("the development engine source is unchanged upstream; reusing", second.stdout)
            self.assertIn(f"reusing the engine source already extracted for {source_id}: ", second.stdout)
            self.assertEqual(first_marker_mtime, marker.stat().st_mtime_ns, "a reused extraction is never touched again")
            calls = curl_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("If-None-Match" in c for c in calls), calls)

    def test_development_archive_changed_upstream_downloads_and_extracts_fresh(self) -> None:
        """Item 176/177: when the server answers with new content (a real
        200, not a 304), a fresh download and a new content-addressed
        extraction happen - the old one is left alone, not reused, and not
        deleted either, since it may still be exactly what an old cached
        image was built from."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel="development", engine_min="0.1.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            self.write_docker_never_has_the_published_image(fake, log)
            old_bytes = make_engine_archive_bytes(b"FROM scratch\n# v1\n")
            new_bytes = make_engine_archive_bytes(b"FROM scratch\n# v2\n")
            state_dir = tmp / "state"
            state_dir.mkdir()
            (state_dir / "archive").write_bytes(old_bytes)
            (state_dir / "etag").write_text('"etag-v1"', encoding="utf-8")
            curl_log = tmp / "curl.log"
            curl_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{curl_log}'\n"
                "out=\"\"; headerfile=\"\"; inm=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    case \"$prev\" in\n"
                "        -o) out=$a ;;\n"
                "        -D) headerfile=$a ;;\n"
                f"        -H) case \"$a\" in If-None-Match:*) inm=$(printf '%s' \"$a\" | sed 's/^If-None-Match: *//') ;; esac ;;\n"
                "    esac\n"
                "    prev=$a\n"
                "done\n"
                f"current_etag=$(cat '{state_dir}/etag')\n"
                "if [ \"$inm\" = \"$current_etag\" ]; then\n"
                "    [ -n \"$headerfile\" ] && : > \"$headerfile\"\n"
                "    printf '304'\n"
                "    exit 0\n"
                "fi\n"
                f"[ -n \"$headerfile\" ] && printf 'ETag: %s\\r\\n' \"$current_etag\" > \"$headerfile\"\n"
                f"[ -n \"$out\" ] && cp '{state_dir}/archive' \"$out\"\n"
                "printf '200'\n"
            )
            write_executable(fake / "curl", curl_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}

            first = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            old_id = hashlib.sha256(old_bytes).hexdigest()[:12]
            old_marker = bundle / ".build" / "engine-src" / f"{old_id}.ok"
            self.assertTrue(old_marker.exists())

            # the server now has new content under a new ETag - simulating
            # main having actually moved
            (state_dir / "archive").write_bytes(new_bytes)
            (state_dir / "etag").write_text('"etag-v2"', encoding="utf-8")
            second = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertIn("downloaded a changed engine source from", second.stdout)
            new_id = hashlib.sha256(new_bytes).hexdigest()[:12]
            new_marker = bundle / ".build" / "engine-src" / f"{new_id}.ok"
            self.assertTrue(new_marker.exists(), "the changed content gets its own, new content-addressed directory")
            self.assertNotEqual(old_id, new_id)
            self.assertTrue(old_marker.exists(), "the old extraction is left in place, not deleted")

    def test_extracted_tree_missing_its_completion_marker_is_cleaned_and_reextracted(self) -> None:
        """Item 177: a tree without its own completion marker is never
        trusted, even when the archive it would come from is itself
        reused unchanged (a 304) - it is removed and extracted again."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel="development", engine_min="0.1.0")
            fake = self.make_fake(tmp)
            log = tmp / "runtime.log"
            self.write_docker_never_has_the_published_image(fake, log)
            archive_bytes = make_engine_archive_bytes()
            archive_file = tmp / "archive.tar.gz"
            archive_file.write_bytes(archive_bytes)
            curl_log = tmp / "curl.log"
            etag = '"fixed-etag"'
            curl_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{curl_log}'\n"
                "out=\"\"; headerfile=\"\"; inm=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    case \"$prev\" in\n"
                "        -o) out=$a ;;\n"
                "        -D) headerfile=$a ;;\n"
                f"        -H) case \"$a\" in If-None-Match:*) inm=$(printf '%s' \"$a\" | sed 's/^If-None-Match: *//') ;; esac ;;\n"
                "    esac\n"
                "    prev=$a\n"
                "done\n"
                f"if [ \"$inm\" = '{etag}' ]; then\n"
                "    [ -n \"$headerfile\" ] && : > \"$headerfile\"\n"
                "    printf '304'\n"
                "    exit 0\n"
                "fi\n"
                f"[ -n \"$headerfile\" ] && printf 'ETag: {etag}\\r\\n' > \"$headerfile\"\n"
                f"[ -n \"$out\" ] && cp '{archive_file}' \"$out\"\n"
                "printf '200'\n"
            )
            write_executable(fake / "curl", curl_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}

            first = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            source_id = hashlib.sha256(archive_bytes).hexdigest()[:12]
            marker = bundle / ".build" / "engine-src" / f"{source_id}.ok"
            extracted = bundle / ".build" / "engine-src" / source_id
            self.assertTrue(marker.exists())
            marker.unlink()  # simulate an earlier run interrupted mid-extraction

            second = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, second.returncode, second.stdout + second.stderr)
            self.assertNotIn(f"reusing the engine source already extracted for {source_id}", second.stdout,
                             "a tree with no marker is never reused as-is")
            self.assertTrue(marker.exists(), "re-extraction recreates the marker")
            self.assertTrue((extracted / "bases" / "ubuntu" / "Containerfile").exists())


class SudoEnvironmentTests(unittest.TestCase):
    """The launcher's documented variables must reach an elevated build rather
    than being silently dropped by sudo's own environment reset (docs/BUNDLE.md
    "Channels"). The fake sudo below mimics real sudo's own semantics for
    `sudo VAR=value cmd` (export each leading NAME=value token, then exec)."""

    def test_signing_key_and_engine_source_survive_sudo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = tmp / "bin"
            fake.mkdir()
            for tool in CHANNEL_TOOLS:
                found = shutil.which(tool)
                if found:
                    (fake / tool).symlink_to(found)
            fake_df_always(fake)
            real_id = shutil.which("id")
            (fake / "id").unlink()
            write_executable(fake / "id", (
                "#!/bin/sh\ncase \"$1\" in\n  -u) echo 1000 ;;\n  -g) echo 1000 ;;\n"
                f"  *) exec '{real_id}' \"$@\" ;;\nesac\n"
            ))
            write_executable(fake / "sudo", (
                "#!/bin/sh\n"
                "if [ \"$1\" = -v ]; then exit 0; fi\n"
                "while [ $# -gt 0 ]; do\n"
                "    case \"$1\" in\n"
                "        *=*) export \"$1\"; shift ;;\n"
                "        --) shift; break ;;\n"
                "        *) break ;;\n"
                "    esac\n"
                "done\n"
                "exec \"$@\"\n"
            ))
            log = tmp / "runtime.log"
            marker = tmp / "marker"
            # $1 is no longer necessarily the subcommand: the automatic
            # bundle-disk default (item 141) puts --root before it whenever
            # no location was given, as here. Matched on "$*" instead, so
            # this keeps working regardless of where --root lands.
            podman_script = (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                "case \"$*\" in\n"
                f"  *'synos build /bundle'*) printf '%s\\n' \"$SYNOS_SIGNING_KEY\" > '{marker}'; printf '%s\\n' \"$SYNOS_ENGINE_SOURCE\" >> '{marker}' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            write_executable(fake / "podman", podman_script)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_SIGNING_KEY": "top-secret-signing-key", "SYNOS_ENGINE_SOURCE": str(ROOT)}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("will run through sudo", result.stdout)
            lines = marker.read_text(encoding="utf-8").splitlines()
            self.assertEqual(["top-secret-signing-key", str(ROOT)], lines,
                             "SYNOS_SIGNING_KEY and SYNOS_ENGINE_SOURCE must reach the elevated command unchanged")

    def test_running_the_whole_launcher_under_sudo_is_explained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = make_channel_bundle(tmp, channel=None, engine_min="0.2.0")
            fake = tmp / "bin"
            fake.mkdir()
            for tool in CHANNEL_TOOLS:
                found = shutil.which(tool)
                if found:
                    (fake / tool).symlink_to(found)
            fake_df_always(fake)
            real_id = shutil.which("id")
            (fake / "id").unlink()
            write_executable(fake / "id", (
                "#!/bin/sh\ncase \"$1\" in\n  -u) echo 0 ;;\n  -g) echo 0 ;;\n"
                f"  *) exec '{real_id}' \"$@\" ;;\nesac\n"
            ))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SUDO_USER": "person"}
            # no runtime on PATH: this only needs to reach the sudo-detection note before failing
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("already running as root through sudo", result.stdout)
            self.assertIn("sudo SYNOS_VAR=value ./build.sh", result.stdout)


LAUNCHER_UPDATE_TOOLS =("sh", "sed", "head", "awk", "grep", "cat", "cp", "tr", "dirname", "printf",
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
