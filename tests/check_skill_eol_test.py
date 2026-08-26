#!/usr/bin/env python3
"""Regressions for fail-closed Skill EOL normalization."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPOSITORY / "tools"))

import check_skill_eol  # noqa: E402


class CheckSkillEolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="check-skill-eol-test-")
        self.root = Path(self.temporary.name) / "make-bilingual-study-pdf"
        shutil.copytree(
            SKILL_ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_main(self, *, fix: bool) -> tuple[int, str]:
        arguments = ["check_skill_eol.py", *(["--fix"] if fix else [])]
        output = io.StringIO()
        with (
            mock.patch.object(check_skill_eol, "SKILL_ROOT", self.root),
            mock.patch.object(sys, "argv", arguments),
            contextlib.redirect_stdout(output),
        ):
            result = check_skill_eol.main()
        return result, output.getvalue()

    def test_regular_file_is_normalized(self) -> None:
        path = self.root / "needs-normalization.txt"
        path.write_bytes(b"first\r\nsecond\rthird\n")
        result, output = self.run_main(fix=True)
        self.assertEqual(result, 0, output)
        self.assertEqual(path.read_bytes(), b"first\nsecond\nthird\n")
        self.assertIn("normalized Skill line endings: 1 changed", output)

    def test_symlink_target_and_earlier_file_remain_unchanged(self) -> None:
        earlier = self.root / "00-needs-normalization.txt"
        earlier.write_bytes(b"inside\r\n")
        outside = Path(self.temporary.name) / "outside.txt"
        outside.write_bytes(b"outside\r\n")
        link = self.root / "zz-outside-link.txt"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        result, output = self.run_main(fix=True)

        self.assertNotEqual(result, 0, output)
        self.assertIn("symbolic links are not allowed", output)
        self.assertEqual(outside.read_bytes(), b"outside\r\n")
        self.assertEqual(earlier.read_bytes(), b"inside\r\n")

    def test_hardlink_target_and_earlier_file_remain_unchanged(self) -> None:
        earlier = self.root / "00-needs-normalization.txt"
        earlier.write_bytes(b"inside\r\n")
        outside = Path(self.temporary.name) / "outside-hardlink-target.txt"
        outside.write_bytes(b"outside\r\n")
        link = self.root / "zz-outside-hardlink.txt"
        try:
            os.link(outside, link)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        result, output = self.run_main(fix=True)

        self.assertNotEqual(result, 0, output)
        self.assertIn("multiply linked files are not allowed", output)
        self.assertEqual(outside.read_bytes(), b"outside\r\n")
        self.assertEqual(link.read_bytes(), b"outside\r\n")
        self.assertEqual(earlier.read_bytes(), b"inside\r\n")

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_windows_junction_target_remains_unchanged(self) -> None:
        earlier = self.root / "00-needs-normalization.txt"
        earlier.write_bytes(b"inside\r\n")
        target = Path(self.temporary.name) / "junction-target"
        target.mkdir()
        outside = target / "outside.txt"
        outside.write_bytes(b"outside\r\n")
        junction = self.root / "zz-junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if created.returncode != 0:
            self.skipTest(f"junctions unavailable: {created.stdout}{created.stderr}")
        try:
            result, output = self.run_main(fix=True)
            self.assertNotEqual(result, 0, output)
            self.assertIn("reparse points are not allowed", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
            self.assertEqual(earlier.read_bytes(), b"inside\r\n")
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_fifo_is_rejected_without_reading_or_partial_fix(self) -> None:
        earlier = self.root / "00-needs-normalization.txt"
        earlier.write_bytes(b"inside\r\n")
        os.mkfifo(self.root / "zz-payload.pipe")
        result, output = self.run_main(fix=True)
        self.assertNotEqual(result, 0, output)
        self.assertIn("non-regular filesystem entries are not allowed", output)
        self.assertEqual(earlier.read_bytes(), b"inside\r\n")

    def test_reparse_attribute_is_detected(self) -> None:
        status = SimpleNamespace(
            st_file_attributes=check_skill_eol.WINDOWS_REPARSE_ATTRIBUTE,
            st_reparse_tag=0,
        )
        self.assertTrue(check_skill_eol.is_reparse_point(status))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main(verbosity=2)
