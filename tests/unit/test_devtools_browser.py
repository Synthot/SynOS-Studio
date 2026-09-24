"""tools/devtools_browser.py: the minimal Chrome DevTools Protocol client
and the StudioSession flow that drives the real Studio page. No test here
starts a real browser or opens a real socket to one — `_WS` is exercised
against a fake socket object, and `StudioSession`'s own page-interaction
methods are exercised with a scripted fake in place of `_WS`."""
from __future__ import annotations

import base64
import importlib.util
import json
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


db = load_module("devtools_browser_under_test", "tools/devtools_browser.py")


# -------------------------------------------------------------- fake socket
def make_server_frame(payload: bytes) -> bytes:
    """A server-to-client websocket text frame: FIN+opcode 0x1, unmasked
    (real servers, including Chrome, never mask their own frames)."""
    head = bytearray([0x81])
    length = len(payload)
    if length < 126:
        head.append(length)
    elif length < 65536:
        head += bytes([126]) + struct.pack(">H", length)
    else:
        head += bytes([127]) + struct.pack(">Q", length)
    return bytes(head) + payload


HANDSHAKE_RESPONSE = (
    b"HTTP/1.1 101 Switching Protocols\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"\r\n"
)


class FakeSocket:
    def __init__(self, initial_recv: bytes = b""):
        self.sent = b""
        self._recv_buffer = initial_recv
        self.timeout_set: list[float] = []
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout_set.append(value)

    def send(self, data: bytes) -> int:
        self.sent += data
        return len(data)

    def recv(self, n: int) -> bytes:
        chunk, self._recv_buffer = self._recv_buffer[:n], self._recv_buffer[n:]
        return chunk

    def close(self) -> None:
        self.closed = True

    def feed(self, data: bytes) -> None:
        self._recv_buffer += data


