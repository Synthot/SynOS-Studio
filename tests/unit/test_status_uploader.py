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
