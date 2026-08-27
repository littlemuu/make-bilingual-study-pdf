#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common import json_loads_strict
from audit_outputs import validate_compile_output_binding
from visual_utils import validate_visual_review_binding
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


def parse_pages(value: str, maximum: int) -> list[int]:
    pages: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            first, last = item.split("-", 1)
            pages.update(range(int(first), int(last) + 1))
        else:
            pages.add(int(item))
    if not pages or min(pages) < 1 or max(pages) > maximum:
        raise ValueError(f"reviewed pages must fall between 1 and {maximum}")
    return sorted(pages)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record a real visual inspection of every compiled-PDF contact sheet."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--status", choices=("passed", "failed"), required=True)
    parser.add_argument(
        "--reviewed-pages",
        required=True,
        help="use 'all' only after inspecting every contact sheet, or a comma/range list",
    )
    parser.add_argument("--spot-check-pages", default="")
    parser.add_argument("--notes", required=True)
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, work_dir, allow_missing=False)
    for directory in (output_dir / "build", output_dir / "pdf-renders", output_dir / "contact"):
        validate_artifact_tree(directory, work_dir, allow_missing=True)
    compile_path = output_dir / "compile-audit.json"
    review_path = output_dir / "visual-review.json"
    qa_path = output_dir / "qa-report.json"
    for path in (compile_path, review_path, qa_path):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    # Once the complete publication surface is known-safe, invalidate all old
    # approvals before trusting any mutable compile metadata.
    remove_artifact_file(review_path, boundary=work_dir)
    remove_artifact_file(qa_path, boundary=work_dir)
    if not os.path.lexists(compile_path):
        raise SystemExit(f"missing compile audit: {compile_path}")
    compile_audit = json_loads_strict(
        read_artifact_text(compile_path, boundary=work_dir)
    )
    if not isinstance(compile_audit, dict):
        raise SystemExit("compile audit must be a JSON object")
    compile_status = compile_audit.get("automated_status")
    if not isinstance(compile_status, str):
        raise SystemExit("compile audit status must be a string")
    if compile_status != "passed":
        raise SystemExit("automated compile QA has not passed; visual approval is blocked")
    if compile_audit.get("failures") != []:
        raise SystemExit("passed compile audit failures must be an empty array")
    try:
        _, binding_errors = validate_compile_output_binding(
            work_dir, compile_audit, output_dir / "output-audit.json"
        )
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        raise SystemExit(f"cannot validate compile output binding: {exc}") from exc
    if binding_errors:
        raise SystemExit(
            "compile output freeze chain is stale: " + "; ".join(binding_errors)
        )
    page_count = int(compile_audit["page_count"])
    if page_count < 1:
        raise SystemExit("compile audit has no output pages")
    if not args.notes.strip():
        raise SystemExit("visual-review notes must record concrete observations")
    if args.reviewed_pages == "all":
        reviewed_pages = list(range(1, page_count + 1))
    else:
        try:
            reviewed_pages = parse_pages(args.reviewed_pages, page_count)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.status == "passed" and len(reviewed_pages) != page_count:
        raise SystemExit("a passing review must cover every output page")
    try:
        spot_checks = (
            parse_pages(args.spot_check_pages, page_count)
            if args.spot_check_pages.strip()
            else []
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    declared_contact_sheets = compile_audit.get("contact_sheets")
    if not isinstance(declared_contact_sheets, list) or not declared_contact_sheets:
        raise SystemExit("compile audit has no contact sheets; visual approval is blocked")
    contact_sheets = []
    covered_pages: list[int] = []
    for item in declared_contact_sheets:
        if not isinstance(item, dict):
            raise SystemExit("compile audit contains an invalid contact-sheet record")
        path = work_relative_artifact_path(
            output_dir, item.get("path"), label="contact-sheet path"
        )
        try:
            path.relative_to(output_dir / "contact")
        except ValueError as exc:
            raise ArtifactSafetyError(
                "contact-sheet paths must stay inside output/contact"
            ) from exc
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        if not os.path.lexists(path) or sha256_artifact(
            path, boundary=work_dir
        ) != item.get("sha256"):
            raise SystemExit(f"contact sheet missing or changed: {item['path']}")
        contact_sheets.append(item["path"])
        first_page = int(item["first_page"])
        last_page = int(item["last_page"])
        if first_page < 1 or last_page < first_page or last_page > page_count:
            raise SystemExit(f"contact sheet has an invalid page range: {item['path']}")
        covered_pages.extend(range(first_page, last_page + 1))
    if covered_pages != list(range(1, page_count + 1)):
        raise SystemExit("contact sheets do not cover every output page exactly once")
    pdf_path = work_relative_artifact_path(
        output_dir, compile_audit.get("pdf"), label="compiled PDF path"
    )
    validate_artifact_file(pdf_path, boundary=work_dir, allow_missing=True)
    if not os.path.lexists(pdf_path) or sha256_artifact(
        pdf_path, boundary=work_dir
    ) != compile_audit.get("pdf_sha256"):
        raise SystemExit("compiled PDF changed after automated QA")

    review = {
        "status": args.status,
        "compile_audit_sha256": sha256_artifact(
            compile_path, boundary=work_dir
        ),
        "pdf": compile_audit["pdf"],
        "pdf_sha256": compile_audit["pdf_sha256"],
        "page_count": page_count,
        "reviewed_pages": reviewed_pages,
        "contact_sheets_inspected": contact_sheets,
        "contact_sheets_sha256": {
            item["path"]: item["sha256"] for item in declared_contact_sheets
        },
        "spot_check_pages": spot_checks,
        "notes": args.notes,
        "failures": [] if args.status == "passed" else ["human visual review failed"],
    }
    if args.status == "passed":
        _, binding_errors = validate_visual_review_binding(
            work_dir, review, compile_audit
        )
        if binding_errors:
            raise SystemExit(
                "visual review binding is stale or invalid: "
                + "; ".join(binding_errors)
            )
    atomic_write_text(
        review_path,
        json.dumps(review, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )
    print(json.dumps(review, ensure_ascii=False, indent=2))
    if args.status == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
