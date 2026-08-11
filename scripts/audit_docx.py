#!/usr/bin/env python3
"""Audit a V2 bilingual DOCX before PDF rendering and visual review."""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

from lxml import etree


W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
CJK_RE = re.compile(r"[\u3400-\u9fff]")
PROBLEM_RE = re.compile(r"Problem \(([^)]+)\)")
EXAMPLE_RE = re.compile(r"Example \(([^)]+)\)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument("--expected-examples", type=int)
    parser.add_argument("--expected-tips", type=int)
    parser.add_argument("--expected-links", type=int)
    parser.add_argument("--minimum-images", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with zipfile.ZipFile(args.docx) as archive:
        document_xml = archive.read("word/document.xml")
        relationships_xml = archive.read("word/_rels/document.xml.rels")
        root = etree.fromstring(document_xml)
        relationships = etree.fromstring(relationships_xml)
        text = "\n".join(root.xpath("//w:t/text()", namespaces=W_NS))
        external_links = sorted(
            item.get("Target")
            for item in relationships
            if item.get("TargetMode") == "External"
        )
        images = [
            name
            for name in archive.namelist()
            if name.startswith("word/media/") and not name.endswith("/")
        ]

    problem_ids = PROBLEM_RE.findall(text)
    example_ids = EXAMPLE_RE.findall(text)
    tips = text.count("Low-Resource Tip")
    problem_ranges = []
    active = []

    def finish_problem_range() -> None:
        nonlocal active
        if not active:
            return
        range_text = "".join(
            text
            for paragraph in active
            for text in paragraph.xpath(".//w:t/text()", namespaces=W_NS)
        ).lstrip()
        if range_text.startswith("Problem ("):
            problem_ranges.append(active)
        active = []

    for child in root.xpath("/w:document/w:body/*", namespaces=W_NS):
        side_colors = child.xpath(
            "./w:pPr/w:pBdr/w:left/@w:color | ./w:pPr/w:pBdr/w:right/@w:color",
            namespaces=W_NS,
        )
        if child.tag == f"{{{W_NS['w']}}}p" and side_colors.count("D97706") == 2:
            starts_problem = child.xpath(
                "./w:pPr/w:pBdr/w:top[@w:color='D97706' and @w:sz='12']",
                namespaces=W_NS,
            )
            if starts_problem and active:
                finish_problem_range()
            active.append(child)
            if child.xpath(
                "./w:pPr/w:pBdr/w:bottom[@w:color='D97706']", namespaces=W_NS
            ):
                finish_problem_range()
            continue
        finish_problem_range()
    finish_problem_range()

    stable_problem_ranges = []
    for paragraphs in problem_ranges:
        indents = {
            (
                paragraph.xpath("string(./w:pPr/w:ind/@w:left)", namespaces=W_NS),
                paragraph.xpath("string(./w:pPr/w:ind/@w:right)", namespaces=W_NS),
            )
            for paragraph in paragraphs
        }
        numbered = [
            paragraph
            for paragraph in paragraphs
            if paragraph.xpath("./w:pPr/w:numPr", namespaces=W_NS)
        ]
        numbering_origins_are_explicit = all(
            paragraph.xpath(
                "string(./w:pPr/w:ind/@w:firstLine)", namespaces=W_NS
            )
            == "0"
            and not paragraph.xpath("./w:pPr/w:ind/@w:hanging", namespaces=W_NS)
            for paragraph in numbered
        )
        legacy_horizontal_rules = any(
            paragraph.xpath(".//*[local-name()='rect' and @*[local-name()='hr']='t']")
            for paragraph in paragraphs
        )
        separators = [
            paragraph
            for paragraph in paragraphs[1:]
            if paragraph.xpath(
                "./w:pPr/w:pBdr/w:top[@w:color='D97706']", namespaces=W_NS
            )
        ]
        stable_problem_ranges.append(
            len(indents) == 1
            and numbering_origins_are_explicit
            and not legacy_horizontal_rules
            and len(separators) == 1
        )
    checks = {
        "docx_opens": True,
        "problem_ids_are_unique": len(problem_ids) == len(set(problem_ids)),
        "no_internal_problem_markers": "V2-PROBLEM-CALLOUT" not in text,
        "chinese_present": bool(CJK_RE.search(text)),
        "minimum_images_met": len(images) >= args.minimum_images,
        "problem_callout_borders_are_aligned": (
            len(problem_ranges) == len(problem_ids) and all(stable_problem_ranges)
        ),
    }
    if args.expected_problems is not None:
        checks["problem_count_matches"] = len(problem_ids) == args.expected_problems
    if args.expected_examples is not None:
        checks["example_count_matches"] = len(example_ids) == args.expected_examples
    if args.expected_tips is not None:
        checks["tip_count_matches"] = tips == args.expected_tips
    if args.expected_links is not None:
        checks["external_link_count_matches"] = len(external_links) == args.expected_links

    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "docx": str(args.docx.resolve()),
        "problem_count": len(problem_ids),
        "problem_ids": problem_ids,
        "problem_range_count": len(problem_ranges),
        "example_count": len(example_ids),
        "low_resource_tip_count": tips,
        "external_link_count": len(external_links),
        "external_links": external_links,
        "image_count": len(images),
        "chinese_character_count": len(CJK_RE.findall(text)),
        "checks": checks,
        "failures": [name for name, value in checks.items() if not value],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
