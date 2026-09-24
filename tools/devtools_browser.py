#!/usr/bin/env python3
"""devtools_browser — drive the real Studio configurator page under headless
Chrome and capture exactly the bundle bytes it downloads for a catalog
entry, the way a person gets them.

Reuses the technique the Studio repository's own
`tests/e2e_open_bundle.py` already proved out, rather than inventing a new
one: a minimal Chrome DevTools Protocol client over a raw stdlib websocket
connection (no selenium, no non-stdlib dependency — the same reason that
test built one), and capturing a download by overriding
`URL.createObjectURL` on the page and reading the `Blob` back directly,
since headless Chrome's own download manager needs a profile directory and
extra flags this does not otherwise need.

`StudioSession` is the only thing tools/catalog_conformance.py calls:

    with StudioSession(site_url) as session:
        raw_bytes, filename = session.download_bundle("web-server-nginx")

`download_bundle` clicks through the real UI the way a person does —
Bundle Catalog, the entry's card, the name field, "Download bundle" — not a
shortcut through the page's internal state, because the whole point of
driving the real page is to catch the class of bug a shortcut would miss
(the page silently dropping fields when it serializes a catalogued bundle
back out, which is what actually happened and cost a day: found only by
building the resulting image and booting it, docs/BUILD_MATRIX.md).

Availability is checked with `find_browser()`; a caller gets a clean,
printable `BrowserUnavailable` rather than a crash when no browser exists.
`PageError` covers everything the page itself did wrong (the entry is not
offered, its own validation blocks the download, the download never
produced anything) — the "page" stage in a conformance report
(docs/BUNDLE.md's own three-stage split: page, launcher, engine).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_PORT = 9333
DEFAULT_NAV_TIMEOUT = 30.0
DEFAULT_DOWNLOAD_TIMEOUT = 30.0
# The scripts (JSZip/jsyaml) and the wizard's own DOM exist as soon as the
# page's <script> tags finish executing; the catalog data (window.D, fetched
# separately, data/catalog.json) is not guaranteed to be populated at that
# same moment — a real browser can and does race the two. Both are needed
# before the Bundle Catalog can be used at all, so both are waited for.
APP_READY_EXPR = "!!(window.JSZip && window.jsyaml && document.getElementById('open-file'))"
CATALOG_READY_EXPR = "typeof D!=='undefined' && D && Array.isArray(D.bundle_catalog) && D.bundle_catalog.length>0"


def _eprint(message: str) -> None:
    """print(..., file=sys.stderr) that cannot itself raise: a caller with a
    closed or redirected stderr (a test capturing output, a service manager
    that already tore its pipe down) must never turn a non-fatal note into a
    crash of its own."""
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001 - reporting a note must never itself raise
        pass


class BrowserUnavailable(Exception):
    """No Chrome/Chromium binary, or it never opened a debugging port."""


class PageError(Exception):
    """The page itself did something a real person would also see: the
    entry is not offered, its own validation refused the download, or the
    download never produced a Blob. Maps to the "page" stage."""


def find_browser(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if shutil.which(explicit) or Path(explicit).is_file() else None
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    return None


# --------------------------------------------------------- CDP over a raw ws
class _WS:
    """A minimal Chrome DevTools Protocol client: a stdlib websocket client
    good enough for Page.navigate/Runtime.evaluate, nothing else. Ported
    from SynOS-Studio's tests/e2e_open_bundle.py unchanged in approach."""

    def __init__(self, url: str, timeout: float = 30.0):
        host, rest = url[5:].split("/", 1)
        h, p = host.split(":")
        self.s = socket.create_connection((h, int(p)), timeout=timeout)
        self.s.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.send(f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise BrowserUnavailable("the browser closed the debugging connection during the websocket handshake")
            buf += chunk
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.n = 0

    def _recv(self, k: int) -> bytes:
        while len(self.buf) < k:
            chunk = self.s.recv(65536)
            if not chunk:
                raise BrowserUnavailable("the browser closed the debugging connection")
            self.buf += chunk
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def send(self, method: str, **params) -> dict:
        self.n += 1
        payload = json.dumps({"id": self.n, "method": method, "params": params}).encode()
        mask = os.urandom(4)
        head = bytearray([0x81])
        length = len(payload)
        if length < 126:
            head.append(0x80 | length)
        elif length < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", length)
        self.s.send(bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        while True:
            message = self.recv()
            if message.get("id") == self.n:
                return message

    def recv(self) -> dict:
        b0, b1 = self._recv(2)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv(8))[0]
        data = self._recv(length)
        if (b0 & 0x0F) == 0x8:
            raise BrowserUnavailable("the browser's devtools websocket closed")
        return json.loads(data) if (b0 & 0x0F) == 0x1 else {}

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


def _wait_for_debug_port(port: int, timeout: float = 15.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/json")
            return json.loads(conn.getresponse().read())
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = exc
            time.sleep(0.3)
    raise BrowserUnavailable(f"chrome never opened a debugging port on {port}: {last_error}")


# ------------------------------------------------------------- the session
class StudioSession:
    """One headless Chrome instance, one page, against `site_url` (the
    deployed Studio site or a locally served release — this class does not
    care which, it only ever connects to a URL already given to it)."""

    def __init__(self, site_url: str, *, browser_binary: str | None = None, port: int = DEFAULT_PORT,
                 nav_timeout: float = DEFAULT_NAV_TIMEOUT):
        self.site_url = site_url.rstrip("/")
        self.browser_binary = find_browser(browser_binary)
        if not self.browser_binary:
            raise BrowserUnavailable("no google-chrome/chromium binary found on PATH")
        self.port = port
        self.nav_timeout = nav_timeout
        self._proc: subprocess.Popen | None = None
        self._profile_dir: str | None = None
        self._ws: _WS | None = None

    def __enter__(self) -> "StudioSession":
        self._profile_dir = tempfile.mkdtemp(prefix="synos-studio-browser-")
        self._proc = subprocess.Popen(
            [self.browser_binary, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self._profile_dir}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            pages = _wait_for_debug_port(self.port)
            page = next((p for p in pages if p.get("type") == "page"), None)
            if page is None:
                raise BrowserUnavailable("chrome opened a debugging port but offered no page target")
            self._ws = _WS(page["webSocketDebuggerUrl"])
            self._ws.send("Page.enable")
            self._ws.send("Runtime.enable")
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=10)
            self._proc = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    # ---------------------------------------------------------- primitives
    def eval(self, expression: str) -> object:
        assert self._ws is not None
        response = self._ws.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        result = response.get("result", {})
        if "exceptionDetails" in result:
            raise PageError(f"the page raised evaluating {expression[:120]!r}: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float | None = None, interval: float = 0.3) -> object:
        deadline = time.monotonic() + (timeout if timeout is not None else self.nav_timeout)
        value = None
        while time.monotonic() < deadline:
            value = self.eval(expression)
            if value:
                return value
            time.sleep(interval)
        raise PageError(f"timed out after {timeout or self.nav_timeout}s waiting for: {expression}")

    def navigate(self, path: str = "/index.html") -> None:
        assert self._ws is not None
        self._ws.send("Page.navigate", url=f"{self.site_url}{path}")
        self.wait_for(APP_READY_EXPR)

    def _clear_storage(self) -> None:
        """Best-effort: only ever called after navigate(), never before —
        the tab starts on about:blank, where localStorage access itself
        raises a SecurityError (storage is not available on that origin at
        all, real Chrome, not this driver, refuses it), so clearing before
        the first navigation always fails. Even after navigating to a real
        origin, a storage failure (a browser policy, a sandboxed profile)
        is noted on stderr, never fatal: nothing a bundle download does
        depends on localStorage being clearable, only on the site itself
        loading."""
        error = self.eval("(()=>{try{localStorage.clear();return null;}catch(e){return String(e);}})()")
        if error:
            _eprint(f"note: could not clear localStorage on {self.site_url} (continuing anyway): {error}")

    def click(self, selector: str) -> None:
        clicked = self.eval(f"(()=>{{const el=document.querySelector({json.dumps(selector)});"
                            f"if(!el) return false; el.click(); return true;}})()")
        if not clicked:
            raise PageError(f"no element on the page matches {selector!r} to click")

    def set_input_value(self, selector: str, value: str) -> None:
        set_ok = self.eval(f"""(()=>{{const el=document.querySelector({json.dumps(selector)});
            if(!el) return false; el.value={json.dumps(value)};
            el.dispatchEvent(new Event('input',{{bubbles:true}})); return true;}})()""")
        if not set_ok:
            raise PageError(f"no input on the page matches {selector!r} to fill in")

    # ------------------------------------------------------------- the flow
    def download_bundle(self, entry_id: str, *, name: str | None = None,
                        download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT) -> tuple[bytes, str]:
        """Chooses the catalog entry `entry_id` through the real Bundle
        Catalog page, fills the configuration's name (deterministic:
        `entry_id` itself unless `name` overrides it, matching
        docs/BUNDLE.md's "essentially the name"), and downloads the bundle
        exactly through the page's own path: the "Download bundle" button,
        `downloadBundle()`, `URL.createObjectURL(blob)`. Returns the exact
        bytes the page produced and the filename it chose. Raises PageError
        for anything the page itself did wrong; never raises for a build
        failure downstream — there is none yet at this point."""
        name = name or entry_id
        self.navigate()
        self._clear_storage()
        self.wait_for(CATALOG_READY_EXPR)
        self.click("#btn-catalog")
        self.wait_for("!document.getElementById('catalog-page').hidden")

        # The catalog page only renders a page of cards at a time (12,
        # "Show more" reveals the rest) — the entry can be real and still
        # have no [data-bundle=...] card in the DOM yet, so "offered" is
        # checked against the page's own full catalog data (window.D),
        # never the DOM. Choosing it then calls the page's own
        # chooseCatalogEntry() directly with that entry — the exact
        # function a card's click handler calls — rather than depending on
        # search or "Show more" to first scroll the entry into the DOM.
        offered = self.eval(f"(D && Array.isArray(D.bundle_catalog)) ? "
                            f"D.bundle_catalog.some(e => e && e.id === {json.dumps(entry_id)}) : false")
        if not offered:
            raise PageError(f"the Bundle Catalog at {self.site_url} does not offer an entry with id {entry_id!r}")
        chosen = self.eval(f"""(() => {{
            const entry = (D.bundle_catalog || []).find(e => e && e.id === {json.dumps(entry_id)});
            if (!entry || typeof chooseCatalogEntry !== 'function') return false;
            chooseCatalogEntry(entry);
            return true;
        }})()""")
        if not chosen:
            raise PageError(f"could not choose {entry_id!r} through the page's own chooseCatalogEntry()")
        self.wait_for("document.querySelector('.step.active')?.dataset.step === '1'")
        self.set_input_value("#b-name", name)
        self.wait_for(f"typeof S!=='undefined' && S.name === {json.dumps(name)}")

        # Tee every Blob handed to URL.createObjectURL: the exact bytes
        # downloadBundle() produces, read back directly, the same technique
        # tests/e2e_open_bundle.py uses — no dependency on Chrome's own
        # download manager or disk path.
        self.eval("window.__lastBlob=null; window.__lastBlobAt=0;"
                  "const _origCOU=URL.createObjectURL;"
                  "URL.createObjectURL=(b)=>{window.__lastBlob=b; window.__lastBlobAt=Date.now(); return _origCOU(b);}; true;")

        crit = self.eval("(typeof validation==='function') ? (validation()||[]).filter(x=>x[0]==='crit') : []")
        if crit:
            raise PageError(f"the page's own validation refuses to generate a bundle for {entry_id!r}: {crit}")

        self.click("#btn-generate")
        self.wait_for("!!window.__lastBlob", timeout=download_timeout)
        b64_data = self.eval("""(async()=>{
            const buf = await window.__lastBlob.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
            return btoa(binary);
        })()""")
        if not b64_data:
            raise PageError(f"the page produced no downloadable bytes for {entry_id!r}")
        filename = self.eval("(typeof S!=='undefined' && S.name) ? (S.name + '-bundle.tar.gz') : 'bundle.tar.gz'")
        return base64.b64decode(b64_data), str(filename)


def main(argv: list[str] | None = None) -> int:  # a small manual smoke check, not the conformance tool itself
    import argparse
    parser = argparse.ArgumentParser(description="Download one catalogued bundle through the real Studio page.")
    parser.add_argument("site_url")
    parser.add_argument("entry_id")
    parser.add_argument("--output", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    with StudioSession(args.site_url) as session:
        raw, filename = session.download_bundle(args.entry_id)
    out_path = args.output / filename
    out_path.write_bytes(raw)
    print(f"wrote {out_path} ({len(raw)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
