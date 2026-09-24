"""The one place in this whole test suite that starts a real browser
against a real, built Studio site.

Every other test for tools/devtools_browser.py (tests/unit/test_devtools_browser.py)
drives a scripted fake in place of the websocket — good for the protocol
and the call sequence, but a fake cannot see a real Chrome behave
differently from the script. That is exactly what reached production
once: `localStorage.clear()` run before the first navigation raised a
SecurityError on `about:blank` (no fake ever visits `about:blank`), and
the Bundle Catalog page's own pagination left an entry's card missing
from the DOM (no fake ever paginates). This test would have failed loudly
on both.

Needs google-chrome or chromium on PATH, and a checkout of the Studio
repository to build a real site from — set SYNOS_STUDIO_REPO to one, or
have `../SynOS-Studio` or `../../SynOS-Studio` next to this checkout
(the layout this repository's own development environment already uses).
Skips cleanly, with a printed reason, when either is missing: a skip here
means this class of bug is not being caught, which is worth saying loudly
in the test output, never silently.

    SYNOS_STUDIO_REPO=/path/to/SynOS-Studio python3 -m unittest tests.unit.test_real_browser
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


db = load_module("devtools_browser_real", "tools/devtools_browser.py")


def find_studio_repo() -> Path | None:
    candidates = [
        os.environ.get("SYNOS_STUDIO_REPO"),
        str(ROOT.parent / "SynOS-Studio"),
        str(ROOT.parent.parent / "SynOS-Studio"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate) / "build.py").is_file():
            return Path(candidate)
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(db.find_browser(), "no google-chrome/chromium on PATH")
class RealBrowserTests(unittest.TestCase):
    studio_repo: Path | None = None
    site_dir: Path | None = None
    server: subprocess.Popen | None = None
    port: int = 0
    tmp: tempfile.TemporaryDirectory | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.studio_repo = find_studio_repo()
        if not cls.studio_repo:
            raise unittest.SkipTest(
                "no SynOS-Studio checkout found to build a real page from "
                "(set SYNOS_STUDIO_REPO, or check one out next to this repository)")

        cls.tmp = tempfile.TemporaryDirectory(prefix="synos-real-browser-test-")
        cls.site_dir = Path(cls.tmp.name) / "site"
        build = subprocess.run(
            [sys.executable, str(cls.studio_repo / "build.py"), "--repo", str(ROOT), "--output", str(cls.site_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if build.returncode != 0:
            cls.tmp.cleanup()
            cls.tmp = None
            raise unittest.SkipTest(f"could not build a real Studio site from {cls.studio_repo}: "
                                    f"{(build.stderr or build.stdout).strip()[:400]}")

        cls.port = free_port()
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(cls.port), "--bind", "127.0.0.1"],
            cwd=cls.site_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        up = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        if not up:
            cls._stop_server()
            cls.tmp.cleanup()
            cls.tmp = None
            raise unittest.SkipTest(f"the local site server never answered on 127.0.0.1:{cls.port}")

    @classmethod
    def _stop_server(cls) -> None:
        if cls.server is not None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait(timeout=10)
            cls.server = None

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_server()
        if cls.tmp is not None:
            cls.tmp.cleanup()
            cls.tmp = None

    def _site_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def test_downloads_a_real_entry_through_a_real_browser_and_it_is_a_valid_bundle(self) -> None:
        with db.StudioSession(self._site_url()) as session:
            raw, filename = session.download_bundle("web-server-nginx")

        self.assertTrue(filename.endswith("-bundle.tar.gz"), filename)
        self.assertGreater(len(raw), 1000, "a real bundle archive is more than a token handful of bytes")

        with tempfile.TemporaryDirectory() as tmp:
            extract_to = Path(tmp)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
                tar.extractall(extract_to, filter="data")
            names = {p.relative_to(extract_to).as_posix() for p in extract_to.rglob("*") if p.is_file()}

            self.assertIn("bundle.json", names)
            self.assertIn("build.sh", names)
            bundle_json = json.loads((extract_to / "bundle.json").read_text())
            self.assertEqual("web-server-nginx", bundle_json.get("name"))
            self.assertIn(bundle_json.get("manifest"), names)
            self.assertTrue(any(n.startswith("profiles/") for n in names))

    def test_a_catalog_entry_past_the_first_page_still_downloads(self) -> None:
        """Pins the pagination bug directly: an entry that is not among the
        Bundle Catalog page's first rendered cards must still be found and
        chosen, via the page's own catalog data rather than the DOM."""
        with db.StudioSession(self._site_url()) as session:
            session.navigate()
            session.wait_for(db.CATALOG_READY_EXPR)
            offered = session.eval(
                "(D && Array.isArray(D.bundle_catalog)) ? D.bundle_catalog.map(e => e.id) : []")
        self.assertIsInstance(offered, list)
        self.assertGreater(len(offered), 12, "this test needs a catalog with more than one page of entries")
        far_entry = offered[-1]  # deliberately not among the first page of cards

        with db.StudioSession(self._site_url()) as session:
            raw, filename = session.download_bundle(far_entry)
        self.assertGreater(len(raw), 1000)
        self.assertTrue(filename.startswith(far_entry))


if __name__ == "__main__":
    unittest.main()
