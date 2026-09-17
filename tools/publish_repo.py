#!/usr/bin/env python3
"""Publish the local SynOS repository to the server named in the manifest, or serve it locally.

Layout on the server: one flat repository per suite, so installed systems use
    URIs: <repository>/<suite>/     Suites: ./

    python3 tools/publish_repo.py publish --dest user@host:/var/www/synos   (rsync over ssh)
    python3 tools/publish_repo.py publish --dest /srv/synos                 (local path)
    python3 tools/publish_repo.py serve [--port 8080]                       (test server)

`serve` prints the URL to put in manifest.yml under packages.repository for
a test build; it serves http://<this host>:<port>/<suite>/ from .build/repo.
"""
from __future__ import annotations

import argparse
import http.server
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)


def suite_of(manifest_path: Path) -> str:
    return str(render_manifest.load_yaml(manifest_path)["suite"])


def publish(repo: Path, dest: str, suite: str) -> int:
    if not (repo / "Packages").is_file():
        print("local repository is empty; run 'make packages' first", file=sys.stderr)
        return 1
    if (repo / "SIGNED").read_text(encoding="utf-8").strip() != "yes":
        print("warning: the local repository is unsigned (run 'make repo-key'); apt on installed systems will refuse it "
              "unless the source is marked Trusted", file=sys.stderr)
    target = f"{dest.rstrip('/')}/{suite}/"
    if ":" in dest and not dest.startswith("/"):
        cmd = ["rsync", "-az", "--delete", "--mkpath", str(repo) + "/", target]
    else:
        Path(target).mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-a", "--delete", str(repo) + "/", target]
    if shutil.which("rsync") is None:
        print("rsync is required", file=sys.stderr)
        return 1
    result = subprocess.run(cmd, check=False)
    if result.returncode == 0:
        print(f"published {suite} repository to {target}")
    return result.returncode


def serve(repo: Path, suite: str, port: int) -> int:
    if not (repo / "Packages").is_file():
        print("local repository is empty; run 'make packages' first", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / suite).symlink_to(repo)
        host = _host_ip()
        os.chdir(root)
        handler = http.server.SimpleHTTPRequestHandler
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        print(f"serving {repo} as http://{host}:{port}/{suite}/")
        print("put this in manifest.yml for a test build:")
        print(f"  packages:\n    repository: http://{host}:{port}/")
        print("stop with Ctrl+C")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _host_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["publish", "serve"])
    parser.add_argument("--manifest", default=str(ROOT / "manifest.yml"))
    parser.add_argument("--repo", default=str(ROOT / ".build" / "repo"))
    parser.add_argument("--dest", default=None, help="rsync destination (user@host:/path or /path)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    suite = suite_of(Path(args.manifest).resolve())
    repo = Path(args.repo).resolve()
    if args.action == "publish":
        if not args.dest:
            print("publish needs --dest", file=sys.stderr)
            return 2
        return publish(repo, args.dest, suite)
    return serve(repo, suite, args.port)


if __name__ == "__main__":
    sys.exit(main())
