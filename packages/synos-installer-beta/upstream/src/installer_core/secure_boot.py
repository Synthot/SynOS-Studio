"""Secure Boot key, signed-loader and MOK enrollment lifecycle."""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .command import CommandRunner
from .model import Architecture, InstallPlan, SecureBoot
from .steps import FailurePolicy, InstallContext, StepWarning


# SynOS documents this as the one-time MOKManager enrollment password.
# It is deliberately executor policy, not serialized plan data.
MOK_ENROLLMENT_PASSWORD = "123456"
MOK_DIRECTORY = Path("var/lib/shim-signed/mok")
MOK_PRIVATE_KEY = MOK_DIRECTORY / "MOK.priv"
MOK_CERTIFICATE = MOK_DIRECTORY / "MOK.der"
MOK_MARKER = Path("var/lib/synos-installer/mok-certificate.sha256")


@dataclass
class PrepareSecureBootStep:
    runner: CommandRunner
    id: str = "prepare-secure-boot"
    title: str = "Prepare Secure Boot signing"
    failure_policy: FailurePolicy = FailurePolicy.FATAL
    progress_weight: int = 4
    destructive: bool = False

    def preflight(self, context: InstallContext) -> None:
        context.validate_plan()
        if _enabled(context.plan):
            self.runner.require_commands(("chroot", "sbverify"))

    def execute(self, context: InstallContext) -> None:
        if not _enabled(context.plan):
            context.values["secure_boot_prepared"] = False
            return
        target = _target(context)
        required = (
            target / "usr/sbin/update-secureboot-policy",
            target / "usr/bin/mokutil",
            target / "usr/bin/openssl",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(
                "Secure Boot tools are missing from target: "
                + ", ".join(missing)
            )
        _require_signed_payloads(target, context.plan)

        private_key = target / MOK_PRIVATE_KEY
        certificate = target / MOK_CERTIFICATE
        marker = target / MOK_MARKER
        if not _is_installer_key(certificate, private_key, marker):
            # Never clone a build-time or live-session private key.
            for path in (private_key, certificate, target / MOK_DIRECTORY / ".rnd"):
                if path.exists() or path.is_symlink():
                    path.unlink()
            if marker.exists() or marker.is_symlink():
                marker.unlink()
            (target / MOK_DIRECTORY).mkdir(parents=True, exist_ok=True)
            self.runner.run(
                (
                    "chroot",
                    str(target),
                    "update-secureboot-policy",
                    "--new-key",
                ),
                timeout=120,
            )
            if not private_key.is_file() or not certificate.is_file():
                raise RuntimeError("Secure Boot key generation produced no key pair")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(_sha256(certificate) + "\n", encoding="ascii")

        private_key.chmod(0o600)
        certificate.chmod(0o644)
        _verify_key_pair(self.runner, target)
        _write_dkms_configuration(target)
        context.values["secure_boot_certificate_sha1"] = _sha1(certificate)
        context.values["secure_boot_prepared"] = True

    def verify(self, context: InstallContext) -> None:
        if not _enabled(context.plan):
            return
        target = _target(context)
        private_key = target / MOK_PRIVATE_KEY
        certificate = target / MOK_CERTIFICATE
        if not private_key.is_file() or not certificate.is_file():
            raise RuntimeError("Secure Boot key pair is missing")
        if private_key.stat().st_mode & 0o077:
            raise RuntimeError("MOK private key permissions are too broad")
        _verify_key_pair(self.runner, target)

    def cleanup(self, context: InstallContext) -> None:
        return None


@dataclass
class VerifyDkmsSignaturesStep:
    """Build DKMS modules after upgrades/drivers and verify their MOK signer."""

    runner: CommandRunner
    id: str = "verify-dkms-signatures"
    title: str = "Build and verify kernel modules"
    failure_policy: FailurePolicy = FailurePolicy.FATAL
    progress_weight: int = 3
    destructive: bool = False

    def preflight(self, context: InstallContext) -> None:
        context.validate_plan()
        self.runner.require_commands(("chroot",))

    def execute(self, context: InstallContext) -> None:
        target = _target(context)
        context.values["dkms_modules"] = []
        if not (target / "usr/sbin/dkms").is_file():
            return

        # `dkms autoinstall` with no `-k` builds against `uname -r` of the
        # process running it. Under `chroot /target`, that is still the live
        # session's own kernel, not the kernel the installer just put in
        # /target — chroot changes the filesystem root, not what the kernel
        # reports itself as. Naming the target's installed kernel explicitly
        # is what makes this build against the system actually being
        # installed, and lets us tell "nothing to build against" (missing
        # headers) apart from "the module does not compile".
        kernels = _target_kernel_releases(target)
        if not kernels:
            context.log(
                f"[{self.id}] no installed kernel was found under /lib/modules "
                "in the target; DKMS autoinstall was skipped"
            )
            return

        outcomes: list[dict[str, str]] = []
        for kernel in kernels:
            registered = _dkms_status(self.runner, target, kernel)
            if not registered:
                continue
            if not _kernel_headers_present(self.runner, target, kernel):
                message = (
                    f"kernel headers for {kernel} are not installed; "
                    "no out-of-tree modules can be built"
                )
                context.log(f"[{self.id}] {message}")
                for entry in registered:
                    outcomes.append(
                        {
                            "module": entry["module"],
                            "version": entry["version"],
                            "kernel": kernel,
                            "state": "headers-missing",
                            "detail": message,
                        }
                    )
                continue

            self.runner.run(
                ("chroot", str(target), "dkms", "autoinstall", "-k", kernel),
                check=False,
                timeout=3600,
            )
            after = {
                (entry["module"], entry["version"]): entry["state"]
                for entry in _dkms_status(self.runner, target, kernel)
            }
            for entry in registered:
                state = after.get((entry["module"], entry["version"]), "unknown")
                if state == "installed":
                    continue
                log_tail = _module_build_log_tail(
                    self.runner, target, entry["module"], entry["version"]
                )
                detail = (
                    f"{entry['module']}/{entry['version']} for kernel {kernel} "
                    f"did not reach 'installed' (dkms reports '{state}')"
                )
                if log_tail:
                    detail += f"\n--- {entry['module']} build log tail ---\n{log_tail}"
                context.log(f"[{self.id}] {detail}")
                outcomes.append(
                    {
                        "module": entry["module"],
                        "version": entry["version"],
                        "kernel": kernel,
                        "state": state,
                        "detail": detail,
                    }
                )

        context.values["dkms_modules"] = outcomes
        if outcomes:
            # A module that failed to build is never something an
            # installation should abort over: it is not required to boot the
            # system, and the person can retry it (or the person who packaged
            # it can fix it) after the fact. VerifyDkmsSignaturesStep.verify
            # still fails the plan, fatally, if a module that *did* get built
            # is not signed by the installation's own MOK — that is the
            # genuinely fatal case, because it is a Secure Boot violation on
            # a module that will actually try to load.
            summary = "; ".join(
                f"{o['module']}/{o['version']} ({o['kernel']}): {o['state']}"
                for o in outcomes
            )
            raise StepWarning(
                "One or more DKMS modules could not be built and were left "
                f"uninstalled: {summary}"
            )

    def verify(self, context: InstallContext) -> None:
        if not _enabled(context.plan):
            return
        target = _target(context)
        certificate = target / MOK_CERTIFICATE
        if not certificate.is_file():
            raise RuntimeError("MOK certificate is missing before DKMS verification")
        serial_result = self.runner.run(
            (
                "chroot",
                str(target),
                "openssl",
                "x509",
                "-inform",
                "DER",
                "-in",
                f"/{MOK_CERTIFICATE}",
                "-serial",
                "-noout",
            ),
            timeout=30,
        )
        certificate_serial = _hex_identifier(serial_result.stdout)
        if not certificate_serial:
            raise RuntimeError("Could not determine MOK certificate serial")

        module_root = target / "lib/modules"
        modules = (
            sorted(module_root.glob("*/updates/dkms/*.ko*"))
            if module_root.is_dir()
            else []
        )
        for module in modules:
            relative = "/" + str(module.relative_to(target))
            result = self.runner.run(
                (
                    "chroot",
                    str(target),
                    "modinfo",
                    "-F",
                    "sig_key",
                    relative,
                ),
                check=False,
                timeout=30,
            )
            module_key = _hex_identifier(result.stdout)
            if result.returncode != 0 or module_key != certificate_serial:
                raise RuntimeError(
                    "DKMS module is not signed by the installation MOK: "
                    f"{relative}"
                )

    def cleanup(self, context: InstallContext) -> None:
        return None


@dataclass
class EnrollSecureBootStep:
    runner: CommandRunner
    id: str = "enroll-secure-boot"
    title: str = "Schedule MOK enrollment"
    failure_policy: FailurePolicy = FailurePolicy.FATAL
    progress_weight: int = 2
    destructive: bool = False

    def preflight(self, context: InstallContext) -> None:
        context.validate_plan()

    def execute(self, context: InstallContext) -> None:
        if not _enabled(context.plan):
            context.values["mok_enrollment_pending"] = False
            return
        if not context.values.get("secure_boot_prepared"):
            raise RuntimeError("Secure Boot signing was not prepared")
        target = _target(context)
        certificate = target / MOK_CERTIFICATE
        fingerprint = _sha1(certificate)
        # Validate the complete signed loader chain before mutating EFI vars.
        _verify_signed_efi_chain(self.runner, target, context.plan)

        if _is_enrolled(self.runner, target):
            context.values["mok_enrollment_pending"] = False
            return
        if fingerprint not in _pending_fingerprints(self.runner, target):
            password_input = (
                f"{MOK_ENROLLMENT_PASSWORD}\n"
                f"{MOK_ENROLLMENT_PASSWORD}\n"
            )
            self.runner.run(
                (
                    "chroot",
                    str(target),
                    "mokutil",
                    "--import",
                    f"/{MOK_CERTIFICATE}",
                ),
                input_text=password_input,
                timeout=60,
            )
        self.runner.run(
            ("chroot", str(target), "mokutil", "--timeout", "-1"),
            timeout=30,
        )
        context.values["mok_enrollment_pending"] = True

    def verify(self, context: InstallContext) -> None:
        if not _enabled(context.plan):
            return
        target = _target(context)
        certificate = target / MOK_CERTIFICATE
        if not _is_enrolled(self.runner, target):
            fingerprint = _sha1(certificate)
            if fingerprint not in _pending_fingerprints(self.runner, target):
                raise RuntimeError("MOK enrollment request is not present")
        _verify_signed_efi_chain(self.runner, target, context.plan)

    def cleanup(self, context: InstallContext) -> None:
        # Revoking all pending imports could destroy an unrelated user request.
        return None


_DKMS_STATUS_RE = re.compile(
    r"^(?P<module>[^/,\s]+)/(?P<version>[^,\s]+),\s*"
    r"(?P<kernel>[^,\s]+),\s*(?P<architecture>[^:\s]+):\s*"
    r"(?P<state>[a-z-]+)(?:\s.*)?$"
)


def _target_kernel_releases(target: Path) -> list[str]:
    """Kernel versions actually installed in /target, from /lib/modules —
    not the live session's `uname -r`, which chroot does not change."""
    module_root = target / "lib/modules"
    if not module_root.is_dir():
        return []
    return sorted(entry.name for entry in module_root.iterdir() if entry.is_dir())


def _kernel_headers_present(runner: CommandRunner, target: Path, kernel: str) -> bool:
    # A headers package for `kernel` symlinks /usr/src/linux-headers-<kernel>
    # into /lib/modules/<kernel>/build; that is exactly what DKMS itself
    # needs to build against, so it is the authoritative check. Resolved
    # inside the chroot: an absolute symlink recorded under /target would
    # otherwise resolve against the real root, not /target's.
    result = runner.run(
        ("chroot", str(target), "test", "-e", f"/lib/modules/{kernel}/build"),
        check=False,
        timeout=30,
        log_output=False,
    )
    return result.returncode == 0


def _dkms_status(
    runner: CommandRunner, target: Path, kernel: str
) -> list[dict[str, str]]:
    result = runner.run(
        ("chroot", str(target), "dkms", "status", "-k", kernel),
        check=False,
        timeout=120,
        log_output=False,
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        match = _DKMS_STATUS_RE.match(line.strip())
        if match:
            entries.append(match.groupdict())
    return entries


def _module_build_log_tail(
    runner: CommandRunner,
    target: Path,
    module: str,
    version: str,
    *,
    lines: int = 40,
) -> str:
    """The last lines of the DKMS build log for one module/version, across
    both the flat (.../build/make.log) and per-kernel/arch DKMS log layouts."""
    script = (
        f"f=$(find /var/lib/dkms/{shlex.quote(module)}/{shlex.quote(version)} "
        "-name make.log 2>/dev/null | sort | tail -n1); "
        f'[ -n "$f" ] && tail -n {int(lines)} "$f"'
    )
    result = runner.run(
        ("chroot", str(target), "sh", "-c", script),
        check=False,
        timeout=30,
        log_output=False,
    )
    return result.stdout.strip()


def _enabled(plan: InstallPlan) -> bool:
    return plan.platform.secure_boot is SecureBoot.ENABLED


def _is_installer_key(certificate: Path, private_key: Path, marker: Path) -> bool:
    if not certificate.is_file() or not private_key.is_file() or not marker.is_file():
        return False
    return marker.read_text(encoding="ascii").strip() == _sha256(certificate)


def _verify_key_pair(runner: CommandRunner, target: Path) -> None:
    certificate_public = runner.run(
        (
            "chroot",
            str(target),
            "openssl",
            "x509",
            "-inform",
            "DER",
            "-in",
            f"/{MOK_CERTIFICATE}",
            "-pubkey",
            "-noout",
        ),
        timeout=30,
    ).stdout.strip()
    private_public = runner.run(
        (
            "chroot",
            str(target),
            "openssl",
            "pkey",
            "-in",
            f"/{MOK_PRIVATE_KEY}",
            "-pubout",
        ),
        timeout=30,
    ).stdout.strip()
    if not certificate_public or certificate_public != private_public:
        raise RuntimeError("MOK certificate does not match its private key")


def _write_dkms_configuration(target: Path) -> None:
    config = target / "etc/dkms/framework.conf.d/synos-sb-sign.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'mok_signing_key="/var/lib/shim-signed/mok/MOK.priv"\n'
        'mok_certificate="/var/lib/shim-signed/mok/MOK.der"\n',
        encoding="utf-8",
    )
    config.chmod(0o644)


def _require_signed_payloads(target: Path, plan: InstallPlan) -> None:
    if plan.platform.architecture is Architecture.AMD64:
        paths = (
            target / "usr/lib/shim/shimx64.efi.signed.latest",
            target / "usr/lib/grub/x86_64-efi-signed/grubx64.efi.signed",
        )
    else:
        paths = (
            target / "usr/lib/shim/shimaa64.efi.signed.latest",
            target / "usr/lib/grub/arm64-efi-signed/grubaa64.efi.signed",
        )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Signed Secure Boot payloads are missing: " + ", ".join(missing)
        )


