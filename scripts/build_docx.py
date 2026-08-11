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

from docx import Document

from docx_ast import transform
import docx_style


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


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
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument("--title", default="English-Chinese Bilingual Study Edition")
    parser.add_argument("--header-label", default="Bilingual study edition")
    parser.add_argument("--footer-label", default="英中双语学习版")
    parser.add_argument("--latin-font", default=docx_style.LATIN_FONT)
    parser.add_argument("--cjk-font", default=docx_style.CJK_FONT)
    parser.add_argument("--code-font", default=docx_style.CODE_FONT)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()

    source = args.input_markdown.resolve()
    output = args.output_docx.resolve()
    if not source.is_file():
        raise SystemExit(f"input Markdown not found: {source}")
    if shutil.which("pandoc") is None:
        raise SystemExit("pandoc is required to build the DOCX")

    temp_context = None
    if args.work_dir:
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
    grouped = transform(ast)
    problem_count = int(grouped.get("meta", {}).get("v2-problem-group-count", {}).get("c", "0"))
    if args.expected_problems is not None and problem_count != args.expected_problems:
        raise SystemExit(
            f"Problem grouping count {problem_count} does not match expected {args.expected_problems}"
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
    if marker_count:
        raise SystemExit(f"internal Problem markers remain in DOCX: {marker_count}")
    if style_report["problem_callouts"] != problem_count:
        raise SystemExit(
            "styled Problem callout count does not match transformed Problem count: "
            f"{style_report['problem_callouts']} != {problem_count}"
        )
    if (
        style_report["problem_numbering_origins_explicit"]
        != style_report["problem_numbered_paragraphs"]
    ):
        raise SystemExit(
            "numbered Problem paragraphs do not all have an explicit stable border origin: "
            f"{style_report['problem_numbering_origins_explicit']} != "
            f"{style_report['problem_numbered_paragraphs']}"
        )
    if style_report["problem_legacy_horizontal_rules"]:
        raise SystemExit(
            "legacy VML horizontal rules remain in Problem callouts: "
            f"{style_report['problem_legacy_horizontal_rules']}"
        )

    report = {
        "status": "passed",
        "output": str(output),
        "problem_groups": problem_count,
        **style_report,
        "internal_problem_markers": marker_count,
    }
    print(json.dumps(report, ensure_ascii=False))
    if temp_context is not None:
        temp_context.cleanup()


if __name__ == "__main__":
    main()
