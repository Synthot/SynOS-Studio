#!/usr/bin/env python3
"""status_uploader — carries tools/build_status.py's build-status.json to
wherever a Studio page's own web server can serve it from, since the owner
is willing to hand the daily build service upload credentials rather than
move the file by hand.

Configuration lives under an `upload:` section of the same conformance.yml
tools/catalog_conformance.py already reads — never committed to this
repository (see packaging/catalog-conformance/conformance.example.yml,
which the real, private config is copied from and filled in outside any
checkout). `upload:` is one destination (a mapping, the original shape) or
several (a list of mappings, each named) — parse_upload_destinations()
reads either shape into an ordered list; parse_upload_config() is the
original single-mapping parser, kept for exactly the callers (and tests)
that only ever had one destination::

    upload:
      protocol: sftp            # sftp | scp | ftps | ftp | local (ftp refused unless allow_insecure_ftp: true)
      host: studio.example
      port: 22                  # optional; each protocol's own default otherwise
      username: builder
      password: null            # ssh: with key_path unset, or alongside it; ftps/ftp: the account's password
      key_path: /etc/synos/conformance-upload-key   # ssh only; a private key, unencrypted, owned by the service user
      remote_path: /var/www/studio/data/build-status.json
      allow_insecure_ftp: false
      retries: 3
      retry_backoff_s: 5

Or, several destinations, in order — used to rehearse a publish against a
local development copy of the Studio page before it ever reaches
production (FanOutUploader, below; tools/catalog_conformance.py wires this
in as the build run's own upload path)::

    upload:
      - name: dev-site
        protocol: local          # not a network at all: a plain file copy
        remote_path: /var/www/dev-studio/data/build-status.json
        owner: www-data:www-data  # optional; chown after copy (best-effort, never fatal)
        mode: "0644"              # optional; chmod after copy
      - name: production
        protocol: sftp
        host: studio.example
        remote_path: /var/www/studio/data/build-status.json
        username: builder
        key_path: /etc/synos/conformance-upload-key

Every `protocol: local` destination is the rehearsal: it is published, and
-- when the run gave FanOutUploader a `verify_url` (tools/catalog_conformance.py
derives this from the same `site_url` the run already has, the page's own
`/data/build-status.json`) -- fetched back over HTTP and checked to
actually be a valid, schema_version 2 document *whose bytes are the exact
file this run just published* (verify_published(), item 201), proving the
*served* copy is good and is genuinely this run's own, not merely that a
write to disk succeeded somewhere, and not merely that whatever answered
that URL happened to parse as a plausible build-status.json (a second web
server or vhost quietly answering the same Host header with a different,
older, but still schema-valid file passed the old check and must not pass
this one). Every non-local ("production") destination is attempted only
once every local destination has passed both steps; a rehearsal failure is
reported against every configured destination (the one that actually
failed with its own reason, every production one "skipped" with that
reason) and never touches a network destination at all.

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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path, PurePosixPath

ALLOWED_PROTOCOLS = {"sftp", "scp", "ftps", "ftp", "local"}
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_S = 5.0
DEFAULT_TIMEOUT_S = 30.0
STATUS_SCHEMA_VERSION = 2  # tools/build_status.py's own SCHEMA_VERSION; not imported (kept dependency-light,
                            # same as this module's original design), duplicated here as a two-line shape check


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
    remote_path: str
    host: str | None = None       # required for every protocol except "local"
    name: str = "upload"           # this destination's own label; only meaningful for reporting when there is
                                     # more than one (parse_upload_destinations gives each list entry its own)
    port: int | None = None
    username: str | None = None
    password: str | None = None
    key_path: str | None = None
    allow_insecure_ftp: bool = False
    retries: int = DEFAULT_RETRIES
    retry_backoff_s: float = DEFAULT_BACKOFF_S
    timeout_s: float = DEFAULT_TIMEOUT_S
    mode: str | None = None        # "local" only: chmod the copy to this (octal string, e.g. "0644")
    owner: str | None = None       # "local" only: chown the copy to this ("user" or "user:group"); best-effort,
                                     # never fatal -- a build host's own upload user may not have the rights


@dataclasses.dataclass
class UploadResult:
    ok: bool
    detail: str  # safe to print or put in a report; never contains a secret


def parse_upload_config(data: dict | None) -> UploadConfig | None:
    """A single destination (the original, one-mapping shape). None (no
    `upload:` section at all) means the uploader is simply not configured —
    not an error, since it is explicitly optional (item 73). Prefer
    parse_upload_destinations() for a config that may hold either shape;
    this one stays for callers -- and tests -- that only ever had one."""
    if not data:
        return None
    if not isinstance(data, dict):
        raise UploadConfigError("upload: must be a mapping, or a list of mappings (see parse_upload_destinations)")
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
    if not data.get("remote_path"):
        raise UploadConfigError("upload.remote_path is required")
    if protocol != "local" and not data.get("host"):
        raise UploadConfigError("upload.host is required (every protocol except \"local\")")
    return UploadConfig(
        protocol=protocol,
        host=data.get("host"),
        remote_path=data["remote_path"],
        name=data.get("name", "upload"),
        port=data.get("port"),
        username=data.get("username"),
        password=data.get("password"),
        key_path=data.get("key_path"),
        allow_insecure_ftp=bool(data.get("allow_insecure_ftp", False)),
        retries=int(data.get("retries", DEFAULT_RETRIES)),
        retry_backoff_s=float(data.get("retry_backoff_s", DEFAULT_BACKOFF_S)),
        timeout_s=float(data.get("timeout_s", DEFAULT_TIMEOUT_S)),
        mode=data.get("mode"),
        owner=data.get("owner"),
    )


def parse_upload_destinations(data) -> list[UploadConfig]:
    """`config.upload`, in either shape it may be written in: one mapping
    (a single destination, parse_upload_config unchanged) or a list of
    mappings (several, in order — the order FanOutUploader's rehearsal-
    then-production rule replays). None or an empty list means nothing
    configured, same as parse_upload_config's None. Every list entry gets
    its own `name` (its own `name:` key, or "upload-N" by position) so a
    report can say which destination did what; two entries sharing a name
    is refused outright, not resolved by guessing which one a report line
    meant."""
    if not data:
        return []
    if isinstance(data, dict):
        single = parse_upload_config(data)
        return [single] if single else []
    if not isinstance(data, list):
        raise UploadConfigError("upload: must be a mapping or a list of mappings")
    destinations: list[UploadConfig] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise UploadConfigError(f"upload[{index}]: each destination must be a mapping")
        item = dict(item)
        item.setdefault("name", f"upload-{index}")
        parsed = parse_upload_config(item)
        if parsed is not None:
            destinations.append(parsed)
    names = [d.name for d in destinations]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise UploadConfigError(f"upload: destination name(s) used more than once: {', '.join(duplicates)}")
    return destinations


def describe_destination(config: UploadConfig) -> str:
    """The only representation of a destination this module ever prints or
    reports — protocol, user, host, port, remote path; never a password or
    a key's contents. "local" has no host at all — a plain path is the
    whole story."""
    if config.protocol == "local":
        return f"local:{config.remote_path}"
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


def _run_local(config: UploadConfig, local_path: Path) -> None:
    """Not a network at all: the "destination" is a path on this same
    machine — the development site's own document root, most often — so
    "uploading" is a plain, atomic file copy (write to a sibling temp file,
    then os.replace(), so a reader — nginx serving it — never sees a half-
    written file). config.mode/config.owner are applied afterwards,
    best-effort: a permission problem chmod-ing or chown-ing is reported as
    a transport error (never silently ignored), but never leaves a partial
    file at the destination path — only the temp file is removed."""
    dest = Path(config.remote_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.new.{os.getpid()}")
        shutil.copyfile(local_path, tmp)
        if config.mode:
            os.chmod(tmp, int(config.mode, 8))
        if config.owner:
            import grp
            import pwd
            user, _, group = config.owner.partition(":")
            uid = pwd.getpwnam(user).pw_uid if user else -1
            gid = grp.getgrnam(group).gr_gid if group else -1
            os.chown(tmp, uid, gid)
        os.replace(tmp, dest)
    except (OSError, ValueError, KeyError) as exc:
        try:
            tmp.unlink(missing_ok=True)  # noqa: F821 - tmp is set before any of the above can raise past mkdir
        except (NameError, OSError):
            pass
        raise UploadTransportError(f"could not copy to {dest}: {exc}") from exc


def _default_transport(config: UploadConfig, local_path: Path) -> None:
    if config.protocol == "scp":
        _run_scp(config, local_path)
    elif config.protocol == "sftp":
        _run_sftp(config, local_path)
    elif config.protocol == "ftps":
        _run_ftp(config, local_path, secure=True)
    elif config.protocol == "ftp":
        _run_ftp(config, local_path, secure=False)
    elif config.protocol == "local":
        _run_local(config, local_path)
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


def _default_fetcher(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - URL comes from an operator's own config
        return response.read()


def verify_published(url: str, local_path: Path, *, fetcher=None, timeout: float = DEFAULT_TIMEOUT_S) -> UploadResult:
    """Fetches `url` back over HTTP -- the *served* copy, proving the web
    server is actually handing out something for this run to check, not
    merely that a file landed on disk somewhere -- and checks, in order:
    the fetch itself succeeds; the body is valid JSON; it is a
    schema_version 2 object with an `entries` mapping (the shape
    tools/build_status.py's own StatusUpdater writes, and the shape a
    console refuses to render otherwise -- unchanged from before, a
    malformed or wrong-shape body still fails exactly the way it always
    has); and, new (item 201), that its bytes are byte-for-byte identical
    to `local_path` -- the exact file this same run just published to
    every `protocol: local` destination.

    That last check exists because a schema-valid document is not
    necessarily *this run's own* document: on a host where the deployed
    site and some other site both answer on the address this fetch uses
    (a second nginx vhost matching the same Host header, a stale reverse
    proxy route, anything that means the bytes verify_url actually serves
    did not come from the write this run just did), the old checks above
    all passed against a different, older, but still perfectly
    schema-valid build-status.json -- and the rehearsal reported "ok" for
    a file nobody had just published. Comparing bytes (reported as a
    SHA-256 of each side, so a person can tell the two apart without
    fetching the served copy themselves) is the only way to catch that;
    the misconfiguration causing it is never this tool's to fix, but
    reporting "ok" for someone else's file is.

    Never raises: a fetch, JSON, shape, or mismatch problem all come back
    as UploadResult(False, ...), the same contract as upload_file, so a
    caller can treat "could not verify" and "could not upload" the same
    way."""
    fetcher = fetcher or _default_fetcher
    try:
        raw = fetcher(url, timeout)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return UploadResult(False, f"could not fetch {url} back to verify it: {exc}")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return UploadResult(False, f"{url} did not come back as valid JSON: {exc}")
    if not isinstance(data, dict):
        return UploadResult(False, f"{url} did not come back as a JSON object")
    if data.get("schema_version") != STATUS_SCHEMA_VERSION:
        return UploadResult(False, f"{url} did not come back as schema_version {STATUS_SCHEMA_VERSION} "
                                    f"(got {data.get('schema_version')!r})")
    if not isinstance(data.get("entries"), dict):
        return UploadResult(False, f"{url} did not come back with an 'entries' object")
    served_digest = hashlib.sha256(raw).hexdigest()
    try:
        local_digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    except OSError as exc:
        return UploadResult(False, f"could not read {local_path} back to compare against {url}: {exc}")
    if served_digest != local_digest:
        return UploadResult(False,
            f"{url} is schema-valid but is NOT the file this run just published -- served sha256 "
            f"{served_digest} != published sha256 {local_digest}")
    return UploadResult(True, f"verified {url} is this run's own file (sha256 {local_digest}, "
                              f"schema_version {STATUS_SCHEMA_VERSION}, {len(data['entries'])} entrie(s))")


@dataclasses.dataclass
class DestinationOutcome:
    """One destination's (or one rehearsal verification's) own result, for
    a per-destination report — never an aggregate a caller has to pick
    apart itself."""
    name: str
    protocol: str
    ok: bool
    detail: str
    skipped: bool = False


class FanOutUploader:
    """Publishes one file to every configured destination
    (parse_upload_destinations()'s ordered list), with one deliberate
    ordering rule (docs/BUILD_MATRIX.md, "Publishing the build-status
    badge", the rehearsal-then-production note): every `protocol: local`
    destination — the rehearsal, a copy this same machine can serve, e.g.
    a development copy of the Studio page — is published and, when
    `verify_url` is given, fetched back and validated (verify_published(),
    above) before a single non-local ("production") destination is even
    attempted. If any local destination fails to publish, or the fetch-
    back verification fails, every production destination is reported
    "skipped" with that reason and none of them is touched — a failure to
    reach production this way can never leave the rehearsal copy stale
    (production was simply never tried), and a rehearsal failure can never
    reach a customer's own site.

    One destination's own failure never masks a sibling's *within* a phase
    that does run: every destination there is attempted and reported on
    its own (upload_file()'s own per-call try/except, unchanged) — the
    gate above only ever withholds the *later* phase, never a peer in the
    same one.

    `maybe_upload` is called many times over one run (once per live state
    change, StatusUpdater's own item 73 policy, plus once, unconditionally,
    at the end) — `self.outcomes`/`self.warnings` accumulate every call's
    results, oldest first, the same running-total contract ChangeUploader's
    own `.attempts`/`.warnings` already have."""

    def __init__(self, destinations: list[UploadConfig], *, dry_run: bool = False, transport=None, sleeper=None,
                verify_url: str | None = None, verify_fetcher=None):
        self.destinations = list(destinations)
        self.dry_run = dry_run
        self.transport = transport
        self.sleeper = sleeper
        self.verify_url = verify_url
        self.verify_fetcher = verify_fetcher
        self.local = [d for d in self.destinations if d.protocol == "local"]
        self.production = [d for d in self.destinations if d.protocol != "local"]
        self.outcomes: list[DestinationOutcome] = []
        self.warnings: list[str] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.destinations)

    def maybe_upload(self, local_path: Path) -> list[DestinationOutcome]:
        if not self.enabled:
            return []
        with self._lock:
            outcomes = self._run_once(local_path)
            self.outcomes.extend(outcomes)
            self.warnings.extend(f"{o.name}: {o.detail}" for o in outcomes if not o.ok)
            return outcomes

    def _run_once(self, local_path: Path) -> list[DestinationOutcome]:
        outcomes: list[DestinationOutcome] = []
        rehearsal_ok = True
        for dest in self.local:
            # Never the injected `transport` here: a "local" destination is
            # a plain, deterministic filesystem copy with nothing to fake
            # (unlike scp/sftp/ftp, which need a real network to really
            # exercise) — always the real _run_local, so a test proves the
            # rehearsal actually wrote a real, readable file.
            result = upload_file(dest, local_path, dry_run=self.dry_run, transport=None, sleeper=self.sleeper)
            outcomes.append(DestinationOutcome(dest.name, dest.protocol, result.ok, result.detail))
            if not result.ok:
                rehearsal_ok = False
        if rehearsal_ok and self.local and self.verify_url and not self.dry_run:
            verify_result = verify_published(self.verify_url, local_path, fetcher=self.verify_fetcher)
            outcomes.append(DestinationOutcome("rehearsal-verify", "verify", verify_result.ok, verify_result.detail))
            if not verify_result.ok:
                rehearsal_ok = False
        if not rehearsal_ok:
            for dest in self.production:
                outcomes.append(DestinationOutcome(
                    dest.name, dest.protocol, False,
                    "skipped: the rehearsal (local) publish did not pass, so production was never attempted",
                    skipped=True))
            return outcomes
        for dest in self.production:
            result = upload_file(dest, local_path, dry_run=self.dry_run, transport=self.transport, sleeper=self.sleeper)
            outcomes.append(DestinationOutcome(dest.name, dest.protocol, result.ok, result.detail))
        return outcomes


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
