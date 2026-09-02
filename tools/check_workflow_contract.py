#!/usr/bin/env python3
"""Fail closed when the staged CI/ruleset migration workflow contract drifts."""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CONTEXTS = (
    "workflow-lint",
    "self-test",
    "windows-filesystem",
    "macos-filesystem",
    "installer-parity",
    "automated-forward",
)


class ContractError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a mapping")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a mapping")
    return value


def _sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractError(f"{label} must be a sequence")
    return value


def _run_text(job: dict[str, Any]) -> str:
    steps = _sequence(job.get("steps"), "job steps")
    return "\n".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))


def _require_triggers(workflow: dict[str, Any], expected: set[str], label: str) -> None:
    triggers = _mapping(workflow.get("on"), f"{label} triggers")
    missing = expected.difference(triggers)
    if missing:
        raise ContractError(f"{label} is missing triggers: {sorted(missing)}")


def _validate_aggregate(
    job: dict[str, Any], expected_needs: set[str], label: str
) -> None:
    if job.get("if") != "${{ always() }}":
        raise ContractError(f"{label} must use the exact always-run condition")
    if set(_sequence(job.get("needs"), f"{label} needs")) != expected_needs:
        raise ContractError(f"{label} must depend on every required evidence job")

    evaluator_step: dict[str, Any] | None = None
    for step in _sequence(job.get("steps"), f"{label} steps"):
        if isinstance(step, dict) and "tools/check_job_results.py" in str(step.get("run", "")):
            evaluator_step = step
            break
    if evaluator_step is None:
        raise ContractError(f"{label} must call the strict job-result evaluator")
    env = _mapping(evaluator_step.get("env"), f"{label} evaluator environment")
    if env.get("CI_NEEDS_JSON") != "${{ toJSON(needs) }}":
        raise ContractError(f"{label} must pass the complete needs result object")
    try:
        tokens = shlex.split(str(evaluator_step["run"]))
        require_index = tokens.index("--require")
    except (ValueError, KeyError) as exc:
        raise ContractError(f"{label} evaluator must declare required job names") from exc
    required_tokens = [token for token in tokens[require_index + 1 :] if token.strip()]
    if len(required_tokens) != len(expected_needs) or set(required_tokens) != expected_needs:
        raise ContractError(f"{label} evaluator must check every dependency result")


def validate_contracts(root: Path = ROOT) -> None:
    workflows = root / ".github" / "workflows"
    baseline = _load(workflows / "baseline.yml")
    safety = _load(workflows / "safety.yml")
    release = _load(workflows / "release.yml")

    _require_triggers(baseline, {"pull_request", "push", "workflow_dispatch"}, "baseline")
    push = _mapping(_mapping(baseline["on"], "baseline triggers")["push"], "push")
    if push.get("branches") != ["main"]:
        raise ContractError("baseline push trigger must remain restricted to main")

    jobs = _mapping(baseline.get("jobs"), "baseline jobs")
    for context in REQUIRED_CONTEXTS:
        job = _mapping(jobs.get(context), f"required job {context}")
        if "if" in job:
            raise ContractError(f"required job {context} must not have a job-level if")
        if not _sequence(job.get("steps"), f"required job {context} steps"):
            raise ContractError(f"required job {context} must execute real steps")

    tier = _mapping(jobs.get("tier-complete"), "tier-complete job")
    tier_name = str(tier.get("name", ""))
    if "pr-fast" not in tier_name or "main-full" not in tier_name:
        raise ContractError("tier-complete must emit pr-fast or main-full by event")
    _validate_aggregate(tier, set(REQUIRED_CONTEXTS), "tier-complete")

    baseline_runs = "\n".join(_run_text(_mapping(job, "baseline job")) for job in jobs.values())
    for required in (
        "tools/run_test_suite.py workflow-contracts",
        "tools/run_test_suite.py \"$SUITE\"",
        "tests/v23_e2e_test.py",
    ):
        if required not in baseline_runs:
            raise ContractError(f"baseline does not execute required command: {required}")

    _require_triggers(safety, {"schedule", "workflow_dispatch"}, "safety")
    safety_jobs = _mapping(safety.get("jobs"), "safety jobs")
    expected_safety_jobs = {
        "bind-default-branch",
        "macos-apfs-alias",
        "four-path-installer-parity",
        "fault-injection",
        "safety-complete",
    }
    if not expected_safety_jobs.issubset(safety_jobs):
        raise ContractError("safety workflow is missing a required evidence job")
    for job_id in expected_safety_jobs.difference({"safety-complete"}):
        job = _mapping(safety_jobs[job_id], f"safety job {job_id}")
        if "if" in job:
            raise ContractError(f"safety job {job_id} must not have a job-level if")
        if not _sequence(job.get("steps"), f"safety job {job_id} steps"):
            raise ContractError(f"safety job {job_id} must execute real steps")
    final_safety = _mapping(safety_jobs["safety-complete"], "safety-complete")
    if final_safety.get("name") != "safety":
        raise ContractError("safety-complete must emit the safety check context")
    _validate_aggregate(
        final_safety,
        {
            "bind-default-branch",
            "macos-apfs-alias",
            "four-path-installer-parity",
            "fault-injection",
        },
        "safety-complete",
    )

    _require_triggers(release, {"repository_dispatch"}, "release")
    release_text = (workflows / "release.yml").read_text(encoding="utf-8")
    for required in (
        "actions/workflows/baseline.yml/runs",
        "actions/workflows/safety.yml/runs",
        "SAFETY_MAX_AGE_HOURS: 168",
        'head_sha="$CANDIDATE_SHA"',
    ):
        if required not in release_text:
            raise ContractError(f"release evidence gate is missing: {required}")

    development = (root / "docs" / "development.md").read_text(encoding="utf-8")
    if "python tools/run_test_suite.py full" not in development:
        raise ContractError("development documentation must use the canonical full suite")

    state_path = root / "docs" / "ruleset-migration-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load ruleset migration state: {exc}") from exc
    if state.get("live_write_authorized") is not False:
        raise ContractError("ruleset migration artifact must not authorize a live write")
    rulesets = {entry.get("id"): entry for entry in state.get("rulesets", [])}
    if set(rulesets) != {21659417, 21659622}:
        raise ContractError("ruleset migration artifact must contain both exact live IDs")

    def contexts(entry: dict[str, Any], side: str) -> list[str]:
        rules = entry[side]["rules"]
        status_rule = next(rule for rule in rules if rule["type"] == "required_status_checks")
        return [item["context"] for item in status_rule["parameters"]["required_status_checks"]]

    for entry in rulesets.values():
        if entry["observed"].get("name") != entry.get("name"):
            raise ContractError("observed ruleset name must match the exact live name")
        if entry["proposed"].get("name") != entry.get("name"):
            raise ContractError("proposed ruleset name must preserve the live name")
        if set(contexts(entry, "observed")) != set(REQUIRED_CONTEXTS):
            raise ContractError("observed ruleset contexts must remain the exact six")
    if contexts(rulesets[21659417], "proposed") != ["pr-fast"]:
        raise ContractError("proposed branch ruleset must require only pr-fast")
    if set(contexts(rulesets[21659622], "proposed")) != {"main-full", "safety"}:
        raise ContractError("proposed tag ruleset must require main-full and safety")


def main() -> int:
    try:
        validate_contracts()
    except ContractError as exc:
        print(f"workflow contract failed: {exc}", file=sys.stderr)
        return 1
    print("workflow contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