def _verify_signed_efi_chain(
    runner: CommandRunner, target: Path, plan: InstallPlan
) -> None:
    suffix = "x64" if plan.platform.architecture is Architecture.AMD64 else "aa64"
    paths = (
        target / "boot/efi/EFI/SynOS" / f"shim{suffix}.efi",
        target / "boot/efi/EFI/SynOS" / f"grub{suffix}.efi",
    )
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError("Signed EFI chain is incomplete: " + ", ".join(missing))
    for path in paths:
        self_check = runner.run(
            ("sbverify", "--list", str(path)), check=False, timeout=30
        )
        if self_check.returncode != 0:
            raise RuntimeError(f"EFI executable is not signed: {path}")


def _is_enrolled(runner: CommandRunner, target: Path) -> bool:
    fingerprint = _sha1(target / MOK_CERTIFICATE)
    enrolled = _mok_fingerprints(runner, target, "--list-enrolled")
    if enrolled is not None:
        return fingerprint in enrolled

    # Upstream mokutil 0.7.2 returns 0 for "not enrolled" and 1 for
    # "already enrolled". Parse its C-locale message only as a fallback;
    # never reinterpret its process status as a boolean.
    result = runner.run(
        (
            "chroot",
            str(target),
            "mokutil",
            "--test-key",
            f"/{MOK_CERTIFICATE}",
        ),
        check=False,
        timeout=30,
    )
    message = f"{result.stdout}\n{result.stderr}".lower()
    return "is already enrolled" in message or "is already in db" in message