def unmask_client_frame(sent: bytes) -> tuple[str, dict]:
    """Decodes one client->server frame the way a real server would, to
    check _WS.send() actually produced a correct masked frame."""
    b0, b1 = sent[0], sent[1]
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack(">H", sent[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        length = struct.unpack(">Q", sent[offset:offset + 8])[0]
        offset += 8
    assert masked, "a client frame must be masked"
    mask = sent[offset:offset + 4]
    offset += 4
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(sent[offset:offset + length]))
    message = json.loads(payload)
    return message["method"], message.get("params", {})


class WSFrameTests(unittest.TestCase):
    def _connect(self, monkeypatch_recv: bytes = b"") -> tuple["db._WS", FakeSocket]:
        sock = FakeSocket(HANDSHAKE_RESPONSE + monkeypatch_recv)
        original = db.socket.create_connection
        db.socket.create_connection = lambda *a, **k: sock
        try:
            ws = db._WS("ws://127.0.0.1:9333/devtools/page/abc")
        finally:
            db.socket.create_connection = original
        return ws, sock

    def test_handshake_sends_a_websocket_upgrade_request(self) -> None:
        ws, sock = self._connect()
        request = sock.sent.decode()
        self.assertIn("GET /devtools/page/abc HTTP/1.1", request)
        self.assertIn("Upgrade: websocket", request)
        self.assertIn("Sec-WebSocket-Key:", request)

    def test_send_produces_a_correctly_masked_frame_and_matches_the_response_by_id(self) -> None:
        ws, sock = self._connect()
        sent_before_call = len(sock.sent)  # sock.sent also carries the HTTP handshake request itself
        # The response the "server" gives back, id=1 (the first send()).
        response = json.dumps({"id": 1, "result": {"result": {"value": 42}}}).encode()
        sock.feed(make_server_frame(response))
        result = ws.send("Runtime.evaluate", expression="1+1")
        method, params = unmask_client_frame(sock.sent[sent_before_call:])
        self.assertEqual("Runtime.evaluate", method)
        self.assertEqual("1+1", params.get("expression"))
        self.assertEqual(42, result["result"]["result"]["value"])

    def test_send_skips_unrelated_messages_until_the_matching_id(self) -> None:
        ws, sock = self._connect()
        unrelated = json.dumps({"method": "Page.loadEventFired", "params": {}}).encode()
        matching = json.dumps({"id": 1, "result": {"result": {"value": "ok"}}}).encode()
        sock.feed(make_server_frame(unrelated) + make_server_frame(matching))
        result = ws.send("Page.enable")
        self.assertEqual("ok", result["result"]["result"]["value"])

    def test_a_close_frame_from_the_server_is_reported_as_unavailable(self) -> None:
        ws, sock = self._connect()
        close_frame = bytes([0x88, 0x00])  # opcode 0x8 (close), zero-length payload
        sock.feed(close_frame)
        with self.assertRaises(db.BrowserUnavailable):
            ws.recv()

    def test_recv_handles_a_medium_length_payload(self) -> None:
        ws, sock = self._connect()
        payload = json.dumps({"id": 1, "value": "x" * 200}).encode()  # forces the 126 length-prefix branch
        sock.feed(make_server_frame(payload))
        message = ws.recv()
        self.assertEqual(200, len(message["value"]))


class FindBrowserTests(unittest.TestCase):
    def test_returns_the_first_binary_found(self) -> None:
        original = db.shutil.which
        db.shutil.which = lambda name: "/usr/bin/google-chrome" if name == "google-chrome" else None
        try:
            self.assertEqual("/usr/bin/google-chrome", db.find_browser())
        finally:
            db.shutil.which = original

    def test_returns_none_when_nothing_is_installed(self) -> None:
        original = db.shutil.which
        db.shutil.which = lambda name: None
        try:
            self.assertIsNone(db.find_browser())
        finally:
            db.shutil.which = original

    def test_an_explicit_binary_is_used_when_findable(self) -> None:
        original = db.shutil.which
        db.shutil.which = lambda name: "/opt/custom-chrome" if name == "/opt/custom-chrome" else None
        try:
            self.assertEqual("/opt/custom-chrome", db.find_browser("/opt/custom-chrome"))
        finally:
            db.shutil.which = original


class WaitForDebugPortTests(unittest.TestCase):
    def test_returns_the_pages_list_once_the_port_answers(self) -> None:
        class FakeResponse:
            def read(self):
                return json.dumps([{"type": "page", "webSocketDebuggerUrl": "ws://x"}]).encode()

        class FakeConn:
            def __init__(self, *a, **k):
                pass

            def request(self, method, path):
                pass

            def getresponse(self):
                return FakeResponse()

        original = db.http.client.HTTPConnection
        db.http.client.HTTPConnection = FakeConn
        try:
            pages = db._wait_for_debug_port(9333, timeout=2)
        finally:
            db.http.client.HTTPConnection = original
        self.assertEqual("page", pages[0]["type"])

    def test_raises_browser_unavailable_when_the_port_never_answers(self) -> None:
        class FailingConn:
            def __init__(self, *a, **k):
                raise ConnectionRefusedError("nobody is listening")

        original = db.http.client.HTTPConnection
        db.http.client.HTTPConnection = FailingConn
        try:
            with self.assertRaises(db.BrowserUnavailable):
                db._wait_for_debug_port(9333, timeout=0.5)
        finally:
            db.http.client.HTTPConnection = original


class ScriptedWS:
    """A strict, ordered queue of answers to successive Runtime.evaluate
    calls: the caller knows StudioSession's own call order (it is this
    module's own code, not the page's), so this needs no pattern matching
    on the expression text, only "what does the Nth eval() call get back".
    A value that is an Exception instance comes back as a page exception
    (StudioSession.eval raises PageError); anything else comes back as
    Runtime.evaluate's own returned value. Non-evaluate sends (Page.navigate,
    Page.enable, ...) always succeed and consume nothing from the queue."""

    def __init__(self, values: list[object]):
        self.values = list(values)
        self.expressions: list[str] = []

    def send(self, method: str, **params) -> dict:
        if method != "Runtime.evaluate":
            return {"result": {"result": {"value": True}}}
        self.expressions.append(params.get("expression", ""))
        if not self.values:
            return {"result": {"result": {"value": None}}}
        value = self.values.pop(0)
        if isinstance(value, Exception):
            return {"result": {"exceptionDetails": str(value)}}
        return {"result": {"result": {"value": value}}}


def _session_with(ws: ScriptedWS) -> "db.StudioSession":
    session = db.StudioSession.__new__(db.StudioSession)
    session.site_url = "http://fake-studio"
    session.nav_timeout = 5
    session._ws = ws
    return session


class StudioSessionPrimitiveTests(unittest.TestCase):
    def test_eval_returns_the_value(self) -> None:
        session = _session_with(ScriptedWS([2]))
        self.assertEqual(2, session.eval("1+1"))

    def test_eval_raises_page_error_on_an_exception(self) -> None:
        session = _session_with(ScriptedWS([RuntimeError("it broke")]))
        with self.assertRaises(db.PageError):
            session.eval("boom")

    def test_wait_for_returns_as_soon_as_truthy(self) -> None:
        session = _session_with(ScriptedWS([True]))
        self.assertTrue(session.wait_for("ready", timeout=2))

    def test_wait_for_times_out_and_raises_page_error(self) -> None:
        session = _session_with(ScriptedWS([]))  # queue empty -> eval() always returns None
        with self.assertRaises(db.PageError):
            session.wait_for("never true", timeout=0.3, interval=0.1)

    def test_click_raises_when_nothing_matches_the_selector(self) -> None:
        session = _session_with(ScriptedWS([False]))
        with self.assertRaises(db.PageError):
            session.click("#missing")

    def test_set_input_value_raises_when_the_input_is_not_found(self) -> None:
        session = _session_with(ScriptedWS([False]))
        with self.assertRaises(db.PageError):
            session.set_input_value("#missing", "x")


class DownloadBundleFlowTests(unittest.TestCase):
    """Pins download_bundle()'s own eval() call sequence, in order:
    1 navigate's app-ready wait, 1 _clear_storage (None = no error; a real
    page only gets this call *after* navigating, never on about:blank — the
    real-browser bug this class exists to prevent regressing on), 1 wait
    for the catalog data itself (window.D.bundle_catalog, not just the
    scripts, to be populated — a real page can race the two), 1 click
    #btn-catalog, 1 wait for the catalog page, 1 "offered" check against
    window.D (never the DOM: the catalog page only renders one page of
    cards at a time), 1 call to the page's own chooseCatalogEntry() (not a
    DOM click on a card, which might not be rendered), 1 wait for the
    naming step, 1 set #b-name, 1 wait for S.name, 1 to arm the Blob tee,
    1 validation() check, 1 click #btn-generate, 1 wait for the Blob, 1 to
    read it back as base64, 1 for the filename."""

    def _happy_path_values(self, blob_b64: str, filename: str) -> list[object]:
        return [True, None, True, True, True, True, True, True, True, True, True, [], True, True, blob_b64, filename]

    def test_downloads_and_decodes_the_real_bytes(self) -> None:
        raw = b"fake archive bytes"
        blob_b64 = base64.b64encode(raw).decode()
        ws = ScriptedWS(self._happy_path_values(blob_b64, "web-server-nginx-bundle.tar.gz"))
        session = _session_with(ws)
        data, filename = session.download_bundle("web-server-nginx")
        self.assertEqual(raw, data)
        self.assertEqual("web-server-nginx-bundle.tar.gz", filename)
        # sanity: the name field was actually set to the entry id, and the
        # entry's own id was used to find it in the page's own catalog data.
        self.assertTrue(any('"web-server-nginx"' in e for e in ws.expressions))

    def test_a_noted_storage_clear_failure_does_not_abort_the_download(self) -> None:
        """The regression this whole file exists to prevent regressing on:
        a real page can refuse to clear storage (or, before this fix,
        clearing it before the first navigation always raised); either way
        it must be a note, never fatal."""
        raw = b"fake archive bytes"
        blob_b64 = base64.b64encode(raw).decode()
        values = self._happy_path_values(blob_b64, "web-server-nginx-bundle.tar.gz")
        values[1] = "SecurityError: Access is denied for this document."  # _clear_storage's own error string
        session = _session_with(ScriptedWS(values))
        data, filename = session.download_bundle("web-server-nginx")
        self.assertEqual(raw, data)

    def test_entry_not_offered_raises_page_error(self) -> None:
        # calls: app-ready, clear-storage, catalog-data-ready, click #btn-catalog, wait catalog page, offered=False
        values = [True, None, True, True, True, False]
        session = _session_with(ScriptedWS(values))
        with self.assertRaises(db.PageError):
            session.download_bundle("no-such-entry")

    def test_validation_blocking_raises_page_error(self) -> None:
        # everything succeeds up through arming the Blob tee, then validation() returns a crit error
        values = [True, None, True, True, True, True, True, True, True, True, True,
                 [["crit", "something is wrong"]]]
        session = _session_with(ScriptedWS(values))
        with self.assertRaises(db.PageError):
            session.download_bundle("web-server-nginx")

    def test_no_blob_ever_produced_raises_page_error(self) -> None:
        # validation passes, #btn-generate is clicked, but the Blob wait then times out
        values = [True, None, True, True, True, True, True, True, True, True, True, [], True]
        session = _session_with(ScriptedWS(values))
        with self.assertRaises(db.PageError):
            session.download_bundle("web-server-nginx", download_timeout=0.3)


if __name__ == "__main__":
    unittest.main()
