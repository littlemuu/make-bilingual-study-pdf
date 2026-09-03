#!/usr/bin/env python3
"""Run the repository's canonical, deduplicated Python test command suites."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SKILL = "skills/make-bilingual-study-pdf"


@dataclass(frozen=True)
class Command:
    command_id: str
    args: tuple[str, ...]
    requires_upstream_validator: bool = False


COMMANDS = {
    command.command_id: command
    for command in (
        Command("pip-check", ("-m", "pip", "check")),
        Command("check-skill-eol", ("tools/check_skill_eol.py",)),
        Command("manifest-check", ("tools/build_release_manifest.py", "--check")),
        Command("installed-release-check", (f"{SKILL}/scripts/release_check.py",)),
        Command("repository-release-check", ("tools/repository_release_check.py",)),
        Command(
            "skill-validator",
            ("tools/validate_skill.py", SKILL, "--upstream-validator", "{upstream_validator}"),
            True,
        ),
        Command(
            "skill-validator-regressions",
            (
                "tests/skill_validator_test.py",
                "--upstream-validator",
                "{upstream_validator}",
            ),
            True,
        ),
        Command("workflow-contract", ("tools/check_workflow_contract.py",)),
        Command("test-suite-contract", ("tests/ci_test_suite_test.py",)),
        Command("job-results-regressions", ("tests/job_results_test.py",)),
        Command("workflow-contract-regressions", ("tests/workflow_contract_test.py",)),
        Command("eol-regressions", ("tests/check_skill_eol_test.py",)),
        Command("installed-release-regressions", ("tests/release_check_test.py",)),
        Command("repository-release-regressions", ("tests/repository_release_check_test.py",)),
        Command("profile-binding-regressions", ("tests/profile_binding_test.py",)),
        Command("safe-artifact-regressions", ("tests/safe_artifacts_test.py",)),
        Command("work-boundary-regressions", ("tests/work_artifact_boundary_test.py",)),
        Command(
            "translation-boundary-regressions",
            ("tests/translation_artifact_boundary_test.py",),
        ),
        Command("output-boundary-regressions", ("tests/output_artifact_boundary_test.py",)),
        Command("docx-boundary-regressions", ("tests/docx_artifact_boundary_test.py",)),
        Command(
            "compile-boundary-regressions",
            ("tests/compile_final_artifact_boundary_test.py",),
        ),
        Command("self-test", (f"{SKILL}/scripts/self_test.py",)),
        Command("profile-regressions", ("tests/v23_profile_test.py",)),
        Command("mineru-regressions", ("tests/v23_mineru_test.py",)),
        Command("ir-source-regressions", ("tests/v23_ir_source_test.py",)),
        Command("output-regressions", ("tests/v23_output_test.py",)),
        Command("docx-regressions", ("tests/v23_docx_test.py",)),
        Command("visual-gate-regressions", ("tests/v23_visual_gate_test.py",)),
        Command(
            "validate-assignment-profile",
            (f"{SKILL}/scripts/pipeline.py", "validate-profile", "assignment-en-zh"),
        ),
        Command(
            "validate-paper-profile",
            (f"{SKILL}/scripts/pipeline.py", "validate-profile", "academic-paper-en-zh"),
        ),
        Command(
            "validate-lecture-profile",
            (f"{SKILL}/scripts/pipeline.py", "validate-profile", "lecture-notes-en-zh"),
        ),
    )
}


REPOSITORY_GATES = (
    "pip-check",
    "check-skill-eol",
    "manifest-check",
    "installed-release-check",
    "repository-release-check",
    "skill-validator",
    "skill-validator-regressions",
    "workflow-contract",
    "test-suite-contract",
    "job-results-regressions",
    "workflow-contract-regressions",
)

CORE_REGRESSIONS = (
    "eol-regressions",
    "installed-release-regressions",
    "repository-release-regressions",
    "profile-binding-regressions",
    "safe-artifact-regressions",
    "work-boundary-regressions",
    "translation-boundary-regressions",
    "output-boundary-regressions",
    "docx-boundary-regressions",
    "compile-boundary-regressions",
    "self-test",
    "profile-regressions",
    "mineru-regressions",
    "ir-source-regressions",
    "output-regressions",
    "docx-regressions",
    "visual-gate-regressions",
    "validate-assignment-profile",
    "validate-paper-profile",
    "validate-lecture-profile",
)

PR_FAST_REGRESSIONS = (
    "eol-regressions",
    "repository-release-regressions",
    "profile-binding-regressions",
    "safe-artifact-regressions",
    "self-test",
    "profile-regressions",
    "mineru-regressions",
    "ir-source-regressions",
    "output-regressions",
    "docx-regressions",
    "visual-gate-regressions",
    "validate-assignment-profile",
    "validate-paper-profile",
    "validate-lecture-profile",
)

SUITES = {
    "workflow-contracts": (
        "workflow-contract",
        "test-suite-contract",
        "job-results-regressions",
        "workflow-contract-regressions",
    ),
    "metadata": REPOSITORY_GATES,
    "pr-fast": REPOSITORY_GATES + PR_FAST_REGRESSIONS,
    "full": REPOSITORY_GATES + CORE_REGRESSIONS,
    "windows-full": (
        "eol-regressions",
        "installed-release-regressions",
        "repository-release-regressions",
        "profile-binding-regressions",
        "safe-artifact-regressions",
        "work-boundary-regressions",
        "translation-boundary-regressions",
        "output-boundary-regressions",
        "docx-boundary-regressions",
        "compile-boundary-regressions",
    ),
    "fault-injection": (
        "eol-regressions",
        "installed-release-regressions",
        "repository-release-regressions",
        "profile-binding-regressions",
        "safe-artifact-regressions",
        "work-boundary-regressions",
        "translation-boundary-regressions",
        "output-boundary-regressions",
        "docx-boundary-regressions",
        "compile-boundary-regressions",
        "visual-gate-regressions",
    ),
}


def _expanded_args(command: Command, upstream_validator: Path | None) -> list[str]:
    if command.requires_upstream_validator and upstream_validator is None:
        raise ValueError(
            f"suite command {command.command_id!r} requires --upstream-validator"
        )
    values = {"upstream_validator": str(upstream_validator) if upstream_validator else ""}
    return [argument.format(**values) for argument in command.args]


def iter_commands(suite: str) -> Iterable[Command]:
    for command_id in SUITES[suite]:
        yield COMMANDS[command_id]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=sorted(SUITES))
    parser.add_argument("--upstream-validator", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-json", action="store_true")
    args = parser.parse_args()

    if args.list_json:
        print(json.dumps(list(SUITES[args.suite]), indent=2))
        return 0

    for command in iter_commands(args.suite):
        try:
            command_args = _expanded_args(command, args.upstream_validator)
        except ValueError as exc:
            parser.error(str(exc))
        display = [sys.executable, *command_args]
        print(f"[{command.command_id}] {subprocess.list2cmdline(display)}", flush=True)
        if not args.dry_run:
            subprocess.run(display, cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
