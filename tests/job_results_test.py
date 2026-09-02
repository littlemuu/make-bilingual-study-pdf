#!/usr/bin/env python3
"""Adversarial tests for strict aggregate-job dependency evaluation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "tools" / "check_job_results.py"
SPEC = importlib.util.spec_from_file_location("check_job_results", CHECKER)
assert SPEC and SPEC.loader
check_job_results = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_job_results
SPEC.loader.exec_module(check_job_results)


def encoded(**results: object) -> str:
    return json.dumps({job: {"result": result} for job, result in results.items()})


class JobResultsTests(unittest.TestCase):
    def test_all_exact_success_results_pass(self) -> None:
        check_job_results.require_all_success(
            encoded(lint="success", tests="success"), ("lint", "tests")
        )

    def test_every_non_success_conclusion_fails_closed(self) -> None:
        for result in (
            "failure",
            "cancelled",
            "skipped",
            "neutral",
            "timed_out",
            "action_required",
            "",
            None,
        ):
            with self.subTest(result=result), self.assertRaisesRegex(
                check_job_results.ResultError, "tests="
            ):
                check_job_results.require_all_success(
                    encoded(lint="success", tests=result), ("lint", "tests")
                )

    def test_missing_job_or_result_field_fails_closed(self) -> None:
        cases = (
            json.dumps({"lint": {"result": "success"}}),
            json.dumps({"lint": {"result": "success"}, "tests": {}}),
            json.dumps({"lint": {"result": "success"}, "tests": None}),
        )
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaisesRegex(
                check_job_results.ResultError, "tests=missing"
            ):
                check_job_results.require_all_success(raw, ("lint", "tests"))

    def test_malformed_or_non_object_json_fails_closed(self) -> None:
        for raw in ("", "{", "[]", "null", '"success"'):
            with self.subTest(raw=raw), self.assertRaises(check_job_results.ResultError):
                check_job_results.require_all_success(raw, ("lint",))

    def test_unexpected_or_duplicate_job_names_fail_closed(self) -> None:
        with self.assertRaisesRegex(check_job_results.ResultError, "unexpected"):
            check_job_results.require_all_success(
                encoded(lint="success", hidden="success"), ("lint",)
            )
        for required in ((), ("lint", "lint")):
            with self.subTest(required=required), self.assertRaisesRegex(
                check_job_results.ResultError, "non-empty and unique"
            ):
                check_job_results.require_all_success(encoded(lint="success"), required)


if __name__ == "__main__":
    unittest.main()
