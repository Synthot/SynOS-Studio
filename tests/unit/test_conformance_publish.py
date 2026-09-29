"""catalog_conformance's publishing half: the rehearsal on a local copy of the
site before production, and the exit code a failed publish earns.

Split out of test_catalog_conformance.py to keep both files under the
reviewable line-count cap tests/unit/test_architecture.py enforces; it shares
that module's fixtures (the loaded tool, the real catalogue, the fake runners)
rather than duplicating them, the same way test_bundle_storage.py shares
test_bundle.py's.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_catalog_conformance import (  # shared fixtures, not duplicated
    ROOT, cc, real_catalog, entry_by_id, make_bundle_archive,
    FakeSession, FAKE_BUILD_SH_SUCCESS, FAKE_BUILD_SH_ENGINE_FAIL,
)


class PublishExitCodeTests(unittest.TestCase):
    """item 198: cc._run()'s own exit code distinguishes "a catalogue entry
    itself failed" (1, the real gate, always wins) from "every entry built
    and tested clean but the publish never reached somewhere it was
    configured to" (3, new) -- so a systemd timer's own result is honest
    even when every entry passed. Real cc._run() throughout (never
    run_build() directly), since the exit code is _run()'s own logic, not
    run_build()'s. cc.fetch_catalog and cc.devtools_browser.StudioSession
    are monkeypatched, same convention EndToEndIsolationTests above uses;
    the "local" destination is always real (a real temp-dir file), the
    "production" one always goes through an injected fake transport --
    never a real network connection."""

    ENTRY_ID = "web-server-nginx"

    def _run(self, tmp: str, *, entry_fails: bool = False, destinations_yaml: str = "",
            upload_transport=None, upload_verify_fetcher=None) -> tuple[int, dict]:
        catalog = real_catalog()
        fake_catalog = {"bundle_catalog": [entry_by_id(catalog, self.ENTRY_ID)]}
        raw = make_bundle_archive(entry_id=self.ENTRY_ID,
                                  build_sh=FAKE_BUILD_SH_ENGINE_FAIL if entry_fails else None)
        session = FakeSession(default_archive=raw)
        original_fetch = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        original_session = cc.devtools_browser.StudioSession
        cc.devtools_browser.StudioSession = lambda *a, **k: session
        # Same isolation EndToEndIsolationTests above uses: no real
        # podman/docker query for the end-of-run cache-volume reclaim step
        # (item 91) -- without this, each test here pays for a handful of
        # real, slow container-runtime subprocess calls for no reason.
        original_container_store = cc.host_resources.container_store
        cc.host_resources.container_store = lambda engine, container_root=None: None
        try:
            config_path = Path(tmp) / "conformance.yml"
            config_path.write_text(
                "catalog_url: http://x\n"
                f"workdir: {tmp}/work\n"
                "site_url: http://dev-site.example\n"
                "smoke: false\n"
                f"{destinations_yaml}\n",
                encoding="utf-8")
            config = cc.Config.load(config_path)
            code = cc._run("build", config, only=[self.ENTRY_ID],
                           upload_transport=upload_transport, upload_verify_fetcher=upload_verify_fetcher)
        finally:
            cc.fetch_catalog = original_fetch
            cc.devtools_browser.StudioSession = original_session
            cc.host_resources.container_store = original_container_store
        report = json.loads((Path(tmp) / "work" / "build-report.json").read_text(encoding="utf-8"))
        return code, report

    def test_entries_pass_but_local_rehearsal_fails_exits_publish_failed_and_skips_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")  # a file where a directory needs to be: mkdir fails cleanly
            prod_calls: list = []
            destinations_yaml = (
                "upload:\n"
                "  - name: dev-site\n"
                "    protocol: local\n"
                "    retries: 1\n"  # no real backoff sleep for a destination this test expects to fail every time
                f"    remote_path: {blocker}/sub/build-status.json\n"
                "  - name: production\n"
                "    protocol: sftp\n"
                "    host: prod.example\n"
                "    remote_path: /remote/build-status.json\n"
            )
            code, report = self._run(tmp, destinations_yaml=destinations_yaml,
                                     upload_transport=lambda c, p: prod_calls.append(c.protocol))
        self.assertEqual(cc.EXIT_PUBLISH_FAILED, code)
        self.assertEqual(3, code)
        self.assertEqual("success", report["targets"][self.ENTRY_ID]["status"])
        self.assertEqual([], prod_calls, "production must never be touched when the rehearsal publish failed")
        by_name = {o["name"]: o for o in report["publish_outcomes"]}
        self.assertFalse(by_name["dev-site"]["ok"])
        self.assertTrue(by_name["production"]["skipped"])

    def test_entries_pass_rehearsal_passes_but_production_fails_exits_publish_failed_and_says_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dest = Path(tmp) / "dev-site" / "build-status.json"
            destinations_yaml = (
                "upload:\n"
                "  - name: dev-site\n"
                "    protocol: local\n"
                f"    remote_path: {local_dest}\n"
                "  - name: production\n"
                "    protocol: sftp\n"
                "    host: prod.example\n"
                "    retries: 1\n"
                "    remote_path: /remote/build-status.json\n"
            )

            def transport(config, path):
                if config.protocol == "sftp":
                    raise RuntimeError("network unreachable")

            code, report = self._run(
                tmp, destinations_yaml=destinations_yaml, upload_transport=transport,
                # item 201: verify_published() now compares bytes, so a real
                # rehearsal pass needs the fetch-back to actually return what
                # was just published -- reading local_dest back, the same
                # file the "local" destination just wrote, is what a real
                # HTTP GET of the served copy would return on a correctly
                # configured host.
                upload_verify_fetcher=lambda url, timeout: local_dest.read_bytes())
            self.assertEqual(cc.EXIT_PUBLISH_FAILED, code)
            self.assertTrue(local_dest.is_file())  # the rehearsal itself genuinely passed
            by_name = {o["name"]: o for o in report["publish_outcomes"]}
            self.assertTrue(by_name["dev-site"]["ok"])
            self.assertFalse(by_name["production"]["ok"])
            self.assertFalse(by_name["production"]["skipped"])  # attempted for real, not skipped -- and failed
            self.assertIn("production failed", cc.publish_summary_line(report))

    def test_a_fully_clean_run_with_publishing_configured_still_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dest = Path(tmp) / "dev-site" / "build-status.json"
            destinations_yaml = (
                "upload:\n"
                "  - name: dev-site\n"
                "    protocol: local\n"
                f"    remote_path: {local_dest}\n"
                "  - name: production\n"
                "    protocol: sftp\n"
                "    host: prod.example\n"
                "    remote_path: /remote/build-status.json\n"
            )
            code, report = self._run(
                tmp, destinations_yaml=destinations_yaml, upload_transport=lambda c, p: None,
                upload_verify_fetcher=lambda url, timeout: local_dest.read_bytes())
        self.assertEqual(cc.EXIT_OK, code)
        self.assertEqual(0, code)
        self.assertTrue(all(o["ok"] for o in report["publish_outcomes"]))

    def test_a_fully_clean_run_with_no_publishing_configured_at_all_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, report = self._run(tmp)
        self.assertEqual(cc.EXIT_OK, code)
        self.assertNotIn("publish_outcomes", report)

    def test_a_failed_entry_always_wins_over_publishing_even_when_publishing_also_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            destinations_yaml = (
                "upload:\n"
                "  - name: production\n"
                "    protocol: sftp\n"
                "    host: prod.example\n"
                "    retries: 1\n"
                "    remote_path: /remote/build-status.json\n"
            )

            def always_fails(config, path):
                raise RuntimeError("network unreachable")

            code, report = self._run(tmp, entry_fails=True, destinations_yaml=destinations_yaml,
                                     upload_transport=always_fails)
        self.assertEqual(cc.EXIT_ENTRIES_FAILED, code)
        self.assertEqual(1, code)
        self.assertNotIn(report["targets"][self.ENTRY_ID]["status"], cc.OK_STATUSES)




if __name__ == "__main__":
    unittest.main()
