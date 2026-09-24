#!/usr/bin/env python3
"""status_uploader — carries tools/build_status.py's build-status.json to
wherever the Studio page's own web server can serve it from, since the
owner is willing to hand the daily build service upload credentials rather
than move the file by hand.

Configuration lives under an `upload:` section of the same conformance.yml
tools/catalog_conformance.py already reads — never committed to this
repository (see packaging/catalog-conformance/conformance.example.yml,
which the real, private config is copied from and filled in outside any
checkout)::

    upload:
      protocol: sftp            # sftp | scp | ftps | ftp (ftp refused unless allow_insecure_ftp: true)
      host: studio.example
      port: 22                  # optional; each protocol's own default otherwise
      username: builder
      password: null            # ssh: with key_path unset, or alongside it; ftps/ftp: the account's password
      key_path: /etc/synos/conformance-upload-key   # ssh only; a private key, unencrypted, owned by the service user
      remote_path: /var/www/studio/data/build-status.json
      allow_insecure_ftp: false
      retries: 3
      retry_backoff_s: 5

SSH transport shells out to the system `scp`/`sftp` binaries (this
project's own rule: never build a shell command from a name read out of a
file, always spawn from an argument vector — the same reason
tools/devtools_browser.py spawns Chrome directly rather than going through
a shell). Password-authenticated SSH additionally requires `sshpass` on
PATH: the password is handed to it through the SSHPASS environment
variable, never as a command-line argument (an argv is visible to every
local user via `ps`; an environment variable of a short-lived child
process is a narrower exposure, and the accepted way to script this
without an interactive prompt) and never through any other channel. FTPS
uses the standard library's own `ftplib.FTP_TLS` (`ftp` — plain,
unencrypted FTP — uses `ftplib.FTP`, refused by parse_upload_config()
unless the config says `allow_insecure_ftp: true`, because both the
credentials and the file travel in clear text otherwise; only use it
against a server with no TLS support you already trust for other reasons).

No credential, from any protocol, is ever put into an exception message,
a log line, this tool's own print output, or a report — `_redact()` scrubs
a configured password out of anything a transport raises before it
propagates, `describe_destination()` is the only representation of a
destination that is ever safe to print, and dry-run mode calls nothing
that could print anything else.

Standalone use, to check a config before wiring it into a real run, or to
check a file actually arrived after one::

    python3 tools/status_uploader.py --config conformance.yml --file /var/lib/synos-conformance/build-status.json
    python3 tools/status_uploader.py --config conformance.yml --file /var/lib/synos-conformance/build-status.json --send

Prints what it would send where by default; `--send` performs one real
upload attempt (no retry) so its result is unambiguous, then says whether
it succeeded — the way to check the file actually arrived, alongside
whatever the server itself offers (an HTTP HEAD on the deployed URL, an
`ls` over the same SSH credentials).
"""
from __future__ import annotations

import argparse
import dataclasses
import ftplib
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

ALLOWED_PROTOCOLS = {"sftp", "scp", "ftps", "ftp"}
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 5.0
DEFAULT_TIMEOUT_S = 30.0


class UploadConfigError(Exception):
    """The upload: section of the config is unusable. Never mentions a
    credential value — only ever which key is missing, malformed, or which
    protocol is unsupported or refused."""


class UploadTransportError(Exception):
    """One transport attempt failed. Every raiser in this module redacts
    the configured password out of its own message before raising."""


@dataclasses.dataclass
class UploadConfig:
    protocol: str
    host: str
    remote_path: str
    port: int | None = None
    username: str | None = None
    password: str | None = None
    key_path: str | None = None
    allow_insecure_ftp: bool = False
    retries: int = DEFAULT_RETRIES
    retry_backoff_s: float = DEFAULT_BACKOFF_S
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclasses.dataclass
class UploadResult:
    ok: bool
    detail: str  # safe to print or put in a report; never contains a secret


