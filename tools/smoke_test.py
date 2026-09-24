#!/usr/bin/env python3
"""smoke_test — boot a built ISO in QEMU, headless and graphical, and check
that the appliance it promised is real.

    python3 tools/smoke_test.py IMAGE.iso
    python3 tools/smoke_test.py IMAGE.iso --profile path/to/leaf-profile.yml
    python3 tools/smoke_test.py IMAGE.iso --resolved path/to/name.resolved.json

Called by tools/build_matrix.py after a successful build, with the profile
already resolved (its `extends` chain merged the way tools/render_manifest.py
merges it) and passed straight to run(); run() itself never re-implements
that merge.

The CLI entry point (main()) is a different case: what a bundle's own
profiles/<id>.yml declares is the *leaf* of an `extends` chain, not the
resolved configuration the image was actually built from — a bundle profile
that inherits its ports from the catalog "kind" it extends declares none of
its own. The honest source of truth for what an image promises is the
resolved configuration the engine writes next to it, dist/<name>.resolved.json
(build.sh, "Write the package lock and SBOM"). main() looks for that file
first (next to the ISO, or at --resolved); only when it is missing does it
fall back to resolving --profile's own `extends` chain itself, the same way
tools/build_matrix.py's _resolve_bundle_profile does for a bundle-catalog
entry. Either way, the result records which source was used.

What it asserts, over a root shell on the serial console (the headless boot):
  - the live system reaches its default systemd target
  - the ports opened by the shipped first-boot firewall script
    (/usr/libexec/synos-first-boot-services) match the profile's
    security.open_ports exactly
  - every software.services entry has a quadlet-generated systemd unit
    that exists and is enabled
  - every software.files entry is present with the mode the profile asked for

...and what it certifies from a second, graphical boot (check_graphical_boot):
  - the live session comes up on its own real graphical path (no debug
    shell, no console=ttyS0) and its first screen, captured once it stops
    changing, reads by OCR as showing both the distribution's own name
    (from the resolved configuration, the same source as the ports check's
    expectation) and the word "Installer" — the promise a "certified" badge
    would stand behind.

The firewall check is honest about its own limit: base_hardening's
first-boot service carries `ConditionKernelCommandLine=!rd.synos.live`, so
the ports are never actually opened inside the live session that boots
here — only on an installed system's real first boot. What is checked is
that the shipped script *would* open exactly the promised ports, not that
they are open right now. Nothing that would need a network or credentials
(an installed system, a signed-in package mirror) is asserted; where the
matrix cannot check something honestly it is left out, not invented.

Requires qemu-system-x86_64 and xorriso on PATH (the graphical check also
needs tesseract and Pillow). Their absence is a clean skip, never a
failure: run() returns {"status": "skipped", ...} if the headless boot's
own tools are missing, or check_graphical_boot returns its own
{"outcome": "skipped", ...} if only the graphical check's extra tools are
missing — everything else still runs. Nothing here touches the network:
the live kernel and initrd are pulled out of the ISO with `xorriso
-osirrox` and booted directly (-kernel/-initrd), bypassing the graphical
GRUB menu. The headless boot adds a root debug shell on the serial console
(console=ttyS0, systemd.debug_shell=ttyS0, serial-getty masked) — the same
recipe tests/framework/grub.py's debug_kernel_arguments uses for the full
acceptance suite, applied here without that suite's GRUB-menu and
screenshot machinery. The graphical boot carries none of that: it is the
plain boot a person would actually see, captured with QEMU's own monitor
(`-display none` still renders into emulated video RAM; `screendump` reads
it directly, without a viewer or a window).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MEMORY_MB = 2048
DEFAULT_BOOT_TIMEOUT = 180        # seconds waiting for the debug shell to answer
DEFAULT_TARGET_TIMEOUT = 240      # seconds waiting for the default target to become active
DEFAULT_COMMAND_TIMEOUT = 20      # seconds per command sent over the serial line

# The graphical check renders a real desktop session (GDM + GNOME Shell),
# not just a text-mode debug shell, so it needs more memory and much more
# patience than the headless checks above -- especially on a host with no
# hardware virtualization (accel=tcg), which this must still work on, just
# slower. 240s is generous for that unaccelerated case; on KVM the session
# typically settles in well under a minute, so the extra headroom is free.
DEFAULT_GRAPHICAL_MEMORY_MB = 4096
DEFAULT_GRAPHICAL_SETTLE_TIMEOUT = 240.0
GRAPHICAL_POLL_INTERVAL = 2.0      # seconds between screendump samples
# Measured against a real image: systemd's own unit start-up (still visible
# under `quiet splash` for a moment before Plymouth takes the framebuffer,
# and briefly again as it hands off to GDM) can sit still for several
# seconds between lines while a slow service starts -- 3 consecutive
# identical samples (4-6s of quiet) was fooled by exactly that pause and
# called a mid-boot console frame "settled". 8 samples (14-16s of quiet)
# comfortably outlasts a single inter-service pause without meaningfully
# slowing down a real settle, which holds indefinitely once the session is
# actually up.
GRAPHICAL_STABLE_SAMPLES = 8


class SmokeTestUnavailable(Exception):
    """qemu-system-x86_64 or xorriso is not on PATH."""


class QemuBootTimeout(Exception):
    """The live system never answered on the serial console in time."""


# --------------------------------------------------------------- discovery
def tools_available() -> tuple[bool, str]:
    missing = [name for name in ("qemu-system-x86_64", "xorriso") if not shutil.which(name)]
    if missing:
        return False, f"{', '.join(missing)} not found on PATH; install qemu-system-x86 and xorriso to run the smoke test"
    return True, ""


def graphical_tools_available() -> tuple[bool, str]:
    """Everything the graphical boot check needs beyond tools_available():
    tesseract to read the screenshot, and Pillow to turn QEMU's PPM
    screendump into a PNG. No display server, VNC viewer or X11 of any kind
    is required -- `-display none` still runs the emulated VGA device, and
    the monitor's screendump command reads directly off it -- so this never
    checks for one."""
    missing = [name for name in ("qemu-system-x86_64", "xorriso", "tesseract") if not shutil.which(name)]
    if missing:
        return False, f"{', '.join(missing)} not found on PATH; install them to run the graphical boot check"
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False, "Pillow (the PIL package) is not installed; install it to run the graphical boot check"
    return True, ""


# ----------------------------------------------------------- ISO handling
def iso_volume_label(iso_path: Path) -> str:
    """The CDLABEL dracut's live module boots with (root=live:CDLABEL=<label>).
    Tried cheapest first; none of these touch the network."""
    blkid = shutil.which("blkid")
    if blkid:
        result = subprocess.run([blkid, "-o", "value", "-s", "LABEL", str(iso_path)], capture_output=True, text=True, check=False)
        label = result.stdout.strip()
        if result.returncode == 0 and label:
            return label
    xorriso = shutil.which("xorriso")
    if xorriso:
        result = subprocess.run([xorriso, "-indev", str(iso_path), "-pvd_info"], capture_output=True, text=True, check=False)
        for line in result.stdout.splitlines():
            match = re.match(r"\s*Volume id\s*:\s*'(.*)'\s*$", line)
            if match:
                return match.group(1)
    # Raw fallback: the ISO9660 primary volume descriptor's Volume Identifier
    # is 32 bytes at offset 32808 (sector 16, byte 40), space-padded.
    with iso_path.open("rb") as handle:
        handle.seek(32808)
        raw = handle.read(32)
    label = raw.decode("ascii", "replace").strip()
    if not label:
        raise SmokeTestUnavailable(f"could not read the volume label of {iso_path}")
    return label


def extract_boot_files(iso_path: Path, workdir: Path) -> tuple[Path, Path]:
    """The live kernel and initrd, pulled out of the ISO (image/LiveOS/{vmlinuz,initrd}, build.sh)."""
    xorriso = shutil.which("xorriso")
    if not xorriso:
        raise SmokeTestUnavailable("xorriso not found on PATH")
    kernel, initrd = workdir / "vmlinuz", workdir / "initrd"
    result = subprocess.run(
        [xorriso, "-osirrox", "on", "-indev", str(iso_path),
         "-extract", "/LiveOS/vmlinuz", str(kernel), "-extract", "/LiveOS/initrd", str(initrd)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not kernel.is_file() or not initrd.is_file():
        raise SmokeTestUnavailable(f"could not extract /LiveOS/vmlinuz and /LiveOS/initrd from {iso_path}: {result.stderr.strip()}")
    return kernel, initrd


# --------------------------------------------------------- serial session
class SerialSession:
    """A QEMU guest's serial console, driven by a marker-delimited command
    protocol: each command is wrapped in two `echo` sentinels so its output
    (and only its output, not the echoed input line) can be recovered from
    the raw byte stream, whether or not the guest shell echoes keystrokes."""

    def __init__(self, proc: subprocess.Popen, transcript_path: Path):
        self.proc = proc
        self._buf = bytearray()
        self._lock = threading.Lock()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self._transcript = transcript_path.open("ab", buffering=0)
        self._closed = False
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        stdout = self.proc.stdout
        assert stdout is not None
        while True:
            chunk = stdout.read(1)
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)
            try:
                self._transcript.write(chunk)
            except ValueError:
                return

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def wait_for(self, pattern: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.snapshot()
            if pattern in data:
                return data
            if self.proc.poll() is not None:
                raise QemuBootTimeout(f"qemu exited (code {self.proc.returncode}) before {pattern!r} appeared; tail: {data[-2000:]!r}")
            time.sleep(0.2)
        raise QemuBootTimeout(f"timed out after {timeout}s waiting for {pattern!r}; tail: {self.snapshot()[-2000:]!r}")

    def run(self, command: str, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> tuple[str, int]:
        token = uuid.uuid4().hex[:10]
        start_marker, end_marker = f"SMOKE-START-{token}", f"SMOKE-END-{token}"
        before = len(self.snapshot())
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(f"echo {start_marker}; {command}; echo {end_marker} $?\n".encode())
        stdin.flush()
        self.wait_for(end_marker.encode(), timeout)
        text = self.snapshot()[before:].decode("utf-8", "replace")
        return parse_command_output(text, start_marker, end_marker)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._transcript.close()
        except OSError:
            pass


def parse_command_output(transcript: str, start_marker: str, end_marker: str) -> tuple[str, int]:
    """Pure parsing, unit-tested directly: recover a command's own stdout (and
    exit code) from a raw serial transcript that may also contain the shell's
    echo of the typed command line. Only a line that is *exactly* the marker
    is the marker's own `echo` output; a typed/echoed input line carries the
    rest of the wrapped command around it and is never mistaken for it."""
    start_match = re.search(rf"^{re.escape(start_marker)}\s*$", transcript, re.MULTILINE)
    end_match = re.search(rf"^{re.escape(end_marker)}\s+(-?\d+)\s*$", transcript, re.MULTILINE)
    if not start_match or not end_match or end_match.start() < start_match.end():
        return "", -1
    body = transcript[start_match.end():end_match.start()]
    body = body.strip("\r\n")
    return body, int(end_match.group(1))


# ------------------------------------------------------------------ QEMU
def spawn_qemu(iso_path: Path, kernel: Path, initrd: Path, label: str, memory_mb: int) -> subprocess.Popen:
    append = (
        f"root=live:CDLABEL={label} rd.live.dir=LiveOS rd.live.squashimg=rootfs.squashfs rd.overlay rd.synos.live=1 "
        f"console=ttyS0,115200n8 systemd.mask=serial-getty@ttyS0.service systemd.debug_shell=ttyS0 "
        f"quiet systemd.show_status=0"
    )
    argv = [
        "qemu-system-x86_64",
        "-m", str(memory_mb),
        "-smp", "2",
        "-kernel", str(kernel),
        "-initrd", str(initrd),
        "-append", append,
        "-cdrom", str(iso_path),
        "-nographic",
        "-no-reboot",
        "-serial", "stdio",
        "-monitor", "none",
        "-net", "none",
        "-machine", "accel=kvm:tcg",
    ]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    except OSError:
        pass


def spawn_qemu_graphical(iso_path: Path, kernel: Path, initrd: Path, label: str, memory_mb: int,
                          qmp_sock: Path) -> subprocess.Popen:
    """Boot the real graphical path: no console=ttyS0, no debug shell, and
    `quiet splash` (build.sh's own "Try" GRUB entry, LIVE_BOOT_ARGS plus
    `quiet splash`) so systemd's scrolling status text is replaced by the
    branded Plymouth splash a person actually sees, not an artifact of
    this check leaving boot messages on that a real boot hides. `-vga std`
    keeps a plain, universally supported framebuffer device; `-display
    none` opens no window (nothing here needs one) but the device still
    renders into its emulated video RAM, which `-qmp`'s screendump command
    reads directly -- see QmpSession."""
    append = (f"root=live:CDLABEL={label} rd.live.dir=LiveOS rd.live.squashimg=rootfs.squashfs "
              f"rd.overlay rd.synos.live=1 quiet splash")
    argv = [
        "qemu-system-x86_64",
        "-m", str(memory_mb),
        "-smp", "2",
        "-kernel", str(kernel),
        "-initrd", str(initrd),
        "-append", append,
        "-cdrom", str(iso_path),
        "-vga", "std",
        "-display", "none",
        "-qmp", f"unix:{qmp_sock},server,nowait",
        "-no-reboot",
        "-net", "none",
        "-machine", "accel=kvm:tcg",
    ]
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class QmpSession:
    """A minimal client for QEMU's JSON monitor protocol (QMP) over the unix
    socket spawn_qemu_graphical opens with -qmp. Only what the graphical
    check needs: connect, do the capabilities handshake, and issue
    screendump. QMP (not the legacy human-readable monitor) because its
    replies are structured JSON, not a prompt to screen-scrape."""

    def __init__(self, sock_path: Path, timeout: float = 30):
        deadline = time.monotonic() + timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self.sock.connect(str(sock_path))
                break
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_exc = exc
                time.sleep(0.2)
        else:
            raise QemuBootTimeout(f"QEMU's QMP socket never appeared at {sock_path}: {last_exc}")
        self._buf = b""
        self._read_json()  # the greeting banner
        self._command({"execute": "qmp_capabilities"})

    def _read_json(self) -> dict:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise QemuBootTimeout("QEMU's QMP connection closed unexpectedly")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return json.loads(line.decode("utf-8"))

    def _command(self, command: dict) -> dict:
        self.sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
        while True:
            response = self._read_json()
            if "return" in response or "error" in response:
                return response
            # else: an asynchronous event line (e.g. RESUME) -- not a reply, keep reading

    def screendump(self, path: Path) -> None:
        response = self._command({"execute": "screendump", "arguments": {"filename": str(path)}})
        if "error" in response:
            raise QemuBootTimeout(f"QMP screendump failed: {response['error']}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def wait_for_settled_screen(qmp: QmpSession, proc: subprocess.Popen, workdir: Path, timeout: float,
                             poll_interval: float = GRAPHICAL_POLL_INTERVAL,
                             stable_samples: int = GRAPHICAL_STABLE_SAMPLES) -> tuple[Path, bool, float]:
    """Poll screendump instead of sleeping a fixed time: hash each capture,
    and once `stable_samples` consecutive captures hash identically, the
    screen has stopped changing -- boot messages, a splash animation or a
    session loading all keep the framebuffer changing, so stability is a
    real signal that it finished, not a guess at how long that takes.

    A screen that never changes at all is not the same thing: a hang on a
    black or frozen frame is also "stable" from frame one, and must not be
    mistaken for a session that settled after loading. settled therefore
    also requires at least one real change from the very first capture.

    Returns (path to the last PPM captured, whether it actually settled,
    seconds elapsed) -- never raises for an ordinary timeout, only if QEMU
    itself dies or the QMP link breaks."""
    start = time.monotonic()
    ppm_path = workdir / "screendump.ppm"
    first_digest: str | None = None
    changed_since_start = False
    last_digest: str | None = None
    stable_count = 0
    while True:
        if proc.poll() is not None:
            raise QemuBootTimeout(f"qemu exited (code {proc.returncode}) before the screen settled")
        qmp.screendump(ppm_path)
        digest = hashlib.sha256(ppm_path.read_bytes()).hexdigest()
        if first_digest is None:
            first_digest = digest
        elif digest != first_digest:
            changed_since_start = True
        stable_count = stable_count + 1 if digest == last_digest else 1
        last_digest = digest
        elapsed = time.monotonic() - start
        if changed_since_start and stable_count >= stable_samples:
            return ppm_path, True, elapsed
        if elapsed >= timeout:
            return ppm_path, False, elapsed
        time.sleep(poll_interval)


# ------------------------------------------------------------ assertions
def parse_is_active(output: str) -> str:
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def wait_default_target(session: SerialSession, timeout: float) -> dict:
    target_out, code = session.run("systemctl get-default", DEFAULT_COMMAND_TIMEOUT)
    target = parse_is_active(target_out) or "graphical.target"
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        out, _ = session.run(f"systemctl is-active {shlex.quote(target)}", DEFAULT_COMMAND_TIMEOUT)
        last_state = parse_is_active(out)
        if last_state == "active":
            return {"name": "default-target", "passed": True, "target": target}
        time.sleep(3)
    return {"name": "default-target", "passed": False, "target": target, "last_state": last_state}


def parse_ufw_allow_ports(script_text: str) -> list[str]:
    """The ports the shipped first-boot script (base_hardening role) would
    open, normalized to "<port>/<proto>". Pure and fixture-tested."""
    ports = []
    for match in re.finditer(r"ufw allow ([0-9]{1,5}(?:/(?:tcp|udp))?)", script_text):
        port = match.group(1)
        ports.append(port if "/" in port else f"{port}/tcp")
    return ports


def normalize_ports(ports: list[str]) -> list[str]:
    return sorted(p if "/" in p else f"{p}/tcp" for p in ports)


def check_open_ports(session: SerialSession, expected_ports: list[str]) -> dict:
    script_path = "/usr/libexec/synos-first-boot-services"
    out, code = session.run(f"cat {script_path} 2>/dev/null", DEFAULT_COMMAND_TIMEOUT)
    if code != 0 and not expected_ports:
        return {"name": "open-ports", "passed": True, "expected": [], "found": [],
                "note": "no security.open_ports and no first-boot script shipped"}
    shipped = normalize_ports(parse_ufw_allow_ports(out))
    expected = normalize_ports(expected_ports)
    return {
        "name": "open-ports", "passed": shipped == expected, "expected": expected, "found": shipped,
        "note": ("checks the shipped first-boot firewall script (ufw allow lines), not live enforcement: "
                 "ports open only on an installed system's real first boot, never inside the live session "
                 "this smoke test boots (ConditionKernelCommandLine=!rd.synos.live)"),
    }


def parse_is_enabled(output: str, exit_code: int) -> str:
    return parse_is_active(output) if output.strip() else ("" if exit_code == 0 else "not-found")


def check_service_unit(session: SerialSession, name: str) -> dict:
    """A quadlet unit (/etc/containers/systemd/<name>.container) is translated to
    a runtime <name>.service by systemd's quadlet generator at boot; this checks
    that translation happened and the unit is enabled, not that the container's
    image was pulled (which needs the network)."""
    out, code = session.run(f"systemctl is-enabled {shlex.quote(name)}.service", DEFAULT_COMMAND_TIMEOUT)
    state = parse_is_enabled(out, code)
    passed = state in ("enabled", "enabled-runtime", "static", "alias", "generated")
    return {
        "name": f"service-unit:{name}", "passed": passed, "state": state,
        "note": ("checks that the quadlet unit was translated and is enabled (\"generated\" is systemd's own "
                 "state for a quadlet-produced unit), not that the container image was pulled or the service "
                 "is actually running: that needs the network, which this session does not have"),
    }


def parse_stat_line(output: str, exit_code: int) -> tuple[str, str] | None:
    if exit_code != 0:
        return None
    parts = output.strip().split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def check_shipped_file(session: SerialSession, path: str, expected_mode: str) -> dict:
    out, code = session.run(f"stat -c '%a %s' {shlex.quote(path)} 2>/dev/null", DEFAULT_COMMAND_TIMEOUT)
    stat = parse_stat_line(out, code)
    if stat is None:
        return {"name": f"file:{path}", "passed": False, "reason": "missing or unreadable"}
    mode, size = stat
    try:
        mode_ok = int(mode, 8) == int(expected_mode, 8)
    except ValueError:
        mode_ok = mode == expected_mode
    return {"name": f"file:{path}", "passed": mode_ok, "mode": mode, "expected_mode": expected_mode, "size": int(size)}


# --------------------------------------------------------- graphical check
def ocr_text(image_path: Path) -> str:
    """Recognized text off a screenshot, via the tesseract CLI directly --
    that binary, not any particular Python OCR binding, is what is
    guaranteed to be on this machine. `stdout` as the output base tells
    tesseract to print the text instead of writing a .txt file."""
    result = subprocess.run(["tesseract", str(image_path), "stdout"], capture_output=True, text=True, check=False)
    return result.stdout


def normalize_for_match(text: str) -> str:
    """Case- and whitespace-insensitive: OCR line-wraps and spaces text in
    ways that do not matter for "is this word on the screen"."""
    return re.sub(r"\s+", " ", text).strip().lower()


def evaluate_graphical_screenshot(ocr_output: str, expected_name: str | None, settled: bool,
                                   settle_timeout: float) -> dict:
    """Pure judgement over already-recognized OCR text plus the settle
    result: no QEMU, no file I/O. Split out of check_graphical_boot so the
    matching rule (what counts as "certified" vs. each distinct way it can
    fall short) is testable against fixed screenshots' OCR output directly,
    without booting anything. Returns outcome/passed/name_found/
    installer_found/note; check_graphical_boot adds the boot-specific
    fields (screenshot path and checksum, settle_seconds, ...)."""
    normalized = normalize_for_match(ocr_output)
    has_visible_content = bool(normalized)
    installer_found = "installer" in normalized
    name_found = (normalize_for_match(expected_name) in normalized) if expected_name else None

    if not settled:
        outcome = "not-settled"
        note = (f"the screen never stopped changing within {settle_timeout:.0f}s; the session may still be "
                "loading (try a longer --graphical-timeout), or the boot is hung -- see the screenshot")
    elif not has_visible_content:
        outcome = "blank-or-unreadable-screen"
        note = "the screen stopped changing but tesseract read no text at all off it; check the screenshot directly"
    elif expected_name is None:
        outcome = "no-expected-name"
        note = ("no distribution name was available (no resolved configuration and no --brand); only checked "
                "that \"Installer\" is on screen")
    elif not name_found:
        outcome = "name-not-found"
        note = (f"tesseract did not find {expected_name!r} on screen; the session clearly came up (the screen "
                "settled and text was readable) but this specific claim could not be verified -- could be a "
                "real branding bug or an OCR miss, check the screenshot")
    elif not installer_found:
        outcome = "installer-word-not-found"
        note = f"{expected_name!r} was found but not the word \"Installer\"; check the screenshot"
    else:
        outcome = "certified"
        note = "the live session came up and OCR read both the distribution's name and \"Installer\" on the first screen"

    return {
        "passed": outcome == "certified", "outcome": outcome,
        "name_found": name_found, "installer_found": installer_found, "note": note,
    }


def check_graphical_boot(iso_path: Path, kernel: Path, initrd: Path, label: str, *, output_dir: Path,
                          expected_name: str | None, memory_mb: int = DEFAULT_GRAPHICAL_MEMORY_MB,
                          settle_timeout: float = DEFAULT_GRAPHICAL_SETTLE_TIMEOUT,
                          poll_interval: float = GRAPHICAL_POLL_INTERVAL,
                          stable_samples: int = GRAPHICAL_STABLE_SAMPLES) -> dict:
    """Boot the real graphical path, capture the first screen once it stops
    changing, and certify what a person actually sees: the distribution's
    own name (expected_name, read from the resolved configuration the same
    way check_open_ports' expectation is, not hard-coded) and the word
    "Installer", both recovered by OCR. Never raises: qemu/tooling problems,
    a hung boot and a genuinely wrong screen all come back as this check's
    own "outcome", not a partial result or a silent pass.

    "outcome" is the point of this check, more than "passed": a session
    that clearly came up but whose name OCR could not read is a materially
    different finding from a boot that never got anywhere, and the report
    says which one happened rather than collapsing both into one boolean."""
    available, reason = graphical_tools_available()
    if not available:
        return {"name": "graphical-boot", "passed": None, "outcome": "skipped", "note": reason}

    from PIL import Image

    screenshot_path = output_dir / "smoke-screenshot.png"
    proc: subprocess.Popen | None = None
    qmp: QmpSession | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="synos-smoke-gfx-") as raw_tmp:
            tmp = Path(raw_tmp)
            qmp_sock = tmp / "qmp.sock"
            proc = spawn_qemu_graphical(iso_path, kernel, initrd, label, memory_mb, qmp_sock)
            qmp = QmpSession(qmp_sock, timeout=30)
            ppm_path, settled, elapsed = wait_for_settled_screen(
                qmp, proc, tmp, settle_timeout, poll_interval, stable_samples)
            Image.open(ppm_path).save(screenshot_path, "PNG")
    except QemuBootTimeout as exc:
        return {"name": "graphical-boot", "passed": False, "outcome": "error", "note": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never let a smoke-test bug fail the whole matrix run
        return {"name": "graphical-boot", "passed": False, "outcome": "error",
                "note": f"{type(exc).__name__}: {exc}"}
    finally:
        if qmp is not None:
            qmp.close()
        if proc is not None:
            terminate(proc)

    screenshot_sha256 = hashlib.sha256(screenshot_path.read_bytes()).hexdigest()
    text = ocr_text(screenshot_path)
    verdict = evaluate_graphical_screenshot(text, expected_name, settled, settle_timeout)

    return {
        "name": "graphical-boot",
        **verdict,
        "settled": settled,
        "settle_seconds": round(elapsed, 1),
        "screenshot": str(screenshot_path),
        "screenshot_sha256": screenshot_sha256,
        "expected_name": expected_name,
        "ocr_text": text.strip(),
    }


# ------------------------------------------------------- profile resolution
def resolved_json_path(iso_path: Path) -> Path:
    """dist/<name>.resolved.json sits next to dist/<name>.iso (build.sh,
    "Write the package lock and SBOM" — both share the same stem)."""
    return iso_path.with_suffix(".resolved.json")


def load_resolved_profile(resolved_path: Path) -> dict:
    """The resolved profile (security.open_ports, software.services/files,
    already `extends`-merged) out of an engine-written dist/<name>.resolved.json."""
    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    return data.get("profile", {}) or {}


def load_resolved_brand_name(resolved_path: Path) -> str | None:
    """The distribution's display name (branding/<id>/brand.yml's
    display_name, carried into dist/<name>.resolved.json's "brand" key) --
    what the live session's first screen is expected to show, e.g.
    "SynOS NGINX". None if the field is missing or empty."""
    data = json.loads(resolved_path.read_text(encoding="utf-8"))
    name = (data.get("brand", {}) or {}).get("display_name")
    return str(name).strip() or None if name else None


def load_brand_display_name(brand_path: Path) -> str | None:
    """Fallback source for the same name, for when no resolved.json exists:
    read display_name straight out of a brand.yml file (a bundle's own
    branding/<id>/brand.yml -- not necessarily under this checkout's own
    branding/, the same situation resolve_profile_chain handles for a leaf
    profile). No GRUB-safety validation like render_manifest.resolve_brand
    does: OCR only needs the text, not a name GRUB can render."""
    sys.path.insert(0, str(ROOT / "tools"))
    import render_manifest  # noqa: E402

    data = render_manifest.load_yaml(brand_path)
    name = data.get("display_name")
    return str(name).strip() or None if name else None


def resolve_profile_chain(profile_path: Path) -> tuple[dict, list[str]]:
    """Resolve a leaf profile's own `extends` chain, for when no
    resolved.json is available. profile_path is a bundle's own
    profiles/<id>.yml — not necessarily under this checkout's profiles/ —
    so only its `extends` parent (a catalog "kind": minimal, server, ...) is
    looked up there, via render_manifest.resolve_profile, the same way
    tools/build_matrix.py's _resolve_bundle_profile does for a bundle-catalog
    entry. Returns (merged profile, chain of profile ids, leaf first... last)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import render_manifest  # noqa: E402

    data = render_manifest.load_yaml(profile_path)
    own_id = data.get("id") or profile_path.stem
    body = {k: v for k, v in data.items() if k not in {"id", "extends", "description"}}
    parent = data.get("extends")
    if not parent:
        return body, [own_id]
    parent_data, parent_chain = render_manifest.resolve_profile(parent)
    return render_manifest.deep_merge(parent_data, body), parent_chain + [own_id]


# --------------------------------------------------------------- runner
def overall_passed(checks: list[dict]) -> bool:
    """A run's overall pass/fail: every check must not have failed.
    "passed": None (only the graphical check uses it, when its own extra
    tools are missing) is a skip, not a failure, and must not drag an
    otherwise-clean run down to "failed" -- consistent with every other
    skip in this module never being reported as one."""
    return bool(checks) and all(check.get("passed") is not False for check in checks)


def run(iso_path: Path, resolved_profile: dict, *, output_dir: Path,
        memory_mb: int = DEFAULT_MEMORY_MB, boot_timeout: float = DEFAULT_BOOT_TIMEOUT,
        target_timeout: float = DEFAULT_TARGET_TIMEOUT, profile_source: str = "unspecified",
        graphical: bool = True, expected_distro_name: str | None = None,
        graphical_memory_mb: int = DEFAULT_GRAPHICAL_MEMORY_MB,
        graphical_settle_timeout: float = DEFAULT_GRAPHICAL_SETTLE_TIMEOUT,
        graphical_poll_interval: float = GRAPHICAL_POLL_INTERVAL,
        graphical_stable_samples: int = GRAPHICAL_STABLE_SAMPLES) -> dict:
    """Boot iso_path headless and check it against resolved_profile (the profile
    dict after tools/render_manifest.py's `extends` merge). profile_source is
    carried through into the result as-is, purely for the report: it says
    nothing about correctness, only where resolved_profile came from. Never
    raises for an ordinary boot or assertion failure: those come back as
    status "failed" with the checks that ran. Only a missing qemu/xorriso is
    "skipped".

    When graphical is true (the default), a second, separate boot follows
    the headless one: see check_graphical_boot. It is scored like any other
    check (its own "passed"), except a "skipped" graphical check (its own
    tools missing) never drags the overall run down to "failed" -- a skip
    is not a failure, here or anywhere else in this module."""
    available, reason = tools_available()
    if not available:
        return {"status": "skipped", "reason": reason, "profile_source": profile_source}
    if not iso_path.is_file():
        return {"status": "skipped", "reason": f"{iso_path} does not exist", "profile_source": profile_source}

    transcript = output_dir / "smoke-serial.log"
    checks: list[dict] = []
    proc: subprocess.Popen | None = None
    session: SerialSession | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="synos-smoke-") as raw_tmp:
            tmp = Path(raw_tmp)
            label = iso_volume_label(iso_path)
            kernel, initrd = extract_boot_files(iso_path, tmp)
            proc = spawn_qemu(iso_path, kernel, initrd, label, memory_mb)
            session = SerialSession(proc, transcript)
            session.wait_for(b"# ", boot_timeout)
            session.run("stty -echo 2>/dev/null", 5)

            checks.append(wait_default_target(session, target_timeout))

            security = resolved_profile.get("security", {}) or {}
            checks.append(check_open_ports(session, list(security.get("open_ports", []) or [])))

            software = resolved_profile.get("software", {}) or {}
            for service in software.get("services", []) or []:
                checks.append(check_service_unit(session, service["name"]))
            for shipped_file in software.get("files", []) or []:
                checks.append(check_shipped_file(session, shipped_file["path"], shipped_file.get("mode", "0644")))

            # Done with the headless debug-shell boot. Close it before the
            # graphical boot starts a second QEMU instance against the same
            # (read-only) ISO, so only one guest is ever running at a time,
            # and reuse the kernel/initrd already extracted into tmp.
            session.close()
            terminate(proc)
            session = None
            proc = None

            if graphical:
                checks.append(check_graphical_boot(
                    iso_path, kernel, initrd, label, output_dir=output_dir,
                    expected_name=expected_distro_name, memory_mb=graphical_memory_mb,
                    settle_timeout=graphical_settle_timeout, poll_interval=graphical_poll_interval,
                    stable_samples=graphical_stable_samples))
    except SmokeTestUnavailable as exc:
        return {"status": "skipped", "reason": str(exc), "profile_source": profile_source}
    except QemuBootTimeout as exc:
        return {"status": "failed", "reason": str(exc), "checks": checks, "transcript": str(transcript),
                "profile_source": profile_source}
    except Exception as exc:  # noqa: BLE001 - never let a smoke-test bug fail the whole matrix run
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", "checks": checks,
                "transcript": str(transcript), "profile_source": profile_source}
    finally:
        if session is not None:
            session.close()
        if proc is not None:
            terminate(proc)

    passed = overall_passed(checks)
    return {"status": "passed" if passed else "failed", "checks": checks, "transcript": str(transcript),
            "profile_source": profile_source}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("iso", type=Path)
    parser.add_argument("--profile", type=Path,
                         help="a bundle's own leaf profile YAML; used to resolve the extends chain only when "
                              "--resolved (or dist/<name>.resolved.json next to the ISO) is not found")
    parser.add_argument("--resolved", type=Path,
                         help="the engine's resolved configuration JSON (dist/<name>.resolved.json); "
                              "auto-discovered next to the ISO when omitted")
    parser.add_argument("--brand", type=Path,
                         help="a bundle's own branding/<id>/brand.yml; used for the graphical check's expected "
                              "distribution name only when --resolved is not found (the resolved configuration "
                              "already carries it otherwise)")
    parser.add_argument("--output", type=Path, default=Path("dist/smoke"))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TARGET_TIMEOUT)
    parser.add_argument("--no-graphical", action="store_true",
                         help="skip the graphical boot / screenshot / OCR check (still run everything else)")
    parser.add_argument("--graphical-timeout", type=float, default=DEFAULT_GRAPHICAL_SETTLE_TIMEOUT,
                         help=f"seconds to wait for the graphical boot's screen to settle "
                              f"(default {DEFAULT_GRAPHICAL_SETTLE_TIMEOUT:.0f}s -- generous for a host with no "
                              f"hardware virtualization, which must still work, just slower)")
    args = parser.parse_args(argv)

    resolved_profile: dict = {}
    expected_distro_name: str | None = None
    resolved_from = args.resolved if args.resolved is not None else resolved_json_path(args.iso)
    if resolved_from.is_file():
        resolved_profile = load_resolved_profile(resolved_from)
        expected_distro_name = load_resolved_brand_name(resolved_from)
        profile_source = f"resolved configuration: {resolved_from}"
    elif args.profile:
        resolved_profile, chain = resolve_profile_chain(args.profile)
        profile_source = f"profile extends chain resolved from {args.profile} (chain: {' -> '.join(chain)})"
        if args.brand and args.brand.is_file():
            expected_distro_name = load_brand_display_name(args.brand)
    elif args.resolved is not None:
        profile_source = f"--resolved {args.resolved} does not exist and no --profile was given; no expectations to check"
    else:
        profile_source = "no --resolved, no dist/<name>.resolved.json next to the ISO, and no --profile; no expectations to check"

    args.output.mkdir(parents=True, exist_ok=True)
    result = run(args.iso, resolved_profile, output_dir=args.output, target_timeout=args.timeout,
                 profile_source=profile_source, graphical=not args.no_graphical,
                 expected_distro_name=expected_distro_name, graphical_settle_timeout=args.graphical_timeout)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("passed", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
