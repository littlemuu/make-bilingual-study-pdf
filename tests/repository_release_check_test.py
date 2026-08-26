#!/usr/bin/env python3
"""Regressions for repository-only release documentation validation."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY / "tools" / "repository_release_check.py"
SKILL_PATH = Path("skills") / "make-bilingual-study-pdf"
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPOSITORY / "tools"))
import repository_release_check as checker_module  # noqa: E402


class RepositoryReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="repository-release-check-test-"
        )
        self.root = Path(self.temporary.name) / "repository"
        (self.root / SKILL_PATH).mkdir(parents=True)
        shutil.copy2(REPOSITORY / "README.md", self.root / "README.md")
        shutil.copy2(
            REPOSITORY / SKILL_PATH / "VERSION", self.root / SKILL_PATH / "VERSION"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_check(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        process = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--repository-root",
                str(self.root),
                *arguments,
            ],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return process, json.loads(process.stdout)

    def assert_failed_with(self, report: dict, fragment: str) -> None:
        self.assertEqual(report["status"], "failed")
        self.assertTrue(
            any(fragment in failure for failure in report["failures"]),
            report["failures"],
        )

    def test_current_repository_documentation_passes(self) -> None:
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        process, report = self.run_check("--expected-version", version)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")

    def test_empty_expected_version_fails(self) -> None:
        process, report = self.run_check("--expected-version", "")
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "expected version ''")

    def test_empty_readme_fails(self) -> None:
        (self.root / "README.md").write_text("", encoding="utf-8")
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "README.md must not be empty")

    def test_commonmark_tilde_fences_are_accepted(self) -> None:
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        (self.root / "README.md").write_text(
            "# Install\n\n"
            "~~~text\n"
            "python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo littlemuu/make-bilingual-study-pdf "
            "--path skills/make-bilingual-study-pdf "
            f"--ref v{version}\n"
            "~~~~\n\n"
            "   ~~~text\n"
            f"python scripts/release_check.py --expected-version {version}\n"
            "   ~~~\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")

    def test_commented_correct_command_does_not_hide_visible_attacker_repo(self) -> None:
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        (self.root / "README.md").write_text(
            "# Install\n\n"
            "<!--\n"
            "```text\n"
            "python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo littlemuu/make-bilingual-study-pdf "
            "--path skills/make-bilingual-study-pdf "
            f"--ref v{version}\n"
            "```\n"
            "-->\n\n"
            "~~~text\n"
            "python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo attacker/malicious-skill "
            "--path skills/make-bilingual-study-pdf "
            f"--ref v{version}\n"
            "~~~\n\n"
            "~~~text\n"
            f"python scripts/release_check.py --expected-version {version}\n"
            "~~~\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "README installer --repo")

    def test_escaped_comment_delimiters_do_not_hide_attacker_command(self) -> None:
        path = self.root / "README.md"
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n\\<!--\n```text\n"
            + "python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            + "--repo attacker/evil --path skills/make-bilingual-study-pdf "
            + f"--ref v{version}\n```\n\\-->\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "exactly one raw")

    def test_container_fences_do_not_hide_attacker_commands(self) -> None:
        path = self.root / "README.md"
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        original = path.read_text(encoding="utf-8")
        attackers = (
            "> ```text\n"
            "> python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo attacker/blockquote --path skills/make-bilingual-study-pdf "
            f"--ref v{version}\n> ```\n",
            "- attacker install:\n"
            "    ```text\n"
            "    python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo attacker/list --path skills/make-bilingual-study-pdf "
            f"--ref v{version}\n    ```\n",
        )
        for attacker in attackers:
            with self.subTest(container=attacker.splitlines()[0]):
                path.write_text(
                    original + "\n" + attacker, encoding="utf-8", newline="\n"
                )
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assert_failed_with(report, "exactly one raw")

    def test_inline_indented_and_unclosed_container_commands_fail_raw_gate(self) -> None:
        path = self.root / "README.md"
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        command = (
            "python \"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py\" "
            "--repo attacker/evil --path skills/make-bilingual-study-pdf "
            f"--ref v{version}"
        )
        original = path.read_text(encoding="utf-8")
        additions = (
            f"Inline visible command: `{command}`\n",
            f"    {command}\n",
            f"> ```text\n> {command}\n",
        )
        for addition in additions:
            with self.subTest(addition=addition.splitlines()[0]):
                path.write_text(
                    original + "\n" + addition, encoding="utf-8", newline="\n"
                )
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assert_failed_with(report, "exactly one raw")

    def test_multiline_installer_command_fails(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original.replace(
                'install-skill-from-github.py" --repo',
                'install-skill-from-github.py"\n--repo',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "one physical line")

    def test_unquoted_placeholder_path_fails(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        path.write_text(
            original.replace(
                '"<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py"',
                "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "canonical line exactly")

    def test_unclosed_html_comment_fails(self) -> None:
        path = self.root / "README.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n<!-- unfinished\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "unclosed HTML comment")

    def test_unclosed_fence_fails(self) -> None:
        path = self.root / "README.md"
        path.write_text(
            path.read_text(encoding="utf-8") + "\n~~~text\nunfinished\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "unclosed fenced code block")

    def test_readme_directory_and_fifo_are_rejected_without_blocking(self) -> None:
        path = self.root / "README.md"
        path.unlink()
        path.mkdir()
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "must be a regular file")

        if not hasattr(os, "mkfifo"):
            return
        path.rmdir()
        os.mkfifo(path)
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "must be a regular file")

    def test_readme_symbolic_link_is_rejected(self) -> None:
        path = self.root / "README.md"
        target = self.root / "README-target.md"
        path.replace(target)
        try:
            path.symlink_to(target.name)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "symbolic link or reparse point")

    def test_version_ancestor_symlink_or_junction_is_rejected(self) -> None:
        skills = self.root / "skills"
        target = self.root / "skills-target"
        skills.replace(target)
        junction_created = False
        try:
            skills.symlink_to(target.name, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                self.skipTest(f"directory symlinks unavailable: {exc}")
            created = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(skills), str(target)],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if created.returncode != 0:
                self.skipTest(
                    "directory symlinks and junctions unavailable: "
                    f"{exc}; {created.stdout}{created.stderr}"
                )
            junction_created = True
        try:
            process, report = self.run_check()
            self.assertNotEqual(process.returncode, 0)
            self.assert_failed_with(report, "repository Skill path")
            self.assert_failed_with(report, "symbolic link or reparse component")
            self.assertFalse(
                any("Skill VERSION" in failure for failure in report["failures"]),
                report["failures"],
            )
        finally:
            if os.path.lexists(skills):
                if junction_created:
                    os.rmdir(skills)
                else:
                    skills.unlink()

    def test_default_root_preserves_and_rejects_repository_link(self) -> None:
        tools = self.root / "tools"
        tools.mkdir()
        shutil.copy2(CHECKER, tools / CHECKER.name)
        linked_root = Path(self.temporary.name) / "repository-link"
        junction_created = False
        if os.name == "nt":
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(linked_root),
                    str(self.root),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if created.returncode != 0:
                self.skipTest(f"junctions unavailable: {created.stdout}{created.stderr}")
            junction_created = True
        else:
            try:
                linked_root.symlink_to(self.root, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
        try:
            process = subprocess.run(
                [sys.executable, str(linked_root / "tools" / CHECKER.name)],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            report = json.loads(process.stdout)
            self.assertNotEqual(process.returncode, 0)
            self.assert_failed_with(report, "repository root")
            self.assert_failed_with(report, "symbolic link or reparse component")
            self.assertFalse(
                any("Skill VERSION" in failure for failure in report["failures"]),
                report["failures"],
            )
        finally:
            if os.path.lexists(linked_root):
                if junction_created:
                    os.rmdir(linked_root)
                else:
                    linked_root.unlink()

    def test_path_swap_between_lstat_and_open_is_rejected(self) -> None:
        path = self.root / "README.md"
        replacement = self.root / "README-replacement.md"
        replacement.write_text("replacement", encoding="utf-8")
        real_open = checker_module.open_read_descriptor

        def replace_then_open(candidate: Path) -> int:
            os.replace(replacement, path)
            return real_open(candidate)

        failures: list[str] = []
        with mock.patch.object(
            checker_module, "open_read_descriptor", side_effect=replace_then_open
        ):
            result = checker_module.read_regular_utf8(path, "repository README.md", failures)
        self.assertIsNone(result)
        self.assertTrue(
            any("changed while it was being opened" in failure for failure in failures),
            failures,
        )

    def test_installer_ref_rejects_suffix_slash_and_duplicate(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        exact_ref = f"--ref v{version}"
        replacements = (
            f"--ref v{version}_other",
            f"--ref v{version}/other",
            f"{exact_ref} {exact_ref}",
        )
        for replacement in replacements:
            with self.subTest(replacement=replacement):
                path.write_text(
                    original.replace(exact_ref, replacement, 1),
                    encoding="utf-8",
                    newline="\n",
                )
                process, report = self.run_check()
                self.assertNotEqual(process.returncode, 0)
                self.assert_failed_with(report, "README installer --ref")

    def test_installer_command_rejects_extra_argv(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        path.write_text(
            original.replace(
                f"--ref v{version}\n```",
                f"--ref v{version} --method download\n```",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "only the documented exact argv")

    def test_installed_verification_command_is_exact(self) -> None:
        path = self.root / "README.md"
        original = path.read_text(encoding="utf-8")
        version = (self.root / SKILL_PATH / "VERSION").read_text(
            encoding="utf-8"
        ).strip()
        path.write_text(
            original.replace(
                f"--expected-version {version}\n```",
                f"--expected-version {version} --ignore-generated-cache\n```",
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "installed verification command")

    def test_second_expected_version_token_fails_raw_gate(self) -> None:
        path = self.root / "README.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\n<!-- hidden duplicate --expected-version -->\n",
            encoding="utf-8",
            newline="\n",
        )
        process, report = self.run_check()
        self.assertNotEqual(process.returncode, 0)
        self.assert_failed_with(report, "exactly one raw --expected-version")

    def test_future_version_does_not_rewrite_historical_acceptance(self) -> None:
        version_path = self.root / SKILL_PATH / "VERSION"
        current = version_path.read_text(encoding="utf-8").strip()
        future = "999.0.0" if current != "999.0.0" else "998.0.0"
        readme_path = self.root / "README.md"
        readme = readme_path.read_text(encoding="utf-8")
        readme = readme.replace(f"v{current}", f"v{future}")
        readme = readme.replace(
            f"--expected-version {current}", f"--expected-version {future}"
        )
        readme_path.write_text(readme, encoding="utf-8", newline="\n")
        version_path.write_text(f"{future}\n", encoding="utf-8", newline="\n")
        process, report = self.run_check("--expected-version", future)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(report["status"], "passed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
