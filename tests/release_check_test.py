#!/usr/bin/env python3
"""Focused regressions for the exact installable-payload release gate."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
SCRIPTS = SKILL_ROOT / "scripts"
TOOLS = REPOSITORY / "tools"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TOOLS))

import _payload_fs as payload_fs  # noqa: E402
import build_release_manifest as manifest_builder  # noqa: E402
from release_check import (  # noqa: E402
    SEMVER_RE,
    valid_manifest_path,
    validate_yaml_quoted_strings,
)

valid_payload_path = manifest_builder.valid_payload_path


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="release-check-test-")
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
            timeout=5,
        )
        report = json.loads(process.stdout)
        return process, report

    def regenerate(self) -> None:
        process = subprocess.run(
            [sys.executable, str(self.generator), "--skill-root", str(self.root)],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def run_generator(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.run_generator_for(self.root, *arguments)

    def run_generator_for(
        self, root: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(self.generator),
                "--skill-root",
                str(root),
                *arguments,
            ],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def run_generator_in_process(self) -> tuple[int, str]:
        output = io.StringIO()
        with (
            mock.patch.object(
                sys,
                "argv",
                ["build_release_manifest.py", "--skill-root", str(self.root)],
            ),
            contextlib.redirect_stdout(output),
        ):
            result = manifest_builder.main()
        return result, output.getvalue()

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

    def test_manifest_builder_can_create_initial_manifest(self) -> None:
        manifest = self.root / "release-manifest.json"
        manifest.unlink()
        process = self.run_generator()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertTrue(manifest.is_file())
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(payload["skill"], "make-bilingual-study-pdf")
        self.assertGreater(len(payload["files"]), 40)

    def test_manifest_builder_preserves_committed_bytes_exactly(self) -> None:
        manifest = self.root / "release-manifest.json"
        original = manifest.read_bytes()

        result, output = self.run_generator_in_process()

        self.assertEqual(result, 0, output)
        self.assertEqual(manifest.read_bytes(), original)

    def test_manifest_partial_write_failure_preserves_original_and_cleans_up(
        self,
    ) -> None:
        manifest = self.root / "release-manifest.json"
        original = manifest.read_bytes()

        def fail_after_partial_write(descriptor: int, payload: bytes) -> None:
            os.write(descriptor, payload[:7])
            raise OSError("simulated manifest write failure")

        with mock.patch.object(
            payload_fs, "write_all", side_effect=fail_after_partial_write
        ):
            result, output = self.run_generator_in_process()

        self.assertNotEqual(result, 0, output)
        self.assertIn("simulated manifest write failure", output)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".release-manifest.json.*")), [])

    def test_manifest_target_swap_is_not_overwritten_and_temp_is_cleaned(
        self,
    ) -> None:
        manifest = self.root / "release-manifest.json"
        replacement = self.root / "replacement-manifest.json"
        intruder = b"concurrent replacement\n"
        replacement.write_bytes(intruder)
        real_atomic_replace = manifest_builder.atomic_replace_bytes

        def replace_then_publish(*args: object, **kwargs: object) -> None:
            os.replace(replacement, manifest)
            real_atomic_replace(*args, **kwargs)

        with mock.patch.object(
            manifest_builder,
            "atomic_replace_bytes",
            side_effect=replace_then_publish,
        ):
            result, output = self.run_generator_in_process()

        self.assertNotEqual(result, 0, output)
        self.assertIn("changed after inventory", output)
        self.assertEqual(manifest.read_bytes(), intruder)
        self.assertEqual(list(self.root.glob(".release-manifest.json.*")), [])

    def test_payload_helper_is_the_only_low_level_owner(self) -> None:
        forbidden_definitions = {
            "atomic_replace_bytes",
            "best_effort_fsync_directory",
            "directory_identity_matches",
            "ensure_safe_parent_chain",
            "hash_regular_file",
            "is_reparse_point",
            "iter_safe_files",
            "lexical_absolute",
            "metadata_matches",
            "open_descriptor",
            "open_read_descriptor",
            "open_regular_fd",
            "portable_path_key",
            "reject_unsafe_status",
            "repository_directory_paths",
            "unsafe_link_kind",
            "valid_payload_path",
            "validate_directory_components",
            "write_all",
        }
        callers = (
            TOOLS / "check_skill_eol.py",
            TOOLS / "build_release_manifest.py",
            TOOLS / "repository_release_check.py",
        )
        for caller in callers:
            with self.subTest(caller=caller.name):
                tree = ast.parse(caller.read_text(encoding="utf-8"))
                imports_helper = any(
                    isinstance(node, ast.ImportFrom)
                    and node.module == "_payload_fs"
                    for node in tree.body
                )
                definitions = {
                    node.name
                    for node in tree.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertTrue(imports_helper)
                self.assertEqual(definitions & forbidden_definitions, set())

    def test_manifest_builder_rejects_hardlink_without_touching_target(self) -> None:
        manifest = self.root / "release-manifest.json"
        original_manifest = manifest.read_bytes()
        outside = Path(self.temporary.name) / "outside-hardlink-target.txt"
        outside.write_bytes(b"outside\r\n")
        link = self.root / "outside-hardlink.txt"
        try:
            os.link(outside, link)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        process = self.run_generator()

        self.assertNotEqual(process.returncode, 0)
        self.assertIn(
            "multiply linked files are not allowed",
            process.stdout + process.stderr,
        )
        self.assertEqual(outside.read_bytes(), b"outside\r\n")
        self.assertEqual(link.read_bytes(), b"outside\r\n")
        self.assertEqual(manifest.read_bytes(), original_manifest)

    def test_wrong_tag_fails(self) -> None:
        process, report = self.run_check("--tag", "v9.9.9")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "release tag mismatch")

    def test_empty_expected_version_fails(self) -> None:
        process, report = self.run_check("--expected-version", "")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "expected version ''")

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
            "CONIN$",
            "conin$.txt",
            "CoNoUt$.LoG",
            "COM0",
            "com0.txt",
            "LPT0",
            "lPt0.bin",
            "COM¹.txt",
            "COM².txt",
            "COM³.txt",
            "LPT¹.txt",
            "LPT².txt",
            "LPT³.txt",
            "NUL .txt",
            "COM1 .bin",
            "CONIN$ .x",
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

    def test_windows_device_name_rules_cover_case_and_extensions(self) -> None:
        invalid_names = (
            "CONIN$",
            "conin$.txt",
            "CoNoUt$.LoG",
            "COM0",
            "com0.txt",
            "LPT0",
            "lPt0.bin",
            "NUL .txt",
            "COM1 .bin",
            "CONIN$ .x",
        )
        valid_names = (
            "coninput.txt",
            "conoutput.txt",
            "com10.txt",
            "lpt10.txt",
            "NULL .txt",
            "COM10 .bin",
            "CONINX$ .x",
        )
        for invalid_name in invalid_names:
            with self.subTest(path=invalid_name):
                self.assertFalse(valid_payload_path(invalid_name))
                self.assertFalse(valid_manifest_path(invalid_name))
        for valid_name in valid_names:
            with self.subTest(path=valid_name):
                self.assertTrue(valid_payload_path(valid_name))
                self.assertTrue(valid_manifest_path(valid_name))

    def test_manifest_builder_rejects_windows_device_names(self) -> None:
        invalid_names = (
            "CONIN$",
            "conin$.txt",
            "CoNoUt$.LoG",
            "COM0",
            "com0.txt",
            "LPT0",
            "lPt0.bin",
            "COM¹.txt",
            "NUL .txt",
            "COM1 .bin",
            "CONIN$ .x",
        )
        for invalid_name in invalid_names:
            with self.subTest(path=invalid_name):
                self.assertFalse(valid_payload_path(invalid_name))
                path = self.root / invalid_name
                try:
                    path.write_text("reserved\n", encoding="utf-8")
                except OSError:
                    # Win32 may reject DOS device names before the builder sees them.
                    self.assertEqual(os.name, "nt")
                    continue
                try:
                    process = self.run_generator()
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn(
                        "payload path is not portable",
                        process.stdout + process.stderr,
                    )
                finally:
                    path.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX ancestor-symlink regression")
    def test_manifest_builder_rejects_skills_ancestor_symlink(self) -> None:
        skills = self.repository / "skills"
        outside_skills = Path(self.temporary.name) / "outside-skills"
        skills.rename(outside_skills)
        outside_manifest = (
            outside_skills / "make-bilingual-study-pdf" / "release-manifest.json"
        )
        original = outside_manifest.read_bytes()
        os.symlink(outside_skills, skills, target_is_directory=True)
        try:
            process = self.run_generator_for(
                skills / "make-bilingual-study-pdf"
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("symbolic links are not allowed", process.stdout)
            self.assertIn("skills directory", process.stdout)
            self.assertEqual(outside_manifest.read_bytes(), original)
        finally:
            skills.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX repository-symlink regression")
    def test_manifest_builder_rejects_repository_symlink(self) -> None:
        original = (self.root / "release-manifest.json").read_bytes()
        linked_repository = Path(self.temporary.name) / "repository-link"
        os.symlink(self.repository, linked_repository, target_is_directory=True)
        try:
            process = self.run_generator_for(
                linked_repository / "skills" / "make-bilingual-study-pdf"
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("symbolic links are not allowed", process.stdout)
            self.assertIn("repository root", process.stdout)
            self.assertEqual((self.root / "release-manifest.json").read_bytes(), original)
        finally:
            linked_repository.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX repository-ancestor regression")
    def test_manifest_builder_rejects_repository_ancestor_symlink(self) -> None:
        real_parent = Path(self.temporary.name) / "real-parent"
        real_parent.mkdir()
        actual_repository = real_parent / "repository"
        self.repository.rename(actual_repository)
        actual_root = (
            actual_repository / "skills" / "make-bilingual-study-pdf"
        )
        outside_manifest = actual_root / "release-manifest.json"
        original = outside_manifest.read_bytes()
        linked_parent = Path(self.temporary.name) / "parent-link"
        os.symlink(real_parent, linked_parent, target_is_directory=True)
        try:
            process = self.run_generator_for(
                linked_parent
                / "repository"
                / "skills"
                / "make-bilingual-study-pdf"
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("symbolic links are not allowed", process.stdout)
            self.assertIn("repository ancestor", process.stdout)
            self.assertEqual(outside_manifest.read_bytes(), original)
        finally:
            linked_parent.unlink()

    def test_skill_frontmatter_rejects_malformed_non_string_and_duplicates(self) -> None:
        path = self.root / "SKILL.md"
        original = path.read_text(encoding="utf-8")
        cases = {
            "malformed collection": original.replace(
                "description: Convert an English academic PDF",
                "description: [unterminated",
                1,
            ),
            "non-string description": re.sub(
                r"(?m)^description:.*$", "description: true", original, count=1
            ),
            "non-string name": original.replace(
                "name: make-bilingual-study-pdf", "name: [make-bilingual-study-pdf]", 1
            ),
            "duplicate name": original.replace(
                "name: make-bilingual-study-pdf",
                "name: make-bilingual-study-pdf\nname: duplicate",
                1,
            ),
            "duplicate description": re.sub(
                r"(?m)^(description:.*)$", r"\1\ndescription: duplicate", original, count=1
            ),
            "comment-only description": re.sub(
                r"(?m)^description:.*$", "description: # comment", original, count=1
            ),
            "bare question indicator": re.sub(
                r"(?m)^description:.*$", "description: ?", original, count=1
            ),
            "bare mapping indicator": re.sub(
                r"(?m)^description:.*$", "description: :", original, count=1
            ),
            "bare sequence indicator": re.sub(
                r"(?m)^description:.*$", "description: -", original, count=1
            ),
            "unexpected frontmatter key": original.replace(
                "description: Convert an English academic PDF",
                "metadata: forbidden-by-current-contract\n"
                "description: Convert an English academic PDF",
                1,
            ),
            "malformed frontmatter line": original.replace(
                "description: Convert an English academic PDF",
                "not-a-mapping-entry\n"
                "description: Convert an English academic PDF",
                1,
            ),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                path.write_text(content, encoding="utf-8", newline="\n")
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0, process.stderr)
                self.assertTrue(
                    any("SKILL.md frontmatter" in item for item in report["failures"]),
                    report["failures"],
                )
                path.write_text(original, encoding="utf-8", newline="\n")

    def test_yaml_metadata_rejects_surrogates_and_preserves_non_bmp(self) -> None:
        skill_path = self.root / "SKILL.md"
        openai_path = self.root / "agents" / "openai.yaml"
        original_skill = skill_path.read_text(encoding="utf-8")
        original_openai = openai_path.read_text(encoding="utf-8")
        description_line = next(
            line
            for line in original_skill.splitlines()
            if line.startswith("description:")
        )

        invalid_skill_values = {
            "high surrogate": 'description: "\\uD800"',
            "low surrogate": 'description: "\\uDFFF"',
            "surrogate pair code units": 'description: "\\uD83D\\uDE00"',
            "eight-digit surrogate": 'description: "\\U0000D800"',
        }
        for label, replacement in invalid_skill_values.items():
            with self.subTest(document="SKILL.md", case=label):
                skill_path.write_text(
                    original_skill.replace(description_line, replacement, 1),
                    encoding="utf-8",
                    newline="\n",
                )
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0, process.stderr)
                self.assert_failed_with(report, "surrogate code points")
        skill_path.write_text(original_skill, encoding="utf-8", newline="\n")

        invalid_openai_values = {
            "interface value": original_openai.replace(
                'display_name: "双语学习 PDF"', 'display_name: "\\uD800"', 1
            ),
            "nested value": original_openai.replace(
                '- "chatgpt"', '- "\\uDFFF"', 1
            ),
            "surrogate pair code units": original_openai.replace(
                'display_name: "双语学习 PDF"',
                'display_name: "\\uD83D\\uDE00"',
                1,
            ),
            "eight-digit surrogate": original_openai.replace(
                'display_name: "双语学习 PDF"',
                'display_name: "\\U0000D800"',
                1,
            ),
            "mapping key": original_openai.replace(
                "  display_name:", '  "\\uD800":', 1
            ),
            "flow mapping key without separation": (
                original_openai + '\nscanner_probe: {"\\uD800":"safe"}\n'
            ),
            "nested flow value without separation": (
                original_openai
                + '\nscanner_probe: {"outer":{"value":"\\uD800"}}\n'
            ),
            "anchor property before scalar": original_openai.replace(
                'display_name: "双语学习 PDF"',
                'display_name: &display "\\uD800"',
                1,
            ),
            "tag property before scalar": original_openai.replace(
                'display_name: "双语学习 PDF"',
                'display_name: !!str "\\uD800"',
                1,
            ),
            "flow anchor property before scalar": (
                original_openai
                + '\nscanner_probe: {"outer":&display "\\uD800"}\n'
            ),
        }
        for label, content in invalid_openai_values.items():
            with self.subTest(document="openai.yaml", case=label):
                openai_path.write_text(content, encoding="utf-8", newline="\n")
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0, process.stderr)
                self.assert_failed_with(report, "surrogate code points")
        openai_path.write_text(original_openai, encoding="utf-8", newline="\n")

        valid_cases = (
            (
                original_skill.replace(
                    description_line, 'description: "Valid 😀 metadata"', 1
                ),
                original_openai.replace(
                    '"双语学习 PDF"', '"😀 双语学习 PDF"', 1
                ),
            ),
            (
                original_skill.replace(
                    description_line,
                    'description: "Valid \\U0001F600 metadata"',
                    1,
                ),
                original_openai.replace(
                    '"双语学习 PDF"', '"\\U0001F600 双语学习 PDF"', 1
                ),
            ),
            (
                original_skill.replace(
                    description_line,
                    'description: "Literal \\\\uD800 text is safe"',
                    1,
                ),
                original_openai.replace(
                    '"双语学习 PDF"', '"Literal \\\\uD800 text"', 1
                ),
            ),
            (
                original_skill.replace(
                    description_line,
                    "description: Don't reject a plain apostrophe",
                    1,
                ),
                original_openai.replace(
                    '"双语学习 PDF"', '"Don\'t reject \\"quoted\\" text"', 1
                ),
            ),
            (
                original_skill.replace(
                    description_line,
                    "description: Visit https://example.com/a:b and keep "
                    'embedded "\\uD800" literal',
                    1,
                ),
                original_openai.replace(
                    'display_name: "双语学习 PDF"',
                    'display_name: "Valid multiline\n      😀 metadata"',
                    1,
                ),
            ),
        )
        for index, (skill_content, openai_content) in enumerate(valid_cases):
            with self.subTest(valid_yaml_case=index):
                skill_path.write_text(
                    skill_content, encoding="utf-8", newline="\n"
                )
                openai_path.write_text(
                    openai_content, encoding="utf-8", newline="\n"
                )
                self.regenerate()
                process, report = self.run_check()
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertEqual(report["status"], "passed")

    def test_yaml_scanner_distinguishes_flow_tokens_from_plain_scalars(self) -> None:
        invalid = (
            r'probe: {"\uD800":"safe"}',
            r'probe: {"outer":{"nested":"\uDFFF"}}',
            r'probe: {"outer":!!str "\uD800"}',
            r'probe: {"outer":&value "\uDFFF"}',
            'anchor: &name "key"\n' + r'probe: {*name:"\uD800"}',
            '\ufeff' + r'{"interface":{"display_name":"\uD800"}}',
            r'--- {"interface":{"display_name":"\uDFFF"}}',
        )
        for content in invalid:
            with self.subTest(invalid=content):
                with self.assertRaisesRegex(ValueError, "surrogate code points"):
                    validate_yaml_quoted_strings(content, label="probe")

        valid = (
            r'probe: {url: https://example.com/a:b, note: plain "\uD800" text}',
            "description: Don't reject plain apostrophes or \"quotes\"",
            'anchor: &name "safe"\nprobe: *name',
        )
        for content in valid:
            with self.subTest(valid=content):
                validate_yaml_quoted_strings(content, label="probe")

    def test_empty_skill_markdown_fails(self) -> None:
        path = self.root / "SKILL.md"
        path.write_bytes(b"")
        self.regenerate()
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0, process.stderr)
        self.assert_failed_with(report, "SKILL.md must not be empty")

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_windows_junction_is_rejected_without_traversal(self) -> None:
        target = Path(self.temporary.name) / "junction-target"
        target.mkdir()
        secret = target / "must-not-be-traversed.txt"
        secret.write_text("outside payload\n", encoding="utf-8")
        junction = self.root / "junction-payload"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        try:
            check, report = self.run_check()
            self.assertNotEqual(check.returncode, 0)
            self.assert_failed_with(report, "reparse points are not allowed")
            self.assertFalse(
                any("must-not-be-traversed.txt" in item for item in report["failures"]),
                report["failures"],
            )
            generator = self.run_generator()
            self.assertNotEqual(generator.returncode, 0)
            self.assertIn(
                "reparse points are not allowed", generator.stdout + generator.stderr
            )
            self.assertTrue(secret.is_file())
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)
        self.assertTrue(secret.is_file())

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_manifest_builder_rejects_skills_ancestor_junction(self) -> None:
        skills = self.repository / "skills"
        outside_skills = Path(self.temporary.name) / "outside-skills"
        skills.rename(outside_skills)
        outside_manifest = (
            outside_skills / "make-bilingual-study-pdf" / "release-manifest.json"
        )
        original = outside_manifest.read_bytes()
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
            process = self.run_generator_for(
                skills / "make-bilingual-study-pdf"
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("reparse points are not allowed", process.stdout)
            self.assertIn("skills directory", process.stdout)
            self.assertEqual(outside_manifest.read_bytes(), original)
        finally:
            if os.path.lexists(skills):
                os.rmdir(skills)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_non_regular_fifo_fails_closed(self) -> None:
        fifo = self.root / "payload.pipe"
        os.mkfifo(fifo)
        check, report = self.run_check()
        self.assertNotEqual(check.returncode, 0)
        self.assert_failed_with(report, "non-regular filesystem entries")
        generator = self.run_generator()
        self.assertNotEqual(generator.returncode, 0)
        self.assertIn(
            "non-regular filesystem entries", generator.stdout + generator.stderr
        )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_version_fifo_fails_without_opening_it(self) -> None:
        version = self.root / "VERSION"
        version.unlink()
        os.mkfifo(version)
        generator = self.run_generator()
        self.assertNotEqual(generator.returncode, 0)
        self.assertIn(
            "non-regular filesystem entries are not allowed in payload: VERSION",
            generator.stdout + generator.stderr,
        )
        check, report = self.run_check()
        self.assertNotEqual(check.returncode, 0)
        self.assert_failed_with(report, "non-regular filesystem entries")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_manifest_fifo_fails_without_opening_it(self) -> None:
        manifest = self.root / "release-manifest.json"
        manifest.unlink()
        os.mkfifo(manifest)
        generator = self.run_generator()
        self.assertNotEqual(generator.returncode, 0)
        self.assertIn(
            "non-regular filesystem entries are not allowed in payload: "
            "release-manifest.json",
            generator.stdout + generator.stderr,
        )
        check, report = self.run_check()
        self.assertNotEqual(check.returncode, 0)
        self.assert_failed_with(report, "non-regular filesystem entries")

    def test_future_version_does_not_depend_on_historical_acceptance(self) -> None:
        current = (self.root / "VERSION").read_text(encoding="utf-8").strip()
        future = "999.0.0" if current != "999.0.0" else "998.0.0"
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

    def test_profiles_reject_duplicate_keys_and_non_integer_schema_versions(self) -> None:
        profile_path = self.root / "profiles" / "assignment-en-zh.json"
        original = profile_path.read_text(encoding="utf-8")
        cases = {
            "duplicate schema_version": (
                original.replace(
                    '  "schema_version": 1,',
                    '  "schema_version": 1,\n  "schema_version": 1,',
                    1,
                ),
                "duplicate JSON object key: 'schema_version'",
            ),
            "floating-point schema_version": (
                original.replace('  "schema_version": 1,', '  "schema_version": 1.0,', 1),
                "schema_version must be an integer",
            ),
            "boolean schema_version": (
                original.replace('  "schema_version": 1,', '  "schema_version": true,', 1),
                "schema_version must be an integer",
            ),
            "boolean coverage threshold": (
                original.replace(
                    '"minimum_global_fivegram_coverage": 0.95',
                    '"minimum_global_fivegram_coverage": false',
                    1,
                ),
                "qa.minimum_global_fivegram_coverage must be a finite number",
            ),
            "boolean native text ratio": (
                original.replace(
                    '"minimum_native_text_page_ratio": 0.7',
                    '"minimum_native_text_page_ratio": false',
                    1,
                ),
                "input.minimum_native_text_page_ratio must be a finite number",
            ),
            "huge native text ratio": (
                original.replace(
                    '"minimum_native_text_page_ratio": 0.7',
                    '"minimum_native_text_page_ratio": ' + "1" + "0" * 400,
                    1,
                ),
                "input.minimum_native_text_page_ratio must be a finite number",
            ),
            "zero warning threshold": (
                original.replace(
                    '"warn_page_below": 0.75', '"warn_page_below": 0', 1
                ),
                "qa.warn_page_below must be a finite number",
            ),
            "NaN coverage threshold": (
                original.replace(
                    '"minimum_global_fivegram_coverage": 0.95',
                    '"minimum_global_fivegram_coverage": NaN',
                    1,
                ),
                "non-finite JSON number is not allowed: NaN",
            ),
            "Infinity coverage threshold": (
                original.replace(
                    '"minimum_global_fivegram_coverage": 0.95',
                    '"minimum_global_fivegram_coverage": Infinity',
                    1,
                ),
                "non-finite JSON number is not allowed: Infinity",
            ),
            "negative Infinity warning threshold": (
                original.replace(
                    '"warn_page_below": 0.75',
                    '"warn_page_below": -Infinity',
                    1,
                ),
                "non-finite JSON number is not allowed: -Infinity",
            ),
            "overflow coverage threshold": (
                original.replace(
                    '"minimum_global_fivegram_coverage": 0.95',
                    '"minimum_global_fivegram_coverage": -1e9999',
                    1,
                ),
                "JSON number is outside the finite float range",
            ),
            "unpaired surrogate": (
                original.replace(
                    '"header_label": "Bilingual study edition"',
                    '"header_label": "\\ud800"',
                    1,
                ),
                "JSON strings must not contain unpaired surrogates",
            ),
        }
        for label, (content, expected_failure) in cases.items():
            with self.subTest(case=label):
                profile_path.write_text(content, encoding="utf-8", newline="\n")
                self.regenerate()
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0, process.stderr)
                self.assert_failed_with(report, expected_failure)
                profile_path.write_text(original, encoding="utf-8", newline="\n")

    def test_profiles_preserve_valid_non_bmp_unicode(self) -> None:
        profile_path = self.root / "profiles" / "assignment-en-zh.json"
        original = profile_path.read_text(encoding="utf-8")
        profile_path.write_text(
            original.replace(
                '"header_label": "Bilingual study edition"',
                '"header_label": "Assignment \\ud83d\\ude00"',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.regenerate()
        process, report = self.run_check()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")

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
