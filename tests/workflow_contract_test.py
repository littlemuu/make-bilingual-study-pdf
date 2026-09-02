#!/usr/bin/env python3
"""Adversarial regressions for workflow tier and migration contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_workflow_contract.py"
SPEC = importlib.util.spec_from_file_location("check_workflow_contract", CHECKER)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / ".github").mkdir()
        shutil.copytree(ROOT / ".github" / "workflows", self.root / ".github" / "workflows")
        (self.root / "docs").mkdir()
        shutil.copy2(ROOT / "docs" / "development.md", self.root / "docs" / "development.md")
        shutil.copy2(
            ROOT / "docs" / "ruleset-migration-state.json",
            self.root / "docs" / "ruleset-migration-state.json",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _replace(self, relative: str, old: str, new: str) -> None:
        path = self.root / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_current_contract_passes(self) -> None:
        checker.validate_contracts(ROOT)

    def test_missing_required_context_is_rejected(self) -> None:
        self._replace(".github/workflows/baseline.yml", "  macos-filesystem:\n", "  retired-macos:\n")
        with self.assertRaisesRegex(checker.ContractError, "required job macos-filesystem"):
            checker.validate_contracts(self.root)

    def test_conditionally_skipped_required_context_is_rejected(self) -> None:
        self._replace(
            ".github/workflows/baseline.yml",
            "  windows-filesystem:\n    runs-on:",
            "  windows-filesystem:\n    if: false\n    runs-on:",
        )
        with self.assertRaisesRegex(checker.ContractError, "job-level if"):
            checker.validate_contracts(self.root)

    def test_tier_aggregate_without_always_is_rejected(self) -> None:
        self._replace(
            ".github/workflows/baseline.yml",
            "    if: ${{ always() }}\n",
            "",
        )
        with self.assertRaisesRegex(checker.ContractError, "exact always-run"):
            checker.validate_contracts(self.root)

    def test_tier_aggregate_missing_one_result_is_rejected(self) -> None:
        self._replace(
            ".github/workflows/baseline.yml",
            "            automated-forward\n",
            "",
        )
        with self.assertRaisesRegex(checker.ContractError, "every dependency result"):
            checker.validate_contracts(self.root)

    def test_safety_without_fault_injection_is_rejected(self) -> None:
        self._replace(".github/workflows/safety.yml", "  fault-injection:\n", "  retired-faults:\n")
        with self.assertRaisesRegex(checker.ContractError, "missing a required evidence job"):
            checker.validate_contracts(self.root)

    def test_safety_aggregate_without_always_is_rejected(self) -> None:
        self._replace(
            ".github/workflows/safety.yml",
            "    if: ${{ always() }}\n",
            "",
        )
        with self.assertRaisesRegex(checker.ContractError, "exact always-run"):
            checker.validate_contracts(self.root)

    def test_release_without_fresh_safety_gate_is_rejected(self) -> None:
        self._replace(
            ".github/workflows/release.yml",
            "SAFETY_MAX_AGE_HOURS: 168",
            "REMOVED_SAFETY_AGE: 168",
        )
        with self.assertRaisesRegex(checker.ContractError, "SAFETY_MAX_AGE_HOURS"):
            checker.validate_contracts(self.root)

    def test_ruleset_plan_cannot_authorize_its_own_live_write(self) -> None:
        state_path = self.root / "docs" / "ruleset-migration-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["live_write_authorized"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(checker.ContractError, "must not authorize"):
            checker.validate_contracts(self.root)

    def test_development_command_duplication_drift_is_rejected(self) -> None:
        self._replace(
            "docs/development.md",
            "python tools/run_test_suite.py full",
            "python tests/v23_profile_test.py",
        )
        with self.assertRaisesRegex(checker.ContractError, "canonical full suite"):
            checker.validate_contracts(self.root)


if __name__ == "__main__":
    unittest.main()
