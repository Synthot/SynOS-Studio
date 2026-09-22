"""The dist/<name>.sha256 file build.sh writes must be a standard sha256sum
checksum file: `sha256sum -c` has to accept it from inside dist/."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def extract_function(source: str, name: str) -> str:
    """Pulls a `function <name>() { ... }` block out of a shell script by
    matching braces, so the test runs the real build.sh code, not a copy."""
    start = source.index(f"function {name}()")
    body_start = source.index("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function {name}() in build.sh")


class ChecksumFormatTests(unittest.TestCase):
    def test_write_iso_checksum_is_extracted_from_build_sh(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("function write_iso_checksum()", build)
        function = extract_function(build, "write_iso_checksum")
        self.assertIn("sha256sum", function)

    def test_sha256sum_dash_c_accepts_the_written_checksum_file(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        function = extract_function(build, "write_iso_checksum")
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            iso = dist / "synos-example-1.0.0-ubuntu-resolute-20260101-amd64.iso"
            iso.write_bytes(b"not a real iso, just some bytes to hash\n")
            sha_path = dist / (iso.stem + ".sha256")
            script = f"{function}\nwrite_iso_checksum '{iso}' '{sha_path}'\n"
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)

            contents = sha_path.read_text(encoding="utf-8")
            # standard format: "<hash>  <name>" (two spaces), name relative to dist/
            self.assertRegex(contents, r"^[0-9a-f]{64}  " + iso.name + r"\n$")
            self.assertNotIn(str(dist), contents, "the checksum file must name the iso relative to dist/")
            self.assertNotIn("SHA256:", contents)

            check = subprocess.run(["sha256sum", "-c", sha_path.name], cwd=dist, capture_output=True, text=True)
            self.assertEqual(0, check.returncode, check.stdout + check.stderr)
            self.assertIn("OK", check.stdout)

            # a tampered image is caught, not silently accepted
            iso.write_bytes(b"tampered\n")
            failed_check = subprocess.run(["sha256sum", "-c", sha_path.name], cwd=dist, capture_output=True, text=True)
            self.assertNotEqual(0, failed_check.returncode)


if __name__ == "__main__":
    unittest.main()