def parse_upload_config(data: dict | None) -> UploadConfig | None:
    """None (no `upload:` section at all) means the uploader is simply not
    configured — not an error, since it is explicitly optional (item 73)."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise UploadConfigError("upload: must be a mapping")
    known = {f.name for f in dataclasses.fields(UploadConfig)}
    unknown = set(data) - known
    if unknown:
        raise UploadConfigError(f"upload: unknown key(s): {', '.join(sorted(unknown))}")
    protocol = data.get("protocol")
    if protocol not in ALLOWED_PROTOCOLS:
        raise UploadConfigError(f"upload.protocol must be one of {sorted(ALLOWED_PROTOCOLS)}, got {protocol!r}")
    if protocol == "ftp" and not data.get("allow_insecure_ftp"):
        raise UploadConfigError(
            "upload.protocol: ftp is plain, unencrypted FTP - the account's credentials and the file itself "
            "travel in the clear over the network. Refused unless upload.allow_insecure_ftp: true says the "
            "owner has explicitly accepted that (docs/BUILD_MATRIX.md explains why); use ftps instead on any "
            "server that supports it.")
    if not data.get("host"):
        raise UploadConfigError("upload.host is required")
    if not data.get("remote_path"):
        raise UploadConfigError("upload.remote_path is required")
    return UploadConfig(
        protocol=protocol,
        host=data["host"],
        remote_path=data["remote_path"],
        port=data.get("port"),
        username=data.get("username"),
        password=data.get("password"),
        key_path=data.get("key_path"),
        allow_insecure_ftp=bool(data.get("allow_insecure_ftp", False)),
        retries=int(data.get("retries", DEFAULT_RETRIES)),
        retry_backoff_s=float(data.get("retry_backoff_s", DEFAULT_BACKOFF_S)),
        timeout_s=float(data.get("timeout_s", DEFAULT_TIMEOUT_S)),
    )


def describe_destination(config: UploadConfig) -> str:
    """The only representation of a destination this module ever prints or
    reports — protocol, user, host, port, remote path; never a password or
    a key's contents."""
    who = f"{config.username}@" if config.username else ""
    port = f":{config.port}" if config.port else ""
    return f"{config.protocol}://{who}{config.host}{port}{config.remote_path}"


def _redact(text: str, config: UploadConfig) -> str:
    if config.password:
        text = text.replace(config.password, "***")
    return text


def _run_scp(config: UploadConfig, local_path: Path) -> None:
    if config.password and not shutil.which("sshpass"):
        raise UploadTransportError(
            "password-authenticated scp needs 'sshpass' on PATH (see tools/status_uploader.py's own docstring); "
            "install it, or use upload.key_path instead")
    target = f"{config.username}@{config.host}:{config.remote_path}" if config.username else f"{config.host}:{config.remote_path}"
    argv = ["scp", "-o", "StrictHostKeyChecking=accept-new"]
    if config.port:
        argv += ["-P", str(config.port)]
    if config.key_path:
        argv += ["-i", config.key_path]
    env = os.environ.copy()
    if config.password:
        argv = ["sshpass", "-e"] + argv
        env["SSHPASS"] = config.password
    argv += [str(local_path), target]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=config.timeout_s, env=env)  # noqa: S603
    if proc.returncode != 0:
        raise UploadTransportError(_redact((proc.stderr or proc.stdout or f"scp exited {proc.returncode}").strip(), config))


def _run_sftp(config: UploadConfig, local_path: Path) -> None:
    if config.password and not shutil.which("sshpass"):
        raise UploadTransportError(
            "password-authenticated sftp needs 'sshpass' on PATH (see tools/status_uploader.py's own docstring); "
            "install it, or use upload.key_path instead")
    target = f"{config.username}@{config.host}" if config.username else config.host
    argv = ["sftp", "-o", "StrictHostKeyChecking=accept-new"]
    if config.port:
        argv += ["-P", str(config.port)]
    if config.key_path:
        argv += ["-i", config.key_path]
    argv += ["-b", "-", target]
    env = os.environ.copy()
    if config.password:
        argv = ["sshpass", "-e"] + argv
        env["SSHPASS"] = config.password
    batch = f"put {local_path} {config.remote_path}\n"
    proc = subprocess.run(argv, input=batch, capture_output=True, text=True,  # noqa: S603
                          timeout=config.timeout_s, env=env)
    if proc.returncode != 0:
        raise UploadTransportError(_redact((proc.stderr or proc.stdout or f"sftp exited {proc.returncode}").strip(), config))


def _run_ftp(config: UploadConfig, local_path: Path, *, secure: bool) -> None:
    ftp_cls = ftplib.FTP_TLS if secure else ftplib.FTP
    ftp = ftp_cls(timeout=config.timeout_s)
    try:
        ftp.connect(config.host, config.port or 21)
        ftp.login(config.username or "anonymous", config.password or "")
        if secure:
            ftp.prot_p()
        remote_dir = str(PurePosixPath(config.remote_path).parent)
        remote_name = PurePosixPath(config.remote_path).name
        if remote_dir not in ("", "."):
            ftp.cwd(remote_dir)
        with local_path.open("rb") as handle:
            ftp.storbinary(f"STOR {remote_name}", handle)
    except ftplib.all_errors as exc:
        raise UploadTransportError(_redact(str(exc), config)) from exc
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001 - closing the connection must never mask the real error
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass


