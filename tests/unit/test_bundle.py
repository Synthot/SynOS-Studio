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
            env = {"PATH": str(fake), "HOME": tmp}
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
            for tool in ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "tr", "dirname", "printf", "mkdir", "rm", "tar", "date", "tee", "mv", "chmod"):
                found = shutil.which(tool)
                if found:
                    (fake / tool).symlink_to(found)
            log = work / "runtime.log"
            (fake / "docker").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$RUNTIME_LOG\"\ncase \"$1\" in pull|image) exit 1;; *) exit 0;; esac\n", encoding="utf-8")
            (fake / "docker").chmod(0o755)
            env = {"PATH": str(fake), "HOME": str(work), "RUNTIME_LOG": str(log), "SYNOS_ENGINE_SOURCE": str(ROOT), "SYNOS_YES": "1"}
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
