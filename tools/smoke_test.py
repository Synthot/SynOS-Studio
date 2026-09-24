#!/usr/bin/env python3
"""smoke_test — boot a built ISO headless in QEMU and check that the
appliance it promised is real.

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

What it asserts, over a root shell on the serial console:
  - the live system reaches its default systemd target
  - the ports opened by the shipped first-boot firewall script
    (/usr/libexec/synos-first-boot-services) match the profile's
    security.open_ports exactly
  - every software.services entry has a quadlet-generated systemd unit
    that exists and is enabled
  - every software.files entry is present with the mode the profile asked for

The firewall check is honest about its own limit: base_hardening's
first-boot service carries `ConditionKernelCommandLine=!rd.synos.live`, so
the ports are never actually opened inside the live session that boots
here — only on an installed system's real first boot. What is checked is
that the shipped script *would* open exactly the promised ports, not that
they are open right now. Nothing that would need a network or credentials
(an installed system, a signed-in package mirror) is asserted; where the
matrix cannot check something honestly it is left out, not invented.

Requires qemu-system-x86_64 and xorriso on PATH. Their absence is a clean
skip, never a failure: run() returns {"status": "skipped", "reason": ...}.
Nothing here touches the network: the live kernel and initrd are pulled out
of the ISO with `xorriso -osirrox` and booted directly (-kernel/-initrd),
bypassing the graphical GRUB menu, with a root debug shell on the serial
console (console=ttyS0, systemd.debug_shell=ttyS0, serial-getty masked) —
the same recipe tests/framework/grub.py's debug_kernel_arguments uses for
the full acceptance suite, applied here without that suite's GRUB-menu and
screenshot machinery, which this headless smoke test does not need.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
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
def run(iso_path: Path, resolved_profile: dict, *, output_dir: Path,
        memory_mb: int = DEFAULT_MEMORY_MB, boot_timeout: float = DEFAULT_BOOT_TIMEOUT,
        target_timeout: float = DEFAULT_TARGET_TIMEOUT, profile_source: str = "unspecified") -> dict:
    """Boot iso_path headless and check it against resolved_profile (the profile
    dict after tools/render_manifest.py's `extends` merge). profile_source is
    carried through into the result as-is, purely for the report: it says
    nothing about correctness, only where resolved_profile came from. Never
    raises for an ordinary boot or assertion failure: those come back as
    status "failed" with the checks that ran. Only a missing qemu/xorriso is
    "skipped"."""
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

    passed = bool(checks) and all(check.get("passed") for check in checks)
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
    parser.add_argument("--output", type=Path, default=Path("dist/smoke"))
    parser.add_argument("--timeout", type=float, default=DEFAULT_TARGET_TIMEOUT)
    args = parser.parse_args(argv)

    resolved_profile: dict = {}
    resolved_from = args.resolved if args.resolved is not None else resolved_json_path(args.iso)
    if resolved_from.is_file():
        resolved_profile = load_resolved_profile(resolved_from)
        profile_source = f"resolved configuration: {resolved_from}"
    elif args.profile:
        resolved_profile, chain = resolve_profile_chain(args.profile)
        profile_source = f"profile extends chain resolved from {args.profile} (chain: {' -> '.join(chain)})"
    elif args.resolved is not None:
        profile_source = f"--resolved {args.resolved} does not exist and no --profile was given; no expectations to check"
    else:
        profile_source = "no --resolved, no dist/<name>.resolved.json next to the ISO, and no --profile; no expectations to check"

    args.output.mkdir(parents=True, exist_ok=True)
    result = run(args.iso, resolved_profile, output_dir=args.output, target_timeout=args.timeout,
                 profile_source=profile_source)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("passed", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