def _default_transport(config: UploadConfig, local_path: Path) -> None:
    if config.protocol == "scp":
        _run_scp(config, local_path)
    elif config.protocol == "sftp":
        _run_sftp(config, local_path)
    elif config.protocol == "ftps":
        _run_ftp(config, local_path, secure=True)
    elif config.protocol == "ftp":
        _run_ftp(config, local_path, secure=False)
    else:  # pragma: no cover - parse_upload_config already refuses anything else
        raise UploadTransportError(f"unknown protocol {config.protocol!r}")


def upload_file(config: UploadConfig, local_path: Path, *, dry_run: bool = False,
                transport=None, sleeper=None) -> UploadResult:
    """Never raises: a failed upload is a warning the caller decides what
    to do with, never an exception that could take a build run down with
    it. Retries `config.retries` times total (so 1 means one attempt, no
    retry) with a linearly increasing backoff between attempts."""
    destination = describe_destination(config)
    if dry_run:
        size = local_path.stat().st_size if local_path.is_file() else None
        size_text = f"{size} byte(s)" if size is not None else "no such file yet"
        return UploadResult(True, f"[dry-run] would upload {local_path.name} ({size_text}) to {destination}")

    transport = transport or _default_transport
    sleeper = sleeper or time.sleep
    attempts = max(1, config.retries)
    last_error = "unknown error"
    for attempt in range(1, attempts + 1):
        try:
            transport(config, local_path)
            return UploadResult(True, f"uploaded {local_path.name} to {destination}")
        except Exception as exc:  # noqa: BLE001 - a transport failure is reported, never fatal here
            last_error = _redact(str(exc), config)
            if attempt < attempts:
                sleeper(config.retry_backoff_s * attempt)
    return UploadResult(False, f"could not upload {local_path.name} to {destination} after {attempts} attempt(s): {last_error}")


class ChangeUploader:
    """Wraps upload_file() with the item-73 policy: called once per entry
    whose state changed, plus once, unconditionally, at the end of a run.
    Thread-safe (tools/catalog_conformance.py's parallel build path calls
    `on_change` from worker threads) purely by virtue of upload_file()
    itself touching no shared state; the lock here only serializes so two
    workers' uploads of the same file do not race each other's retries."""

    def __init__(self, config: UploadConfig | None, *, dry_run: bool = False, transport=None, sleeper=None):
        self.config = config
        self.dry_run = dry_run
        self.transport = transport
        self.sleeper = sleeper
        self.warnings: list[str] = []
        self.attempts: list[UploadResult] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def maybe_upload(self, local_path: Path) -> UploadResult | None:
        if not self.enabled:
            return None
        with self._lock:
            result = upload_file(self.config, local_path, dry_run=self.dry_run,
                                 transport=self.transport, sleeper=self.sleeper)
            self.attempts.append(result)
            if not result.ok:
                self.warnings.append(result.detail)
            return result


def _eprint(message: str) -> None:
    """print(..., file=sys.stderr) that cannot itself raise: a caller with
    a closed or redirected stderr (a test capturing output, a service
    manager that already tore its pipe down) must never turn an error
    message into a crash of its own."""
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001 - reporting an error must never itself raise
        pass


def _cli_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="status_uploader",
        description="Check an upload: config, and optionally send one file with it, without running a build.")
    parser.add_argument("--config", type=Path, required=True, help="the conformance.yml holding the upload: section")
    parser.add_argument("--file", type=Path, required=True, help="the local file to send (e.g. build-status.json)")
    parser.add_argument("--send", action="store_true", help="actually upload (default: dry-run, prints the destination only)")
    args = parser.parse_args(argv)

    import yaml
    data = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    try:
        config = parse_upload_config(data.get("upload"))
    except UploadConfigError as exc:
        _eprint(f"error: {exc}")
        return 2
    if config is None:
        _eprint("no upload: section in this config - nothing to check")
        return 1

    result = upload_file(config, args.file, dry_run=not args.send)
    print(result.detail)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(_cli_main())
