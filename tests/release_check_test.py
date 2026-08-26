#!/usr/bin/env python3
"""Focused regressions for the exact installable-payload release gate."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
SCRIPTS = SKILL_ROOT / "scripts"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

from release_check import SEMVER_RE  # noqa: E402


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-check-test-")
        self.root = Path(self.temporary.name) / "make-bilingual-study-pdf"
        shutil.copytree(
            SKILL_ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def check(self) -> Path:
        return self.root / "scripts" / "release_check.py"

    @property
    def generator(self) -> Path:
        return REPOSITORY / "tools" / "build_release_manifest.py"

    def run_check(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        process = subprocess.run(
            [sys.executable, str(self.check), *arguments],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        report = json.loads(process.stdout)
        return process, report

    def regenerate(self) -> None:
        subprocess.run(
            [sys.executable, str(self.generator), "--skill-root", str(self.root)],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )

    def assert_failed_with(self, report: dict, fragment: str) -> None:
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(fragment in failure for failure in report["failures"]),
            report["failures"],
        )

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

    def test_current_version_tag_and_manifest_pass(self) -> None:
        version = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        process, report = self.run_check(
            "--expected-version", version, "--tag", f"v{version}"
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")
        self.assertGreater(report["manifest_file_count"], 40)

    def test_wrong_tag_fails(self) -> None:
        process, report = self.run_check("--tag", "v9.9.9")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "release tag mismatch")

    def test_missing_core_file_fails(self) -> None:
        (self.root / "scripts" / "audit_translation.py").unlink()
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "missing payload files: scripts/audit_translation.py")

    def test_extra_file_fails(self) -> None:
        (self.root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "unexpected payload files: unexpected.txt")

    def test_changed_file_byte_fails(self) -> None:
        path = self.root / "scripts" / "audit_translation.py"
        path.write_bytes(path.read_bytes() + b"\n")
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "payload size mismatch for scripts/audit_translation.py")

    def test_same_size_file_mutation_fails_sha256(self) -> None:
        path = self.root / "scripts" / "audit_translation.py"
        payload = bytearray(path.read_bytes())
        payload[0] = ord("!") if payload[0] != ord("!") else ord("#")
        path.write_bytes(payload)
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(
            report, "payload sha256 mismatch for scripts/audit_translation.py"
        )

    def test_ref_value_rejects_suffix_slash_and_duplicate(self) -> None:
        original = (self.root / "README.md").read_text(encoding="utf-8")
        version = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        exact_ref = f"--ref v{version}"
        cases = (
            (f"--ref v{version}_other", f"v{version}_other"),
            (f"--ref v{version}/other", f"v{version}/other"),
            (f"{exact_ref} {exact_ref}", f"v{version}"),
        )
        for replacement, marker in cases:
            with self.subTest(value=marker):
                (self.root / "README.md").write_text(
                    original.replace(exact_ref, replacement, 1),
                    encoding="utf-8",
                    newline="\n",
                )
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assert_failed_with(report, "README installer --ref")

    def test_installer_command_rejects_extra_shell_argv(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        version = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        path.write_text(
            original.replace(
                f"--ref v{version}\n```",
                f"--ref v{version} --method download\n```",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.regenerate()
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "only the documented exact argv")

    def test_manifest_rejects_duplicate_unsorted_case_collision_and_unsafe_path(self) -> None:
        manifest_path = self.root / "release-manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        cases: list[tuple[str, Callable[[dict], None]]] = [
            (
                "duplicate",
                lambda value: value["files"].append(dict(value["files"][0])),
            ),
            ("unsorted", lambda value: value["files"].reverse()),
            (
                "case-collision",
                lambda value: value["files"].append(
                    {**value["files"][0], "path": value["files"][0]["path"].upper()}
                ),
            ),
            (
                "unsafe",
                lambda value: value["files"].append(
                    {**value["files"][0], "path": "../outside"}
                ),
            ),
            (
                "reserved-manifest-case-variant",
                lambda value: value["files"].append(
                    {**value["files"][0], "path": "Release-Manifest.json"}
                ),
            ),
        ]
        for name, mutate in cases:
            with self.subTest(case=name):
                manifest = json.loads(json.dumps(original))
                mutate(manifest)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(report["status"], "failed")

    def test_manifest_rejects_nonportable_windows_and_unicode_names(self) -> None:
        manifest_path = self.root / "release-manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        invalid_names = (
            "CON.txt",
            "folder/name.",
            "folder/name ",
            "folder/name?.txt",
            "cafe\u0301.txt",
        )
        for invalid_name in invalid_names:
            with self.subTest(path=invalid_name):
                manifest = json.loads(json.dumps(original))
                manifest["files"].append(
                    {**manifest["files"][0], "path": invalid_name}
                )
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assert_failed_with(report, "has invalid path")

    def test_future_version_does_not_depend_on_historical_acceptance(self) -> None:
        current = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        future = "999.0.0" if current != "999.0.0" else "998.0.0"
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        readme = readme.replace(f"v{current}", f"v{future}")
        readme = readme.replace(
            f"--expected-version {current}", f"--expected-version {future}"
        )
        (self.root / "README.md").write_text(
            readme, encoding="utf-8", newline="\n"
        )
        (self.root / "VERSION").write_text(
            f"{future}\n", encoding="utf-8", newline="\n"
        )
        self.regenerate()
        process, report = self.run_check(
            "--expected-version", future, "--tag", f"v{future}"
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")

    def test_generated_cache_requires_explicit_ignore(self) -> None:
        cache = self.root / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "local.pyc").write_bytes(b"cache")
        strict, strict_report = self.run_check()
        self.assertNotEqual(strict.returncode, 0)
        self.assert_failed_with(strict_report, "unexpected payload files")
        ignored, ignored_report = self.run_check("--ignore-generated-cache")
        self.assertEqual(ignored.returncode, 0, ignored.stdout + ignored.stderr)
        self.assertEqual(ignored_report["status"], "passed")

    def test_ignore_cache_does_not_hide_arbitrary_files(self) -> None:
        cache = self.root / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "payload.exe").write_bytes(b"not generated bytecode")
        process, report = self.run_check("--ignore-generated-cache")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "unexpected payload files")

    def test_ignore_cache_does_not_hide_symbolic_links(self) -> None:
        cache = self.root / "scripts" / "__pycache__"
        cache.mkdir()
        target = Path(self.temporary.name) / "outside.pyc"
        target.write_bytes(b"outside")
        try:
            os.symlink(target, cache / "local.pyc")
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        process, report = self.run_check("--ignore-generated-cache")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "symbolic links are not allowed")

    def test_manifest_control_file_may_not_be_a_symbolic_link(self) -> None:
        manifest = self.root / "release-manifest.json"
        target = Path(self.temporary.name) / "manifest-target.json"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        try:
            os.symlink(target, manifest)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "symbolic links are not allowed")

    def test_malformed_profiles_fail_without_crashing(self) -> None:
        profile_path = self.root / "profiles" / "assignment-en-zh.json"
        original = json.loads(profile_path.read_text(encoding="utf-8"))
        malformed_values = (
            [],
            {**original, "input": []},
        )
        for malformed in malformed_values:
            with self.subTest(value=malformed):
                profile_path.write_text(
                    json.dumps(malformed, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0, process.stderr)
                self.assertEqual(report["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
