#!/usr/bin/env python3
"""Regression tests for the repository Skill metadata validation gate."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPOSITORY_ROOT / "tools" / "validate_skill.py"
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "make-bilingual-study-pdf"


def parse_args(argv: list[str]) -> tuple[Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--upstream-validator", type=Path, required=True)
    args, remaining = parser.parse_known_args(argv)
    return args.upstream_validator.resolve(), remaining


UPSTREAM_VALIDATOR, UNITTEST_ARGS = parse_args(sys.argv[1:])


class SkillValidatorTest(unittest.TestCase):
    def run_validator(
        self,
        skill_md: str,
        *,
        openai_yaml: str | None = None,
        copy_icons: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="skill-validator-test-") as temp_dir:
            root = Path(temp_dir)
            (root / "SKILL.md").write_text(skill_md, encoding="utf-8", newline="\n")
            (root / "agents").mkdir()
            if openai_yaml is None:
                openai_yaml = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
                    encoding="utf-8"
                )
            (root / "agents" / "openai.yaml").write_text(
                openai_yaml, encoding="utf-8", newline="\n"
            )
            if copy_icons:
                shutil.copytree(SKILL_ROOT / "assets", root / "assets")
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            return subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    str(root),
                    "--upstream-validator",
                    str(UPSTREAM_VALIDATOR),
                ],
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                encoding="utf-8",
                timeout=10,
            )

    def test_current_skill_is_valid(self) -> None:
        result = self.run_validator((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_yaml_fails_closed(self) -> None:
        result = self.run_validator(
            "---\nname: make-bilingual-study-pdf\ndescription: [unterminated\n---\nBody\n"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_non_string_description_fails_closed(self) -> None:
        result = self.run_validator(
            "---\nname: make-bilingual-study-pdf\ndescription: [not, a, string]\n---\nBody\n"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_duplicate_name_fails_closed(self) -> None:
        result = self.run_validator(
            "---\n"
            "name: make-bilingual-study-pdf\n"
            "name: another-valid-name\n"
            "description: Valid description.\n"
            "---\nBody\n"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("duplicate key 'name'", result.stdout + result.stderr)

    def test_invalid_openai_yaml_fails_closed(self) -> None:
        result = self.run_validator(
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            openai_yaml="interface: [unterminated\n",
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("invalid or ambiguous agents/openai.yaml", result.stdout)

    def test_duplicate_openai_yaml_key_fails_closed(self) -> None:
        original = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        duplicate = original.replace(
            '  display_name: "双语学习 PDF"',
            '  display_name: "双语学习 PDF"\n  display_name: "duplicate"',
            1,
        )
        result = self.run_validator(
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            openai_yaml=duplicate,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("duplicate key 'display_name'", result.stdout)

    def test_unquoted_openai_yaml_string_fails_closed(self) -> None:
        original = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        unquoted = original.replace(
            '  display_name: "双语学习 PDF"', "  display_name: 双语学习 PDF", 1
        )
        result = self.run_validator(
            (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
            openai_yaml=unquoted,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("string values must be quoted", result.stdout)

    def test_openai_yaml_requires_skill_token_and_string_values(self) -> None:
        original = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        cases = {
            "missing skill token": original.replace(
                "$make-bilingual-study-pdf", "make-bilingual-study-pdf", 1
            ),
            "skill token with suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf-evil", 1
            ),
            "skill token with Unicode suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf恶", 1
            ),
            "skill token with combining-mark suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf\u0301", 1
            ),
            "skill token with zero-width suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf\u200b", 1
            ),
            "skill token with word-joiner suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf\u2060", 1
            ),
            "skill token with private-use suffix": original.replace(
                "$make-bilingual-study-pdf", "$make-bilingual-study-pdf\ue000", 1
            ),
            "embedded skill token": original.replace(
                "$make-bilingual-study-pdf", "prefix$make-bilingual-study-pdf", 1
            ),
            "double-dollar skill token": original.replace(
                "$make-bilingual-study-pdf", "$$make-bilingual-study-pdf", 1
            ),
            "duplicate skill token": original.replace(
                "$make-bilingual-study-pdf",
                "$make-bilingual-study-pdf and $make-bilingual-study-pdf",
                1,
            ),
            "valid plus combining-mark malformed token": original.replace(
                "$make-bilingual-study-pdf",
                "$make-bilingual-study-pdf and $make-bilingual-study-pdf\u0301",
                1,
            ),
            "valid plus zero-width malformed token": original.replace(
                "$make-bilingual-study-pdf",
                "$make-bilingual-study-pdf and $make-bilingual-study-pdf\u200b",
                1,
            ),
            "valid plus double-dollar malformed token": original.replace(
                "$make-bilingual-study-pdf",
                "$make-bilingual-study-pdf and $$make-bilingual-study-pdf",
                1,
            ),
            "valid plus embedded malformed token": original.replace(
                "$make-bilingual-study-pdf",
                "$make-bilingual-study-pdf and prefix$make-bilingual-study-pdf",
                1,
            ),
            "non-string display name": original.replace(
                'display_name: "双语学习 PDF"', "display_name: true", 1
            ),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                result = self.run_validator(
                    (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
                    openai_yaml=content,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_openai_yaml_rejects_unsafe_and_missing_icons(self) -> None:
        original = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        cases = {
            "unsafe icon": original.replace(
                'icon_small: "./assets/icon.svg"',
                'icon_small: "./assets/../SKILL.md"',
                1,
            ),
            "missing icon": original.replace(
                'icon_small: "./assets/icon.svg"',
                'icon_small: "./assets/missing.svg"',
                1,
            ),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                result = self.run_validator(
                    (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"),
                    openai_yaml=content,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_openai_yaml_policy_and_dependencies_contract(self) -> None:
        skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        original = (SKILL_ROOT / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        valid_dependency = (
            original
            + "dependencies:\n"
            + "  tools:\n"
            + '    - type: "mcp"\n'
            + '      value: "github"\n'
            + '      description: "GitHub MCP server"\n'
            + '      transport: "streamable_http"\n'
            + '      url: "https://api.githubcopilot.com/mcp/"\n'
        )
        valid = self.run_validator(skill_md, openai_yaml=valid_dependency)
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)

        cases = {
            "duplicate product": original.replace(
                '    - "chatgpt"', '    - "chatgpt"\n    - "chatgpt"', 1
            ),
            "non-boolean invocation policy": original.replace(
                "allow_implicit_invocation: true",
                'allow_implicit_invocation: "true"',
                1,
            ),
            "non-MCP dependency": valid_dependency.replace(
                'type: "mcp"', 'type: "http"', 1
            ),
        }
        for label, content in cases.items():
            with self.subTest(case=label):
                result = self.run_validator(skill_md, openai_yaml=content)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *UNITTEST_ARGS])
