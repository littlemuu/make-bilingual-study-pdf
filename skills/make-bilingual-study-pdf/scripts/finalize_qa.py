#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from job_state import evaluate_job
from safe_artifacts import (
    atomic_write_text,
    lexical_absolute_path,
    remove_artifact_file,
    validate_artifact_file,
    validate_artifact_tree,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the final QA report only after all automated and visual gates pass."
    )
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    output_dir = work_dir / "output"
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    validate_artifact_tree(output_dir, work_dir, allow_missing=False)
    for directory in (
        output_dir / "build",
        output_dir / "pdf-renders",
        output_dir / "contact",
    ):
        validate_artifact_tree(directory, work_dir, allow_missing=True)
    qa_path = output_dir / "qa-report.json"
    validate_artifact_file(qa_path, boundary=work_dir, allow_missing=True)
    # Invalidate an older pass before evaluating mutable gate and deliverable bytes.
    remove_artifact_file(qa_path, boundary=work_dir)

    report = evaluate_job(work_dir).final_report
    atomic_write_text(
        qa_path,
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
