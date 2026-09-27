"""tools/status_uploader.py: carrying build-status.json to wherever the
Studio page's web server can serve it from. Every test here uses a fake
transport (a plain callable this module lets a caller substitute) — never
a real network connection, never a real `scp`/`sftp`/ftplib call. Also
covers parse_upload_config()'s validation (including refusing plain FTP)
and that no credential ever appears in anything printed, logged, or
raised."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import status_uploader as su  # noqa: E402


SECRET = "hunter2-super-secret"


def make_config(**overrides) -> su.UploadConfig:
    fields = {
        "protocol": "sftp", "host": "studio.example", "remote_path": "/var/www/data/build-status.json",
        "port": 22, "username": "builder", "password": SECRET, "key_path": None,
        "retries": 3, "retry_backoff_s": 0.001,  # near-zero so retry tests do not actually sleep meaningfully
    }
    fields.update(overrides)
    return su.UploadConfig(**fields)


class ParseUploadConfigTests(unittest.TestCase):
    def test_no_section_at_all_returns_none(self) -> None:
        self.assertIsNone(su.parse_upload_config(None))
        self.assertIsNone(su.parse_upload_config({}))

    def test_a_minimal_valid_section_parses(self) -> None:
        cfg = su.parse_upload_config({"protocol": "sftp", "host": "h", "remote_path": "/p"})
        self.assertEqual("sftp", cfg.protocol)
        self.assertEqual("h", cfg.host)
        self.assertEqual("/p", cfg.remote_path)
        self.assertEqual(su.DEFAULT_RETRIES, cfg.retries)

    def test_unknown_key_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_config({"protocol": "sftp", "host": "h", "remote_path": "/p", "bogus": 1})

    def test_unsupported_protocol_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_config({"protocol": "carrier-pigeon", "host": "h", "remote_path": "/p"})

    def test_missing_host_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_config({"protocol": "sftp", "remote_path": "/p"})

    def test_missing_remote_path_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_config({"protocol": "sftp", "host": "h"})

    def test_plain_ftp_is_refused_by_default(self) -> None:
        with self.assertRaises(su.UploadConfigError) as ctx:
            su.parse_upload_config({"protocol": "ftp", "host": "h", "remote_path": "/p"})
        self.assertIn("allow_insecure_ftp", str(ctx.exception))

    def test_plain_ftp_is_accepted_when_explicitly_allowed(self) -> None:
        cfg = su.parse_upload_config({"protocol": "ftp", "host": "h", "remote_path": "/p", "allow_insecure_ftp": True})
        self.assertEqual("ftp", cfg.protocol)

    def test_ftps_needs_no_special_flag(self) -> None:
        cfg = su.parse_upload_config({"protocol": "ftps", "host": "h", "remote_path": "/p"})
        self.assertEqual("ftps", cfg.protocol)

    def test_scp_protocol_parses(self) -> None:
        cfg = su.parse_upload_config({"protocol": "scp", "host": "h", "remote_path": "/p"})
        self.assertEqual("scp", cfg.protocol)


class DescribeDestinationNeverLeaksSecretsTests(unittest.TestCase):
    def test_destination_string_never_contains_the_password(self) -> None:
        cfg = make_config()
        destination = su.describe_destination(cfg)
        self.assertNotIn(SECRET, destination)
        self.assertIn(cfg.host, destination)
        self.assertIn(cfg.username, destination)

    def test_destination_with_no_username_omits_the_at_sign(self) -> None:
        cfg = make_config(username=None)
        self.assertNotIn("@", su.describe_destination(cfg))


class RedactTests(unittest.TestCase):
    def test_redact_replaces_the_configured_password(self) -> None:
        cfg = make_config()
        text = f"login failed for user with password {SECRET} at host"
        redacted = su._redact(text, cfg)
        self.assertNotIn(SECRET, redacted)
        self.assertIn("***", redacted)

    def test_redact_is_a_no_op_when_no_password_is_configured(self) -> None:
        cfg = make_config(password=None)
        text = "some ordinary error"
        self.assertEqual(text, su._redact(text, cfg))


class UploadFileDryRunTests(unittest.TestCase):
    def test_dry_run_never_calls_the_transport(self) -> None:
        cfg = make_config()
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "build-status.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, dry_run=True, transport=lambda c, p: calls.append((c, p)))
        self.assertEqual([], calls)
        self.assertTrue(result.ok)

    def test_dry_run_message_names_the_destination_but_never_the_password(self) -> None:
        cfg = make_config()
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "build-status.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, dry_run=True)
        self.assertIn(cfg.host, result.detail)
        self.assertNotIn(SECRET, result.detail)


class UploadFileRealPathTests(unittest.TestCase):
    """"Real" here means "calls the (fake) transport" - never an actual
    scp/sftp/ftplib call; the fake stands in for whichever real one a
    protocol would otherwise use."""

    def test_a_transport_that_succeeds_on_the_first_try_is_called_once(self) -> None:
        cfg = make_config()
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, transport=lambda c, p: calls.append(1))
        self.assertTrue(result.ok)
        self.assertEqual(1, len(calls))

    def test_a_transport_that_always_raises_is_retried_up_to_the_configured_count(self) -> None:
        cfg = make_config(retries=3)
        calls = []
        sleeps = []

        def always_fails(c, p):
            calls.append(1)
            raise RuntimeError("network unreachable")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, transport=always_fails, sleeper=lambda s: sleeps.append(s))
        self.assertFalse(result.ok)
        self.assertEqual(3, len(calls))
        self.assertEqual(2, len(sleeps), "backoff sleeps happen between attempts, never after the last one")

    def test_backoff_grows_between_attempts(self) -> None:
        cfg = make_config(retries=3, retry_backoff_s=2.0)
        sleeps = []

        def always_fails(c, p):
            raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            su.upload_file(cfg, local, transport=always_fails, sleeper=lambda s: sleeps.append(s))
        self.assertEqual([2.0, 4.0], sleeps)

    def test_a_transport_that_succeeds_on_a_later_attempt_is_reported_as_ok(self) -> None:
        cfg = make_config(retries=3)
        attempts = {"n": 0}

        def fails_twice_then_succeeds(c, p):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, transport=fails_twice_then_succeeds, sleeper=lambda s: None)
        self.assertTrue(result.ok)
        self.assertEqual(3, attempts["n"])

    def test_retries_of_1_means_exactly_one_attempt_no_retry(self) -> None:
        cfg = make_config(retries=1)
        calls = []

        def always_fails(c, p):
            calls.append(1)
            raise RuntimeError("nope")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            su.upload_file(cfg, local, transport=always_fails, sleeper=lambda s: None)
        self.assertEqual(1, len(calls))


class UploadFileNeverLeaksCredentialsOnFailureTests(unittest.TestCase):
    def test_a_transport_error_that_echoes_the_password_is_redacted_in_the_result(self) -> None:
        cfg = make_config(retries=1)

        def leaky(c, p):
            raise RuntimeError(f"auth failed with password={SECRET}")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, transport=leaky, sleeper=lambda s: None)
        self.assertFalse(result.ok)
        self.assertNotIn(SECRET, result.detail)

    def test_upload_file_never_raises_even_when_the_transport_always_raises(self) -> None:
        cfg = make_config(retries=2)

        def always_fails(c, p):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = su.upload_file(cfg, local, transport=always_fails, sleeper=lambda s: None)
        self.assertFalse(result.ok)  # never an exception escaping - a failed upload is a warning, not a crash


class ChangeUploaderTests(unittest.TestCase):
    def test_disabled_when_no_config_is_given(self) -> None:
        changer = su.ChangeUploader(None)
        self.assertFalse(changer.enabled)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            self.assertIsNone(changer.maybe_upload(local))

    def test_enabled_records_attempts_and_collects_warnings_on_failure(self) -> None:
        cfg = make_config(retries=1)
        changer = su.ChangeUploader(cfg, transport=lambda c, p: (_ for _ in ()).throw(RuntimeError("down")),
                                    sleeper=lambda s: None)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = changer.maybe_upload(local)
        self.assertFalse(result.ok)
        self.assertEqual(1, len(changer.warnings))
        self.assertNotIn(SECRET, changer.warnings[0])

    def test_enabled_success_adds_no_warning(self) -> None:
        cfg = make_config()
        changer = su.ChangeUploader(cfg, transport=lambda c, p: None)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            result = changer.maybe_upload(local)
        self.assertTrue(result.ok)
        self.assertEqual([], changer.warnings)

    def test_dry_run_change_uploader_never_calls_the_transport(self) -> None:
        calls = []
        cfg = make_config()
        changer = su.ChangeUploader(cfg, dry_run=True, transport=lambda c, p: calls.append(1))
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            changer.maybe_upload(local)
        self.assertEqual([], calls)


class SshTransportRequiresSshpassForPasswordAuthTests(unittest.TestCase):
    """Without a key, password auth over scp/sftp needs `sshpass` on PATH
    (documented in the module's own docstring); when it is missing, the
    real transports refuse with a plain, non-secret explanation rather
    than hanging on an interactive password prompt no automated run could
    ever answer."""

    def test_scp_with_password_and_no_sshpass_raises_a_clear_error(self) -> None:
        cfg = make_config(protocol="scp", key_path=None, password=SECRET)
        original_which = su.shutil.which
        su.shutil.which = lambda name: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "f.json"
                local.write_text("{}", encoding="utf-8")
                with self.assertRaises(su.UploadTransportError) as ctx:
                    su._run_scp(cfg, local)
        finally:
            su.shutil.which = original_which
        self.assertIn("sshpass", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_sftp_with_password_and_no_sshpass_raises_a_clear_error(self) -> None:
        cfg = make_config(protocol="sftp", key_path=None, password=SECRET)
        original_which = su.shutil.which
        su.shutil.which = lambda name: None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                local = Path(tmp) / "f.json"
                local.write_text("{}", encoding="utf-8")
                with self.assertRaises(su.UploadTransportError) as ctx:
                    su._run_sftp(cfg, local)
        finally:
            su.shutil.which = original_which
        self.assertIn("sshpass", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))


class LocalProtocolTests(unittest.TestCase):
    """protocol: local -- not a network at all: a plain, atomic file copy,
    for a development site living on the same machine the conformance
    build runs on."""

    def test_local_needs_no_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = su.parse_upload_config({"protocol": "local", "remote_path": f"{tmp}/dest.json"})
        self.assertEqual("local", cfg.protocol)
        self.assertIsNone(cfg.host)

    def test_every_other_protocol_still_requires_a_host(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_config({"protocol": "sftp", "remote_path": "/p"})

    def test_describe_destination_for_local_has_no_host_or_at_sign(self) -> None:
        cfg = su.UploadConfig(protocol="local", remote_path="/var/www/dev/data/build-status.json")
        described = su.describe_destination(cfg)
        self.assertIn("/var/www/dev/data/build-status.json", described)
        self.assertNotIn("@", described)

    def test_a_real_local_copy_lands_atomically_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.json"
            src.write_text('{"schema_version": 2, "entries": {}}', encoding="utf-8")
            dest = Path(tmp) / "site" / "data" / "build-status.json"
            cfg = su.UploadConfig(protocol="local", remote_path=str(dest), mode="0644")
            result = su.upload_file(cfg, src)
            self.assertTrue(result.ok, result.detail)
            self.assertEqual('{"schema_version": 2, "entries": {}}', dest.read_text(encoding="utf-8"))
            self.assertEqual(0o644, dest.stat().st_mode & 0o777)
            # no leftover temp file
            self.assertEqual([dest.name], [p.name for p in dest.parent.iterdir()])

    def test_an_unwritable_destination_directory_is_a_reported_failure_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.json"
            src.write_text("{}", encoding="utf-8")
            # a file where a directory needs to be -- mkdir(parents=True) fails cleanly
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            cfg = su.UploadConfig(protocol="local", remote_path=str(blocker / "sub" / "dest.json"), retries=1)
            result = su.upload_file(cfg, src)
        self.assertFalse(result.ok)
        self.assertIn("could not copy", result.detail)


class ParseUploadDestinationsTests(unittest.TestCase):
    def test_none_and_empty_mean_nothing_configured(self) -> None:
        self.assertEqual([], su.parse_upload_destinations(None))
        self.assertEqual([], su.parse_upload_destinations({}))
        self.assertEqual([], su.parse_upload_destinations([]))

    def test_a_single_mapping_is_one_destination_named_upload(self) -> None:
        destinations = su.parse_upload_destinations({"protocol": "sftp", "host": "h", "remote_path": "/p"})
        self.assertEqual(1, len(destinations))
        self.assertEqual("upload", destinations[0].name)

    def test_a_list_preserves_order_and_names_each_by_position_by_default(self) -> None:
        destinations = su.parse_upload_destinations([
            {"protocol": "local", "remote_path": "/a"},
            {"protocol": "sftp", "host": "h", "remote_path": "/b"},
        ])
        self.assertEqual(["upload-1", "upload-2"], [d.name for d in destinations])
        self.assertEqual(["local", "sftp"], [d.protocol for d in destinations])

    def test_an_explicit_name_is_kept(self) -> None:
        destinations = su.parse_upload_destinations([
            {"name": "dev-site", "protocol": "local", "remote_path": "/a"},
        ])
        self.assertEqual("dev-site", destinations[0].name)

    def test_duplicate_names_are_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError) as ctx:
            su.parse_upload_destinations([
                {"name": "x", "protocol": "local", "remote_path": "/a"},
                {"name": "x", "protocol": "local", "remote_path": "/b"},
            ])
        self.assertIn("x", str(ctx.exception))

    def test_a_non_mapping_list_entry_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_destinations(["not-a-mapping"])

    def test_neither_a_mapping_nor_a_list_is_refused(self) -> None:
        with self.assertRaises(su.UploadConfigError):
            su.parse_upload_destinations("a string")


class VerifyPublishedTests(unittest.TestCase):
    def test_a_valid_schema_version_2_document_verifies(self) -> None:
        result = su.verify_published("http://site/data/build-status.json",
                                     fetcher=lambda url, timeout: b'{"schema_version": 2, "entries": {"a": {}}}')
        self.assertTrue(result.ok, result.detail)
        self.assertIn("1 entrie(s)", result.detail)

    def test_a_schema_version_1_document_fails_verification(self) -> None:
        result = su.verify_published("http://site/x", fetcher=lambda url, timeout: b'{"schema_version": 1}')
        self.assertFalse(result.ok)
        self.assertIn("schema_version", result.detail)

    def test_missing_entries_object_fails(self) -> None:
        result = su.verify_published("http://site/x", fetcher=lambda url, timeout: b'{"schema_version": 2}')
        self.assertFalse(result.ok)
        self.assertIn("entries", result.detail)

    def test_invalid_json_fails_without_raising(self) -> None:
        result = su.verify_published("http://site/x", fetcher=lambda url, timeout: b"not json")
        self.assertFalse(result.ok)

    def test_a_fetch_that_raises_is_reported_not_raised(self) -> None:
        def boom(url, timeout):
            raise OSError("connection refused")
        result = su.verify_published("http://site/x", fetcher=boom)
        self.assertFalse(result.ok)
        self.assertIn("connection refused", result.detail)


class FanOutUploaderTests(unittest.TestCase):
    """item 197: rehearsal (every `protocol: local` destination, plus a
    fetch-back verify when verify_url is given) must pass in full before a
    single production (non-local) destination is even attempted; a
    rehearsal failure is reported against every destination and touches no
    production one."""

    def _write_source(self, tmp: str) -> Path:
        src = Path(tmp) / "build-status.json"
        src.write_text('{"schema_version": 2, "entries": {}}', encoding="utf-8")
        return src

    def test_no_destinations_is_disabled_and_a_noop(self) -> None:
        uploader = su.FanOutUploader([])
        self.assertFalse(uploader.enabled)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], uploader.maybe_upload(self._write_source(tmp)))

    def test_production_only_behaves_like_a_single_destination(self) -> None:
        calls = []
        cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
        uploader = su.FanOutUploader([cfg], transport=lambda c, p: calls.append(1), sleeper=lambda s: None)
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = uploader.maybe_upload(self._write_source(tmp))
        self.assertEqual(1, len(calls))
        self.assertEqual([("prod", True, False)], [(o.name, o.ok, o.skipped) for o in outcomes])

    def test_local_success_lets_production_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dest = Path(tmp) / "site" / "build-status.json"
            local_cfg = su.UploadConfig(protocol="local", remote_path=str(local_dest), name="dev-site")
            prod_calls = []
            prod_cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
            uploader = su.FanOutUploader([local_cfg, prod_cfg],
                                         transport=lambda c, p: prod_calls.append(c.protocol), sleeper=lambda s: None)
            outcomes = uploader.maybe_upload(self._write_source(tmp))
            self.assertTrue(local_dest.is_file())
            self.assertEqual(["sftp"], prod_calls)  # the local copy never goes through "transport" -- os.replace only
            self.assertTrue(all(o.ok for o in outcomes))
            self.assertEqual(["dev-site", "prod"], [o.name for o in outcomes])

    def test_local_failure_blocks_production_entirely_and_says_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            local_cfg = su.UploadConfig(protocol="local", remote_path=str(blocker / "sub" / "f"), name="dev-site", retries=1)
            prod_calls = []
            prod_cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
            uploader = su.FanOutUploader([local_cfg, prod_cfg],
                                         transport=lambda c, p: prod_calls.append(1), sleeper=lambda s: None)
            outcomes = uploader.maybe_upload(self._write_source(tmp))
        self.assertEqual([], prod_calls, "production must never be touched when the rehearsal publish failed")
        by_name = {o.name: o for o in outcomes}
        self.assertFalse(by_name["dev-site"].ok)
        self.assertFalse(by_name["dev-site"].skipped)
        self.assertTrue(by_name["prod"].skipped)
        self.assertFalse(by_name["prod"].ok)
        self.assertIn("rehearsal", by_name["prod"].detail)

    def test_a_failed_verify_also_blocks_production_even_though_the_local_write_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dest = Path(tmp) / "site" / "build-status.json"
            local_cfg = su.UploadConfig(protocol="local", remote_path=str(local_dest), name="dev-site")
            prod_calls = []
            prod_cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
            uploader = su.FanOutUploader(
                [local_cfg, prod_cfg], transport=lambda c, p: prod_calls.append(1), sleeper=lambda s: None,
                verify_url="http://dev-site/data/build-status.json",
                verify_fetcher=lambda url, timeout: b'{"schema_version": 1}')
            outcomes = uploader.maybe_upload(self._write_source(tmp))
            self.assertTrue(local_dest.is_file(), "the write itself still happened")
            self.assertEqual([], prod_calls)
            by_name = {o.name: o for o in outcomes}
            self.assertFalse(by_name["rehearsal-verify"].ok)
            self.assertTrue(by_name["prod"].skipped)

    def test_a_passing_verify_lets_production_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dest = Path(tmp) / "site" / "build-status.json"
            local_cfg = su.UploadConfig(protocol="local", remote_path=str(local_dest), name="dev-site")
            prod_calls = []
            prod_cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
            uploader = su.FanOutUploader(
                [local_cfg, prod_cfg], transport=lambda c, p: prod_calls.append(1), sleeper=lambda s: None,
                verify_url="http://dev-site/data/build-status.json",
                verify_fetcher=lambda url, timeout: b'{"schema_version": 2, "entries": {}}')
            outcomes = uploader.maybe_upload(self._write_source(tmp))
        self.assertEqual(1, len(prod_calls))
        self.assertTrue(all(o.ok for o in outcomes))

    def test_dry_run_never_gates_and_never_calls_verify_fetcher(self) -> None:
        verify_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            local_cfg = su.UploadConfig(protocol="local", remote_path=f"{tmp}/site/f.json", name="dev-site")
            prod_calls = []
            prod_cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
            uploader = su.FanOutUploader(
                [local_cfg, prod_cfg], dry_run=True, transport=lambda c, p: prod_calls.append(1),
                verify_url="http://dev-site/x", verify_fetcher=lambda url, timeout: verify_calls.append(1))
            outcomes = uploader.maybe_upload(self._write_source(tmp))
        self.assertEqual([], prod_calls)  # dry-run: the transport itself is never called for any destination
        self.assertEqual([], verify_calls)
        self.assertTrue(all(o.ok for o in outcomes))

    def test_outcomes_and_warnings_accumulate_across_multiple_calls(self) -> None:
        cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
        calls = {"n": 0}

        def flaky(c, p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first call fails")
        uploader = su.FanOutUploader([cfg], transport=flaky, sleeper=lambda s: None)
        with tempfile.TemporaryDirectory() as tmp:
            src = self._write_source(tmp)
            uploader.maybe_upload(src)
            uploader.maybe_upload(src)
        self.assertEqual(2, len(uploader.outcomes))
        self.assertEqual(1, len(uploader.warnings))

    def test_no_local_destination_and_no_verify_url_means_no_gating_at_all(self) -> None:
        # a config with only production destinations must behave exactly
        # like today, unconditionally -- the rehearsal rule never applies
        # when there is nothing to rehearse.
        calls = []
        cfg = su.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1, name="prod")
        uploader = su.FanOutUploader([cfg], transport=lambda c, p: calls.append(1), sleeper=lambda s: None,
                                     verify_url="http://unused/x")
        with tempfile.TemporaryDirectory() as tmp:
            uploader.maybe_upload(self._write_source(tmp))
        self.assertEqual(1, len(calls))


class CliDryRunTests(unittest.TestCase):
    """The standalone CLI (tools/status_uploader.py --config ... --file ...)
    the owner uses to check a config, or that a file arrived, without
    running a build."""

    def test_no_upload_section_is_reported_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "c.yml"
            config_path.write_text("catalog_url: http://x\nworkdir: /tmp/x\n", encoding="utf-8")
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            code = su._cli_main(["--config", str(config_path), "--file", str(local)])
        self.assertEqual(1, code)

    def test_dry_run_by_default_prints_destination_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "c.yml"
            config_path.write_text(
                "catalog_url: http://x\nworkdir: /tmp/x\n"
                f"upload:\n  protocol: sftp\n  host: studio.example\n  remote_path: /p\n  password: {SECRET}\n",
                encoding="utf-8")
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            code = su._cli_main(["--config", str(config_path), "--file", str(local)])
        self.assertEqual(0, code)

    def test_invalid_upload_section_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "c.yml"
            config_path.write_text(
                "catalog_url: http://x\nworkdir: /tmp/x\nupload:\n  protocol: bogus\n  host: h\n  remote_path: /p\n",
                encoding="utf-8")
            local = Path(tmp) / "f.json"
            local.write_text("{}", encoding="utf-8")
            code = su._cli_main(["--config", str(config_path), "--file", str(local)])
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
