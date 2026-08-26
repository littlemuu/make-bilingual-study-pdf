#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import read_json, sha256_file, write_json


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

    work_dir = args.work_dir.expanduser().resolve()
    output_dir = work_dir / "output"
    compile_path = output_dir / "compile-audit.json"
    if not compile_path.is_file():
        raise SystemExit(f"missing compile audit: {compile_path}")
    compile_audit = read_json(compile_path)
    if compile_audit.get("automated_status") != "passed":
        raise SystemExit("automated compile QA has not passed; visual approval is blocked")
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
        path = output_dir / item["path"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise SystemExit(f"contact sheet missing or changed: {item['path']}")
        contact_sheets.append(item["path"])
        first_page = int(item["first_page"])
        last_page = int(item["last_page"])
        if first_page < 1 or last_page < first_page or last_page > page_count:
            raise SystemExit(f"contact sheet has an invalid page range: {item['path']}")
        covered_pages.extend(range(first_page, last_page + 1))
    if covered_pages != list(range(1, page_count + 1)):
        raise SystemExit("contact sheets do not cover every output page exactly once")
    pdf_path = output_dir / compile_audit["pdf"]
    if not pdf_path.is_file() or sha256_file(pdf_path) != compile_audit["pdf_sha256"]:
        raise SystemExit("compiled PDF changed after automated QA")

    review = {
        "status": args.status,
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
    }
    write_json(output_dir / "visual-review.json", review)
    print(json.dumps(review, ensure_ascii=False, indent=2))
    if args.status == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
