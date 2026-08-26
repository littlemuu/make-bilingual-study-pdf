#!/usr/bin/env python3
"""Focused regressions for the public release gate."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from release_check import SEMVER_RE, require_current_tag_literals


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "release_check.py"


class ReleaseCheckTests(unittest.TestCase):
    def test_semver_is_strict_ascii_semver(self) -> None:
        valid = (
            "0.0.0",
            "2.3.0",
            "1.0.0-alpha",
            "1.0.0-0.3.7",
            "1.0.0-x.7.z.92",
            "1.0.0+build.1",
        )
        invalid = (
            "",
            "v1.0.0",
            "01.0.0",
            "1.01.0",
            "1.0.01",
            "1.0.0-01",
            "1.0.0-alpha.01",
            "1\u0661.2.3",
            "2.3.1-\u0661a",
        )
        self.assertTrue(all(SEMVER_RE.fullmatch(value) for value in valid))
        self.assertFalse(any(SEMVER_RE.fullmatch(value) for value in invalid))

    def test_current_version_and_tag_pass(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        process = subprocess.run(
            [
                sys.executable,
                str(CHECK),
                "--expected-version",
                version,
                "--tag",
                f"v{version}",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "passed")

    def test_wrong_tag_fails(self) -> None:
        process = subprocess.run(
            [sys.executable, str(CHECK), "--tag", "v9.9.9"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(process.returncode, 0)
        report = json.loads(process.stdout)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any("release tag mismatch" in failure for failure in report["failures"])
        )

    def test_conflicting_public_tag_literal_fails(self) -> None:
        failures: list[str] = []
        require_current_tag_literals(
            relative="README.md",
            text="current v2.3.0 but installer v2.3.1",
            expected="v2.3.1",
            failures=failures,
        )
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
