#!/usr/bin/env python3
"""Regressions for fail-closed Skill EOL normalization."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
import shutil
import stat
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


JPEG_WITH_PIXEL_SIGNIFICANT_CR = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEY"
    "Ix8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQ"
    "EBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozv/wAARCAAIAAgDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAL/xAAd"
    "EAACAgIDAQAAAAAAAAAAAAACAQMAEQQSIRMx/8QAFQEBAQAAAAAAAAAAAAAAAAAAAwX/"
    "xAAgEQABAwIHAAAAAAAAAAAAAAABAhEAIVEEEzFBccHh/9oADAMBAAIRAxEAPwCRGPal"
    "i1J5RTnBSlF58gJoRFnzSzkibFYT6+56qqq5KlVCmcA6XHI7lFOBCnrvb0T/2Q=="
)


class CheckSkillEolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="check-skill-eol-test-")
        self.repository = Path(self.temporary.name) / "repository"
        self.root = self.repository / "skills" / "make-bilingual-study-pdf"
        self.root.parent.mkdir(parents=True)
        shutil.copytree(
            SKILL_ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_main(
        self,
        *,
        fix: bool,
        repository: Path | None = None,
        root: Path | None = None,
    ) -> tuple[int, str]:
        arguments = ["check_skill_eol.py", *(["--fix"] if fix else [])]
        output = io.StringIO()
        with (
            mock.patch.object(
                check_skill_eol, "REPOSITORY", repository or self.repository
            ),
            mock.patch.object(check_skill_eol, "SKILL_ROOT", root or self.root),
            mock.patch.object(sys, "argv", arguments),
            contextlib.redirect_stdout(output),
        ):
            result = check_skill_eol.main()
        return result, output.getvalue()

    def test_allowlisted_text_is_detected_then_normalized(self) -> None:
        path = self.root / "needs-normalization.md"
        path.write_bytes(b"first\r\nsecond\rthird\n")
        original_mode = stat.S_IMODE(path.stat().st_mode)

        check_result, check_output = self.run_main(fix=False)
        self.assertNotEqual(check_result, 0, check_output)
        self.assertIn("needs-normalization.md", check_output)
        self.assertEqual(path.read_bytes(), b"first\r\nsecond\rthird\n")

        result, output = self.run_main(fix=True)
        self.assertEqual(result, 0, output)
        self.assertEqual(path.read_bytes(), b"first\nsecond\nthird\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode)
        self.assertIn("normalized Skill line endings: 1 changed", output)

    def test_partial_temporary_write_failure_preserves_original_and_cleans_up(
        self,
    ) -> None:
        path = self.root / "write-failure.txt"
        original = b"first\r\nsecond\r\n"
        path.write_bytes(original)

        def fail_after_partial_write(descriptor: int, payload: bytes) -> None:
            os.write(descriptor, payload[:3])
            raise OSError("simulated temporary write failure")

        with mock.patch.object(
            check_skill_eol, "write_all", side_effect=fail_after_partial_write
        ):
            result, output = self.run_main(fix=True)

        self.assertNotEqual(result, 0, output)
        self.assertIn("simulated temporary write failure", output)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(f".{path.name}.eol-*")), [])

    def test_atomic_replace_failure_preserves_original_and_cleans_up(self) -> None:
        path = self.root / "replace-failure.txt"
        original = b"first\r\nsecond\r\n"
        path.write_bytes(original)

        with mock.patch.object(
            check_skill_eol.os,
            "replace",
            side_effect=OSError("simulated atomic replace failure"),
        ):
            result, output = self.run_main(fix=True)

        self.assertNotEqual(result, 0, output)
        self.assertIn("simulated atomic replace failure", output)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(f".{path.name}.eol-*")), [])

    def test_known_jpeg_is_never_read_or_rewritten(self) -> None:
        path = self.root / "pixel-sensitive.jpg"
        path.write_bytes(JPEG_WITH_PIXEL_SIGNIFICANT_CR)
        original_sha = hashlib.sha256(JPEG_WITH_PIXEL_SIGNIFICANT_CR).hexdigest()
        naively_normalized = JPEG_WITH_PIXEL_SIGNIFICANT_CR.replace(
            b"\r\n", b"\n"
        ).replace(b"\r", b"\n")
        self.assertNotEqual(naively_normalized, JPEG_WITH_PIXEL_SIGNIFICANT_CR)

        read_regular_bytes = check_skill_eol.read_regular_bytes

        def reject_jpeg_read(
            repository: Path,
            root: Path,
            candidate: Path,
            label: str,
            status: os.stat_result,
            expected_chain: check_skill_eol.DirectoryChain,
        ) -> bytes:
            self.assertNotEqual(candidate, path, "known binary payload was read")
            return read_regular_bytes(
                repository, root, candidate, label, status, expected_chain
            )

        with mock.patch.object(
            check_skill_eol, "read_regular_bytes", side_effect=reject_jpeg_read
        ):
            check_result, check_output = self.run_main(fix=False)
            self.assertEqual(check_result, 0, check_output)
            fix_result, fix_output = self.run_main(fix=True)
            self.assertEqual(fix_result, 0, fix_output)

        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_sha)
        self.assertEqual(path.read_bytes(), JPEG_WITH_PIXEL_SIGNIFICANT_CR)

    def test_unknown_suffix_fails_before_any_fix(self) -> None:
        earlier = self.root / "00-needs-normalization.txt"
        earlier.write_bytes(b"inside\r\n")
        unknown = self.root / "zz-unknown.assetx"
        unknown.write_bytes(b"unknown\r\nbytes\r")

        check_result, check_output = self.run_main(fix=False)
        self.assertNotEqual(check_result, 0, check_output)
        self.assertIn("unsupported file type in Skill tree", check_output)
        self.assertEqual(unknown.read_bytes(), b"unknown\r\nbytes\r")

        fix_result, fix_output = self.run_main(fix=True)
        self.assertNotEqual(fix_result, 0, fix_output)
        self.assertIn("zz-unknown.assetx", fix_output)
        self.assertEqual(unknown.read_bytes(), b"unknown\r\nbytes\r")
        self.assertEqual(earlier.read_bytes(), b"inside\r\n")

    def test_allowlisted_text_must_be_utf8_without_nul(self) -> None:
        for name, payload, message in (
            ("invalid-utf8.txt", b"\xff\r\n", "not valid UTF-8"),
            ("nul.txt", b"before\x00after\r\n", "NUL bytes"),
        ):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(payload)
                result, output = self.run_main(fix=True)
                self.assertNotEqual(result, 0, output)
                self.assertIn(message, output)
                self.assertEqual(path.read_bytes(), payload)
                path.unlink()

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

    @unittest.skipIf(os.name == "nt", "POSIX ancestor-symlink regression")
    def test_skills_ancestor_symlink_is_rejected_before_external_access(self) -> None:
        skills = self.repository / "skills"
        outside_skills = Path(self.temporary.name) / "outside-skills"
        skills.rename(outside_skills)
        outside = outside_skills / "make-bilingual-study-pdf" / "outside.txt"
        outside.write_bytes(b"outside\r\n")
        os.symlink(outside_skills, skills, target_is_directory=True)
        try:
            result, output = self.run_main(fix=True)
            self.assertNotEqual(result, 0, output)
            self.assertIn("symbolic links are not allowed", output)
            self.assertIn("skills directory", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
        finally:
            skills.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX repository-symlink regression")
    def test_repository_symlink_is_rejected_before_external_access(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside\r\n")
        linked_repository = Path(self.temporary.name) / "repository-link"
        os.symlink(self.repository, linked_repository, target_is_directory=True)
        try:
            result, output = self.run_main(
                fix=True,
                repository=linked_repository,
                root=linked_repository / "skills" / "make-bilingual-study-pdf",
            )
            self.assertNotEqual(result, 0, output)
            self.assertIn("symbolic links are not allowed", output)
            self.assertIn("repository root", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
        finally:
            linked_repository.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX repository-ancestor regression")
    def test_repository_ancestor_symlink_is_rejected(self) -> None:
        real_parent = Path(self.temporary.name) / "real-parent"
        real_parent.mkdir()
        actual_repository = real_parent / "repository"
        self.repository.rename(actual_repository)
        outside = (
            actual_repository
            / "skills"
            / "make-bilingual-study-pdf"
            / "outside.txt"
        )
        outside.write_bytes(b"outside\r\n")
        linked_parent = Path(self.temporary.name) / "parent-link"
        os.symlink(real_parent, linked_parent, target_is_directory=True)
        try:
            linked_repository = linked_parent / "repository"
            result, output = self.run_main(
                fix=True,
                repository=linked_repository,
                root=linked_repository / "skills" / "make-bilingual-study-pdf",
            )
            self.assertNotEqual(result, 0, output)
            self.assertIn("symbolic links are not allowed", output)
            self.assertIn("repository ancestor", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
        finally:
            linked_parent.unlink()

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
            self.fail(
                "Windows junction regression must execute: "
                f"{created.stdout}{created.stderr}"
            )
        try:
            result, output = self.run_main(fix=True)
            self.assertNotEqual(result, 0, output)
            self.assertIn("reparse points are not allowed", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
            self.assertEqual(earlier.read_bytes(), b"inside\r\n")
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_windows_skills_ancestor_junction_is_rejected(self) -> None:
        skills = self.repository / "skills"
        outside_skills = Path(self.temporary.name) / "outside-skills"
        skills.rename(outside_skills)
        outside = outside_skills / "make-bilingual-study-pdf" / "outside.txt"
        outside.write_bytes(b"outside\r\n")
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(skills), str(outside_skills)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if created.returncode != 0:
            self.fail(
                "Windows junction regression must execute: "
                f"{created.stdout}{created.stderr}"
            )
        try:
            result, output = self.run_main(fix=True)
            self.assertNotEqual(result, 0, output)
            self.assertIn("reparse points are not allowed", output)
            self.assertIn("skills directory", output)
            self.assertEqual(outside.read_bytes(), b"outside\r\n")
        finally:
            if os.path.lexists(skills):
                os.rmdir(skills)

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
