#!/usr/bin/env python3
"""Build the V2 editable DOCX from audited English-first bilingual Markdown."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from docx import Document

from docx_ast import transform
import docx_style
from common import read_json, sha256_file
from html_table import validate_table_html
from profile import load_profile, load_work_profile, profile_contract


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def materialize_html_tables(ast: dict[str, Any], pandoc: str = "pandoc") -> dict[str, Any]:
    """Turn validated raw HTML tables into native Pandoc Table nodes."""

    blocks = ast.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("Pandoc AST blocks must be an array")
    materialized: list[dict[str, Any]] = []
    for block in blocks:
        if not (
            isinstance(block, dict)
            and block.get("t") == "RawBlock"
            and isinstance(block.get("c"), list)
            and len(block["c"]) == 2
            and block["c"][0] == "html"
            and isinstance(block["c"][1], str)
            and block["c"][1].lstrip().lower().startswith("<table")
        ):
            materialized.append(block)
            continue
        table_html = validate_table_html(block["c"][1])
        completed = subprocess.run(
            [pandoc, "--from", "html", "--to", "json"],
            input=table_html,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        parsed = json.loads(completed.stdout)
        parsed_blocks = parsed.get("blocks")
        if not isinstance(parsed_blocks, list) or not parsed_blocks or any(
            not isinstance(item, dict) or item.get("t") != "Table"
            for item in parsed_blocks
        ):
            raise ValueError("Pandoc did not materialize HTML as a native Table")
        materialized.extend(parsed_blocks)
    result = dict(ast)
    result["blocks"] = materialized
    return result


def parse_expected_roles(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        role, separator, count_text = value.partition("=")
        if not separator or not role or not count_text.isdigit():
            raise ValueError("--expected-role must use ROLE=COUNT with a nonnegative integer")
        if role in result:
            raise ValueError(f"duplicate --expected-role: {role}")
        result[role] = int(count_text)
    return result


def load_v2_context(
    work_dir: Path, profile_reference: str | Path | None, source: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    work_dir = work_dir.expanduser().resolve()
    required = {
        "profile": work_dir / "profile.json",
        "ir": work_dir / "document-ir.json",
        "build": work_dir / "output" / "build-manifest.json",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise ValueError(f"schema V2 DOCX build is missing frozen artifacts: {missing}")
    profile = load_work_profile(work_dir, profile_reference)
    ir = read_json(required["ir"])
    build = read_json(required["build"])
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 DOCX build requires document IR schema_version 2")
    if ir.get("profile", {}).get("id") != profile["id"]:
        raise ValueError("document IR Profile id does not match frozen Profile")
    if build.get("profile_id") != profile["id"]:
        raise ValueError("build manifest Profile id does not match frozen Profile")
    checks = {
        "profile_file_sha256": sha256_file(required["profile"]),
        "document_ir_sha256": sha256_file(required["ir"]),
    }
    for field, actual in checks.items():
        if build.get(field) != actual:
            raise ValueError(f"build manifest {field} does not match frozen artifact")
    markdown_name = build.get("markdown")
    if not isinstance(markdown_name, str) or not markdown_name:
        raise ValueError("build manifest markdown path is missing")
    frozen_markdown = (work_dir / "output" / markdown_name).resolve()
    if frozen_markdown != source or not frozen_markdown.is_file():
        raise ValueError("input Markdown is not the frozen build-manifest Markdown")
    if build.get("markdown_sha256") != sha256_file(frozen_markdown):
        raise ValueError("frozen Markdown hash does not match build manifest")
    ir_inventory = ir.get("inventories", {}).get("role_inventory")
    if not isinstance(ir_inventory, dict) or build.get("role_inventory") != ir_inventory:
        raise ValueError("build manifest role inventory does not match document IR")
    build_dir = work_dir / "output" / "docx-build"
    build_dir.mkdir(parents=True, exist_ok=True)
    return profile, ir, build, build_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert audited English-first Simplified-Chinese Markdown to an A4 DOCX. "
            "Every Problem block is regrouped as complete English, a separator, then "
            "complete Chinese before styling."
        )
    )
    parser.add_argument("input_markdown", type=Path)
    parser.add_argument("output_docx", type=Path)
    parser.add_argument("--resource-path", type=Path)
    parser.add_argument("--reference-doc", type=Path)
    parser.add_argument("--profile", help="built-in profile id or path to profile JSON")
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument(
        "--expected-role",
        action="append",
        default=[],
        metavar="ROLE=COUNT",
        help="repeatable schema V2 assertion against the frozen IR role inventory",
    )
    parser.add_argument("--title")
    parser.add_argument("--header-label")
    parser.add_argument("--footer-label")
    parser.add_argument("--latin-font")
    parser.add_argument("--cjk-font")
    parser.add_argument("--code-font")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()

    source = args.input_markdown.resolve()
    output = args.output_docx.resolve()
    try:
        if args.work_dir and (args.work_dir.expanduser().resolve() / "profile.json").is_file():
            requested_profile = load_work_profile(args.work_dir, args.profile)
        else:
            requested_profile = load_profile(args.profile)
        expected_role_flags = parse_expected_roles(args.expected_role)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    profile = requested_profile
    if profile.get("schema_version") == 1 and expected_role_flags:
        raise SystemExit("--expected-role is available only for schema V2 Profiles")
    defaults = profile["render"]["docx"]
    args.title = args.title or defaults["title"]
    args.header_label = args.header_label or defaults["header_label"]
    args.footer_label = args.footer_label or defaults["footer_label"]
    args.latin_font = args.latin_font or defaults["latin_font"]
    args.cjk_font = args.cjk_font or defaults["cjk_font"]
    args.code_font = args.code_font or defaults["code_font"]
    if not source.is_file():
        raise SystemExit(f"input Markdown not found: {source}")
    if shutil.which("pandoc") is None:
        raise SystemExit("pandoc is required to build the DOCX")

    temp_context = None
    ir: dict[str, Any] | None = None
    build_manifest: dict[str, Any] | None = None
    if requested_profile.get("schema_version") == 2:
        if args.work_dir is None:
            raise SystemExit("schema V2 DOCX build requires --work-dir")
        try:
            profile, ir, build_manifest, work = load_v2_context(
                args.work_dir, args.profile, source
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif args.work_dir:
        work = args.work_dir.resolve()
        work.mkdir(parents=True, exist_ok=True)
    else:
        temp_context = tempfile.TemporaryDirectory(prefix="bilingual-docx-")
        work = Path(temp_context.name)
    ast_path = work / "source.json"
    grouped_path = work / "grouped.json"
    raw_docx = work / "raw.docx"

    run(["pandoc", str(source), "--from", "markdown", "--to", "json", "--output", str(ast_path)])
    ast = json.loads(ast_path.read_text(encoding="utf-8"))
    try:
        ast = materialize_html_tables(ast)
        grouped = transform(
            ast,
            profile,
            semantic_groups=(ir or {}).get("semantic_groups") if ir is not None else None,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    problem_count = int(grouped.get("meta", {}).get("v2-problem-group-count", {}).get("c", "0"))
    if profile["schema_version"] == 1 and args.expected_problems is not None and problem_count != args.expected_problems:
        raise SystemExit(
            f"Problem grouping count {problem_count} does not match expected {args.expected_problems}"
        )
    role_counts: dict[str, int] = {}
    complete_role_counts: dict[str, int] = {}
    anchor_only_role_counts: dict[str, int] = {}
    if profile["schema_version"] == 2:
        inventory = ir["inventories"]["role_inventory"]
        role_counts = {role: item["occurrence_count"] for role, item in inventory.items()}
        structural_roles = {
            item["role"]
            for item in profile_contract(profile)["roles"]
            if item["grouping"] == "structural-container"
        }
        complete_role_counts = {
            role: item["membership_counts"].get("complete", 0)
            for role, item in inventory.items()
            if role in structural_roles
        }
        anchor_only_role_counts = {
            role: item["membership_counts"].get("anchor-only", 0)
            for role, item in inventory.items()
            if role in structural_roles
        }
        unknown = sorted(set(expected_role_flags) - set(role_counts))
        if unknown:
            raise SystemExit(f"--expected-role names unknown Profile roles: {unknown}")
        mismatches = {
            role: {"expected": count, "frozen": role_counts[role]}
            for role, count in expected_role_flags.items()
            if role_counts[role] != count
        }
        if mismatches:
            raise SystemExit(f"role count assertions disagree with frozen IR: {mismatches}")
        if args.expected_problems is not None and role_counts.get("problem", 0) != args.expected_problems:
            raise SystemExit(
                "Problem count alias disagrees with frozen IR: "
                f"{args.expected_problems} != {role_counts.get('problem', 0)}"
            )
    grouped_path.write_text(json.dumps(grouped, ensure_ascii=False) + "\n", encoding="utf-8")

    resource_path = (args.resource_path or source.parent).resolve()
    command = [
        "pandoc",
        str(grouped_path),
        "--from",
        "json",
        "--to",
        "docx",
        "--resource-path",
        str(resource_path),
        "--output",
        str(raw_docx),
    ]
    if args.reference_doc:
        command.extend(["--reference-doc", str(args.reference_doc.resolve())])
    run(command)

    docx_style.LATIN_FONT = args.latin_font
    docx_style.CJK_FONT = args.cjk_font
    docx_style.CODE_FONT = args.code_font
    docx_style.configure_profile(profile)
    document = Document(raw_docx)
    style_report = docx_style.apply_styles(
        document,
        document_title=args.title,
        header_label=args.header_label,
        footer_label=args.footer_label,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(output)

    with zipfile.ZipFile(output) as archive:
        document_xml = archive.read("word/document.xml")
    marker_count = document_xml.count(b"V2-PROBLEM-CALLOUT")
    generic_marker_count = document_xml.count(b"V23-CALLOUT")
    if marker_count:
        raise SystemExit(f"internal Problem markers remain in DOCX: {marker_count}")
    if generic_marker_count:
        raise SystemExit(f"internal generic callout markers remain in DOCX: {generic_marker_count}")
    if profile["schema_version"] == 1 and style_report["problem_callouts"] != problem_count:
        raise SystemExit(
            "styled Problem callout count does not match transformed Problem count: "
            f"{style_report['problem_callouts']} != {problem_count}"
        )
    if profile["schema_version"] == 1 and (
        style_report["problem_numbering_origins_explicit"]
        != style_report["problem_numbered_paragraphs"]
    ):
        raise SystemExit(
            "numbered Problem paragraphs do not all have an explicit stable border origin: "
            f"{style_report['problem_numbering_origins_explicit']} != "
            f"{style_report['problem_numbered_paragraphs']}"
        )
    if profile["schema_version"] == 1 and style_report["problem_legacy_horizontal_rules"]:
        raise SystemExit(
            "legacy VML horizontal rules remain in Problem callouts: "
            f"{style_report['problem_legacy_horizontal_rules']}"
        )

    if profile["schema_version"] == 2:
        styled = style_report.get("role_callouts", {})
        styled_complete = style_report.get("complete_role_callouts", {})
        styled_anchor = style_report.get("anchor_only_role_callouts", {})
        complete_mismatches = {
            role: {"expected": expected, "styled": styled_complete.get(role, 0)}
            for role, expected in complete_role_counts.items()
            if styled_complete.get(role, 0) != expected
        }
        anchor_mismatches = {
            role: {"expected": expected, "styled": styled_anchor.get(role, 0)}
            for role, expected in anchor_only_role_counts.items()
            if styled_anchor.get(role, 0) != expected
        }
        total_mismatches = {
            role: {
                "expected": complete_role_counts[role] + anchor_only_role_counts[role],
                "styled": styled.get(role, 0),
            }
            for role in complete_role_counts
            if styled.get(role, 0)
            != complete_role_counts[role] + anchor_only_role_counts[role]
        }
        if complete_mismatches or anchor_mismatches or total_mismatches:
            raise SystemExit(
                "styled structural callout counts changed: "
                f"complete={complete_mismatches}, anchor-only={anchor_mismatches}, "
                f"total={total_mismatches}"
            )

    report = {
        "status": "passed",
        "profile": profile["id"],
        "output": str(output),
        "problem_groups": problem_count,
        **style_report,
        "internal_problem_markers": marker_count,
        "internal_generic_markers": generic_marker_count,
    }
    if profile["schema_version"] == 2:
        report.update(
            {
                "role_counts": role_counts,
                "complete_structural_role_counts": complete_role_counts,
                "anchor_only_structural_role_counts": anchor_only_role_counts,
                "document_ir_sha256": build_manifest["document_ir_sha256"],
                "build_manifest_sha256": sha256_file(args.work_dir.resolve() / "output" / "build-manifest.json"),
            }
        )
    print(json.dumps(report, ensure_ascii=False))
    if temp_context is not None:
        temp_context.cleanup()


if __name__ == "__main__":
    main()