def _mok_fingerprints(
    runner: CommandRunner, target: Path, operation: str
) -> frozenset[str] | None:
    result = runner.run(
        ("chroot", str(target), "mokutil", operation),
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    return frozenset(
        normalized
        for value in re.findall(
            r"^SHA1 Fingerprint:\s*([0-9a-f:]+)\s*$",
            result.stdout,
            re.IGNORECASE | re.MULTILINE,
        )
        if (normalized := _sha1_fingerprint(value))
    )


def _pending_fingerprints(
    runner: CommandRunner, target: Path
) -> frozenset[str]:
    return _mok_fingerprints(runner, target, "--list-new") or frozenset()


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes(), usedforsecurity=False).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex_identifier(value: str) -> str:
    # openssl emits "serial=ABCD"; modinfo commonly separates bytes with ':'.
    value = value.strip().splitlines()[0] if value.strip() else ""
    if "=" in value:
        value = value.split("=", 1)[1]
    return "".join(
        character for character in value.lower() if character in "0123456789abcdef"
    ).lstrip("0") or "0"


def _sha1_fingerprint(value: str) -> str:
    """Normalize a complete SHA-1 fingerprint without dropping leading zeroes."""
    normalized = "".join(
        character for character in value.lower() if character in "0123456789abcdef"
    )
    return normalized if len(normalized) == 40 else ""


def _target(context: InstallContext) -> Path:
    target = context.values.get("target")
    if not isinstance(target, Path):
        raise RuntimeError("Target filesystem is not mounted")
    return target
