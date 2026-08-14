#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import read_json, sha256_file, write_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the final QA report only after all automated and visual gates pass."
    )
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    output_dir = work_dir / "output"
    gates = {
        "source": work_dir / "source-audit.json",
        "translation": work_dir / "translation" / "translation-audit.json",
        "output": output_dir / "output-audit.json",
        "compile": output_dir / "compile-audit.json",
        "visual": output_dir / "visual-review.json",
    }
    failures: list[str] = []
    reports = {}
    for name, path in gates.items():
        if not path.is_file():
            failures.append(f"missing {name} gate: {path.relative_to(work_dir)}")
            continue
        reports[name] = read_json(path)

    for name in ("source", "translation", "output", "visual"):
        if name in reports and reports[name].get("status") != "passed":
            failures.append(f"{name} gate is {reports[name].get('status')}")
    if "compile" in reports and (
        reports["compile"].get("automated_status")
        or reports["compile"].get("status")
    ) != "passed":
        failures.append("compile automated gate is not passed")
    if "compile" in reports and "visual" in reports:
        if reports["compile"].get("pdf_sha256") != reports["visual"].get("pdf_sha256"):
            failures.append("visual review refers to a different PDF hash")
        if len(reports["visual"].get("reviewed_pages", [])) != reports["compile"].get(
            "page_count"
        ):
            failures.append("visual review does not cover every compiled page")
        contact_sheets = reports["compile"].get("contact_sheets")
        if not isinstance(contact_sheets, list) or not contact_sheets:
            failures.append("compile gate has no contact-sheet evidence")
        else:
            expected_contacts = [item.get("path") for item in contact_sheets]
            if reports["visual"].get("contact_sheets_inspected") != expected_contacts:
                failures.append("visual review did not inspect every compiled contact sheet")
            expected_hashes = {
                item.get("path"): item.get("sha256") for item in contact_sheets
            }
            if reports["visual"].get("contact_sheets_sha256") != expected_hashes:
                failures.append("visual review contact-sheet hashes do not match compile QA")
        if not str(reports["visual"].get("notes", "")).strip():
            failures.append("visual review has no concrete inspection notes")

    deliverables = {}
    if not failures:
        build = read_json(output_dir / "build-manifest.json")
        candidates = [
            ("markdown", build.get("markdown")),
            ("latex", build.get("latex")),
            ("docx", reports["compile"].get("docx")),
            ("pdf", reports["compile"].get("pdf")),
        ]
        for label, relative in candidates:
            if not relative:
                continue
            path = output_dir / relative
            if not path.is_file():
                failures.append(f"missing final {label}: {relative}")
            else:
                deliverables[label] = {
                    "path": f"output/{relative}",
                    "sha256": sha256_file(path),
                }

    report = {
        "status": "failed" if failures else "passed",
        "source_pdf_sha256": (
            reports.get("compile", {}).get("source_pdf_sha256")
            or read_json(work_dir / "manifest.json").get("source_sha256")
        ),
        "gate_statuses": {
            name: (
                report.get("status")
                if name != "compile"
                else report.get("automated_status") or report.get("status")
            )
            for name, report in reports.items()
        },
        "deliverables": deliverables,
        "warnings": reports.get("compile", {}).get("warnings", []),
        "failures": failures,
    }
    write_json(output_dir / "qa-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
