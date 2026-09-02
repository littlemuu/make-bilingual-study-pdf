#!/usr/bin/env python3
"""Contract tests for the canonical CI test-suite command registry."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools" / "run_test_suite.py"
SPEC = importlib.util.spec_from_file_location("run_test_suite", RUNNER)
assert SPEC and SPEC.loader
run_test_suite = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run_test_suite
SPEC.loader.exec_module(run_test_suite)


class TestSuiteContractTests(unittest.TestCase):
    def test_every_suite_has_unique_known_commands(self) -> None:
        for suite, command_ids in run_test_suite.SUITES.items():
            with self.subTest(suite=suite):
                self.assertTrue(command_ids)
                self.assertEqual(len(command_ids), len(set(command_ids)))
                self.assertFalse(set(command_ids).difference(run_test_suite.COMMANDS))

    def test_full_suite_is_the_complete_command_registry(self) -> None:
        self.assertEqual(
            set(run_test_suite.SUITES["full"]), set(run_test_suite.COMMANDS)
        )

    def test_pr_fast_keeps_gates_core_and_profiles(self) -> None:
        command_ids = set(run_test_suite.SUITES["pr-fast"])
        for required in (
            "repository-release-check",
            "workflow-contract-regressions",
            "safe-artifact-regressions",
            "visual-gate-regressions",
            "validate-assignment-profile",
            "validate-paper-profile",
            "validate-lecture-profile",
        ):
            self.assertIn(required, command_ids)
        for main_or_safety_only in (
            "installed-release-regressions",
            "work-boundary-regressions",
            "translation-boundary-regressions",
            "output-boundary-regressions",
            "docx-boundary-regressions",
            "compile-boundary-regressions",
            "macos-alias-regressions",
        ):
            self.assertNotIn(main_or_safety_only, command_ids)

    def test_list_json_is_machine_readable_and_does_not_execute(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "windows-smoke", "--list-json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            json.loads(completed.stdout),
            list(run_test_suite.SUITES["windows-smoke"]),
        )

    def test_validator_suite_fails_closed_without_validator_path(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "metadata", "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("requires --upstream-validator", completed.stderr)


if __name__ == "__main__":
    unittest.main()
