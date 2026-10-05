"""packages/_lib/resolve-gnome-ext.py's bounded retry: one transient upstream
error (a 502 from the gateway in front of extensions.gnome.org) must not be
fatal to a package, while a real answer like 404 or 403 must still fail at
once. The network is never reached: urllib.request.urlopen and time.sleep
are stubbed in every test."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import socket
import tempfile
import unittest
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_resolver():
    spec = importlib.util.spec_from_file_location("resolve_gnome_ext", ROOT / "packages" / "_lib" / "resolve-gnome-ext.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def http_error(url: str, code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, f"HTTP {code}", message, io.BytesIO(b""))


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def scripted_urlopen(outcomes: list):
    """A urlopen stand-in that plays outcomes in order: an exception is
    raised, bytes are returned as the body. Records every URL asked for."""
    calls: list[str] = []
    remaining = list(outcomes)

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResponse(outcome)

    return urlopen, calls


def zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("metadata.json", json.dumps({"shell-version": ["47"]}))
    return buffer.getvalue()


class RetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = load_resolver()
        self.url = f"{self.resolver.BASE_URL}/extension-info/?uuid=x@example.com"

    def run_fetch(self, outcomes: list, timeout: float = 15):
        urlopen, calls = scripted_urlopen(outcomes)
        stderr = io.StringIO()
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(self.resolver.time, "sleep") as sleep, \
                contextlib.redirect_stderr(stderr):
            try:
                result = self.resolver.fetch_with_retry(self.url, timeout=timeout)
            except BaseException as exc:  # noqa: BLE001 — returned for the test to inspect
                result = exc
        return result, calls, [c.args[0] for c in sleep.call_args_list], stderr.getvalue()

    def test_a_502_then_success_succeeds_and_says_why_it_waited(self) -> None:
        body, calls, sleeps, err = self.run_fetch([http_error(self.url, 502), b'{"ok": true}'])
        self.assertEqual(b'{"ok": true}', body)
        self.assertEqual(2, len(calls))
        self.assertEqual([self.resolver.RETRY_BASE_DELAY], sleeps)
        self.assertIn(f"attempt 1/{self.resolver.RETRY_ATTEMPTS} for {self.url} failed", err)
        self.assertIn("502", err)
        self.assertIn("retrying in 5s", err)

    def test_connection_level_failures_are_retried_too(self) -> None:
        body, calls, _, err = self.run_fetch([
            urllib.error.URLError("temporary failure in name resolution"),
            socket.timeout("timed out"),
            ConnectionResetError(104, "Connection reset by peer"),
            b"done"])
        self.assertEqual(b"done", body)
        self.assertEqual(4, len(calls))
        self.assertEqual(3, err.count("retrying in"))

    def test_repeated_5xx_gives_up_naming_the_url_the_error_and_the_attempts(self) -> None:
        attempts = self.resolver.RETRY_ATTEMPTS
        result, calls, sleeps, err = self.run_fetch([http_error(self.url, 503)] * attempts + [b"never reached"])
        self.assertIsInstance(result, SystemExit)
        message = str(result.code)
        self.assertIn(self.url, message)
        self.assertIn("503", message)
        self.assertIn(f"after {attempts} attempt(s)", message)
        self.assertEqual(attempts, len(calls))
        self.assertEqual([5.0, 10.0, 20.0], sleeps)       # exponential, and no sleep after the last attempt
        self.assertLessEqual(sum(sleeps), self.resolver.RETRY_TOTAL_WAIT_CAP)
        self.assertEqual(attempts - 1, err.count("retrying in"))

    def test_a_404_is_raised_at_once_with_no_retry(self) -> None:
        result, calls, sleeps, err = self.run_fetch([http_error(self.url, 404), b"never reached"])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(404, result.code)
        self.assertEqual(1, len(calls))
        self.assertEqual([], sleeps)
        self.assertEqual("", err)

    def test_a_403_is_not_retried(self) -> None:
        result, calls, sleeps, _ = self.run_fetch([http_error(self.url, 403), b"never reached"])
        self.assertIsInstance(result, urllib.error.HTTPError)
        self.assertEqual(403, result.code)
        self.assertEqual(1, len(calls))
        self.assertEqual([], sleeps)

    def test_a_429_honours_retry_after(self) -> None:
        body, _, sleeps, err = self.run_fetch([http_error(self.url, 429, {"Retry-After": "7"}), b"ok"])
        self.assertEqual(b"ok", body)
        self.assertEqual([7.0], sleeps)
        self.assertIn("retrying in 7s", err)

    def test_a_429_retry_after_is_capped(self) -> None:
        body, _, sleeps, _ = self.run_fetch([http_error(self.url, 429, {"Retry-After": "3600"}), b"ok"])
        self.assertEqual(b"ok", body)
        self.assertEqual([self.resolver.RETRY_AFTER_CAP], sleeps)

    def test_total_wait_is_capped_even_when_every_429_asks_for_the_maximum(self) -> None:
        errors = [http_error(self.url, 429, {"Retry-After": "3600"})] * self.resolver.RETRY_ATTEMPTS
        result, _, sleeps, _ = self.run_fetch(errors)
        self.assertIsInstance(result, SystemExit)
        self.assertLessEqual(sum(sleeps), self.resolver.RETRY_TOTAL_WAIT_CAP)
        self.assertIn(self.url, str(result.code))


class BothCallSitesRetryTests(unittest.TestCase):
    """The info request and the download both go through fetch_with_retry,
    and the info request's own 404 message is exactly what it was."""

    def setUp(self) -> None:
        self.resolver = load_resolver()

    def test_the_info_request_survives_a_502(self) -> None:
        urlopen, calls = scripted_urlopen([http_error("u", 502), json.dumps({"name": "X"}).encode()])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(self.resolver.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            info = self.resolver.fetch_extension_info("x@example.com", shell_version="47")
        self.assertEqual({"name": "X"}, info)
        self.assertEqual(2, len(calls))
        self.assertIn("/extension-info/?uuid=x@example.com&shell_version=47", calls[0])
        self.assertIn("retrying in", err.getvalue())

    def test_the_info_request_404_message_is_unchanged_and_not_retried(self) -> None:
        urlopen, calls = scripted_urlopen([http_error("u", 404), b"never reached"])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(self.resolver.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as ctx:
                self.resolver.fetch_extension_info("x@example.com")
        self.assertEqual("Extension 'x@example.com' not found on extensions.gnome.org", ctx.exception.code)
        self.assertEqual(1, len(calls))
        sleep.assert_not_called()

    def test_the_download_survives_a_502(self) -> None:
        urlopen, calls = scripted_urlopen([http_error("u", 502), zip_bytes()])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(self.resolver.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.resolver.download_extension("x@example.com", "47", directory)
            self.assertTrue((Path(directory) / "metadata.json").is_file())
        self.assertEqual(2, len(calls))
        self.assertIn("/download-extension/x@example.com.shell-extension.zip?shell_version=47", calls[0])
        self.assertIn("retrying in", err.getvalue())

    def test_the_download_gives_up_with_a_message_not_a_traceback(self) -> None:
        attempts = self.resolver.RETRY_ATTEMPTS
        urlopen, calls = scripted_urlopen([http_error("u", 502)] * attempts)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(self.resolver.time, "sleep"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    self.resolver.download_extension("x@example.com", "47", directory)
        self.assertIn("download-extension/x@example.com", str(ctx.exception.code))
        self.assertIn(f"after {attempts} attempt(s)", str(ctx.exception.code))
        self.assertEqual(attempts, len(calls))


if __name__ == "__main__":
    unittest.main()
