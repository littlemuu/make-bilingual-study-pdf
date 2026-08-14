#!/usr/bin/env python3
"""Audit a V2 bilingual DOCX before PDF rendering and visual review."""
from __future__ import annotations

import argparse
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree
from common import read_json, sha256_file
from profile import load_profile, load_work_profile, profile_contract, semantic_group, target_text_pattern


W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
GENERIC_MARKERS = ("V23-CALLOUT-BEGIN", "V23-CALLOUT-END")
STYLE_COLORS = {
    "abstract": "4D7C8A",
    "definition": "2F7D5B",
    "theorem": "365E9D",
    "proof": "667085",
    "example": "708890",
    "note": "4D7C8A",
    "warning": "B54708",
    "tip": "4D7C8A",
    "exercise": "7A5AF8",
}


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


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def occurrence_count(haystack: str, needle: str) -> int:
    needle = normalized_text(needle)
    return normalized_text(haystack).count(needle) if needle else 0


def searchable_sources(node: dict[str, Any]) -> list[str]:
    source = node.get("source", {}).get("text", "")
    candidates = [source]
    if node.get("type") == "table" and "<" in source:
        candidates.append(html.unescape(re.sub(r"<[^>]+>", " ", source)))
    if node.get("type") == "list":
        entries = [
            re.sub(r"^\s*[-*+]\s+", "", line)
            for line in source.splitlines()
            if line.strip()
        ]
        candidates.extend([" ".join(entries), "• " + " • ".join(entries)])
    return [item for item in candidates if item]


def source_occurrence_count(haystack: str, node: dict[str, Any]) -> int:
    return max(
        (occurrence_count(haystack, needle) for needle in searchable_sources(node)),
        default=0,
    )


def load_v2_context(
    work_dir: Path, profile_reference: str | Path | None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path, Path]:
    work_dir = work_dir.expanduser().resolve()
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = work_dir / "output" / "build-manifest.json"
    missing = [str(path) for path in (profile_path, ir_path, build_path) if not path.is_file()]
    if missing:
        raise ValueError(f"schema V2 DOCX audit is missing frozen artifacts: {missing}")
    profile = load_work_profile(work_dir, profile_reference)
    ir = read_json(ir_path)
    build = read_json(build_path)
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 DOCX audit requires document IR schema_version 2")
    if build.get("profile_id") != profile["id"] or ir.get("profile", {}).get("id") != profile["id"]:
        raise ValueError("frozen Profile ids disagree")
    if build.get("profile_file_sha256") != sha256_file(profile_path):
        raise ValueError("build manifest does not bind the frozen Profile file")
    if build.get("document_ir_sha256") != sha256_file(ir_path):
        raise ValueError("build manifest does not bind the frozen document IR")
    if build.get("role_inventory") != ir.get("inventories", {}).get("role_inventory"):
        raise ValueError("build manifest role inventory does not match document IR")
    return profile, ir, build, ir_path, build_path


def write_report(report: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def validate_v2_docx_audit_binding(
    work_dir: Path,
    docx_path: Path,
    audit_path: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], list[str]]:
    """Validate that a passed DOCX audit binds the current frozen V2 inputs.

    The returned expected binding is suitable for copying into downstream reports.
    Callers must treat every returned error as fatal.
    """
    work_dir = work_dir.expanduser().resolve()
    docx_path = docx_path.expanduser().resolve()
    audit_path = (
        audit_path.expanduser().resolve()
        if audit_path is not None
        else work_dir / "output" / "docx-audit.json"
    )
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = work_dir / "output" / "build-manifest.json"
    required = {
        "frozen Profile": profile_path,
        "document IR": ir_path,
        "build manifest": build_path,
        "DOCX": docx_path,
        "DOCX audit": audit_path,
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    if missing:
        return None, {}, [f"missing {label}" for label in missing]

    try:
        profile = read_json(profile_path)
        audit = read_json(audit_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {}, [f"cannot read DOCX audit binding: {exc}"]
    expected = {
        "profile": str(profile.get("id", "")),
        "profile_file_sha256": sha256_file(profile_path),
        "document_ir_sha256": sha256_file(ir_path),
        "build_manifest_sha256": sha256_file(build_path),
        "docx_sha256": sha256_file(docx_path),
    }
    errors: list[str] = []
    if profile.get("schema_version") != 2:
        errors.append("frozen Profile is not schema V2")
    if audit.get("status") != "passed":
        errors.append("DOCX audit status is not passed")
    for field, value in expected.items():
        if audit.get(field) != value:
            errors.append(f"DOCX audit {field} does not match current bytes")
    return audit, expected, errors


def validate_v2_compile_docx_binding(
    work_dir: Path,
    compile_report: dict[str, Any],
    audit_path: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Validate the complete compile-report to DOCX-audit freeze chain."""
    work_dir = work_dir.expanduser().resolve()
    output_dir = work_dir / "output"
    audit_path = (
        audit_path.expanduser().resolve()
        if audit_path is not None
        else output_dir / "docx-audit.json"
    )
    docx_name = compile_report.get("docx")
    if not isinstance(docx_name, str) or not docx_name:
        return {}, ["compile gate does not identify the audited DOCX"]
    docx_path = (output_dir / docx_name).resolve()
    try:
        docx_path.relative_to(output_dir.resolve())
    except ValueError:
        return {}, ["compile gate DOCX path escapes the output directory"]

    _, expected, errors = validate_v2_docx_audit_binding(
        work_dir, docx_path, audit_path
    )
    comparisons = {
        "docx_sha256": "compile gate refers to different DOCX bytes",
        "profile": "compile gate refers to a different frozen Profile",
        "document_ir_sha256": "compile gate refers to a different document IR",
        "build_manifest_sha256": "compile gate refers to a different build manifest",
    }
    for field, message in comparisons.items():
        if compile_report.get(field) != expected.get(field):
            errors.append(message)
    if compile_report.get("docx_audit_bindings") != expected:
        errors.append("compile gate DOCX audit bindings are stale")
    if audit_path.is_file() and compile_report.get("docx_audit_sha256") != sha256_file(
        audit_path
    ):
        errors.append("compile gate refers to different DOCX audit bytes")
    return expected, errors


def audit_v2(args, profile: dict[str, Any]) -> None:
    try:
        profile, ir, build, ir_path, build_path = load_v2_context(
            args.work_dir, args.profile
        )
        expected_flags = parse_expected_roles(args.expected_role)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    contract = profile_contract(profile)
    role_specs = {item["role"]: item for item in contract["roles"]}
    inventory = ir["inventories"]["role_inventory"]
    frozen_role_counts = {
        role: item["occurrence_count"] for role, item in inventory.items()
    }
    unknown = sorted(set(expected_flags) - set(frozen_role_counts))
    if unknown:
        raise SystemExit(f"--expected-role names unknown Profile roles: {unknown}")

    with zipfile.ZipFile(args.docx) as archive:
        document_xml = archive.read("word/document.xml")
        relationships_xml = archive.read("word/_rels/document.xml.rels")
        root = etree.fromstring(document_xml)
        relationships = etree.fromstring(relationships_xml)
        text = "\n".join(root.xpath("//w:t/text()", namespaces=W_NS))
        external_links = sorted(
            item.get("Target") for item in relationships if item.get("TargetMode") == "External"
        )
        images = [
            name for name in archive.namelist()
            if name.startswith("word/media/") and not name.endswith("/")
        ]

    paragraphs: list[dict[str, Any]] = []
    for node in root.xpath("/w:document/w:body/w:p", namespaces=W_NS):
        paragraph_text = "".join(node.xpath(".//w:t/text()", namespaces=W_NS))
        left = node.xpath("string(./w:pPr/w:pBdr/w:left/@w:color)", namespaces=W_NS)
        right = node.xpath("string(./w:pPr/w:pBdr/w:right/@w:color)", namespaces=W_NS)
        top = node.xpath("string(./w:pPr/w:pBdr/w:top/@w:color)", namespaces=W_NS)
        top_size = node.xpath("string(./w:pPr/w:pBdr/w:top/@w:sz)", namespaces=W_NS)
        bottom = node.xpath("string(./w:pPr/w:pBdr/w:bottom/@w:color)", namespaces=W_NS)
        paragraphs.append(
            {
                "node": node,
                "text": paragraph_text,
                "left": left,
                "right": right,
                "top": top,
                "top_size": top_size,
                "bottom": bottom,
                "indent": (
                    node.xpath("string(./w:pPr/w:ind/@w:left)", namespaces=W_NS),
                    node.xpath("string(./w:pPr/w:ind/@w:right)", namespaces=W_NS),
                ),
            }
        )

    expected_callout_colors = {
        STYLE_COLORS[spec["style"]]
        for spec in role_specs.values()
        if spec["grouping"] == "structural-container"
    }
    ranges: list[dict[str, Any]] = []
    index = 0
    while index < len(paragraphs):
        item = paragraphs[index]
        color = item["left"] if item["left"] == item["right"] else ""
        if color not in expected_callout_colors:
            index += 1
            continue
        end = index
        while end + 1 < len(paragraphs):
            candidate = paragraphs[end + 1]
            if candidate["left"] != color or candidate["right"] != color:
                break
            end += 1
            if candidate["bottom"] == color:
                break
        ranges.append(
            {
                "start": index,
                "end": end,
                "color": color,
                "paragraphs": paragraphs[index : end + 1],
                "text": "\n".join(value["text"] for value in paragraphs[index : end + 1]),
            }
        )
        index = end + 1

    nodes = {node["id"]: node for node in ir.get("nodes", [])}
    complete_groups = [
        group for group in ir.get("semantic_groups", [])
        if group.get("membership") == "complete"
        and role_specs[group["role"]]["grouping"] == "structural-container"
    ]
    scoped_anchor_groups = [
        group for group in ir.get("semantic_groups", [])
        if group.get("membership") == "anchor-only"
        and role_specs[group["role"]]["grouping"] == "structural-container"
    ]
    non_structural_anchor_groups = [
        group for group in ir.get("semantic_groups", [])
        if group.get("membership") == "anchor-only"
        and role_specs[group["role"]]["grouping"] != "structural-container"
    ]
    target_re = target_text_pattern(profile)
    complete_container_checks: dict[str, bool] = {}
    scoped_anchor_callout_checks: dict[str, bool] = {}
    complete_range_indexes: set[int] = set()
    scoped_range_indexes: set[int] = set()

    def check_structural_group(group: dict[str, Any], *, scoped: bool) -> tuple[bool, int | None]:
        group_id = group["id"]
        role = group["role"]
        style = role_specs[role]["style"]
        anchor_node = nodes[group["anchor_node_id"]]
        matches = [
            range_index for range_index, item in enumerate(ranges)
            if source_occurrence_count(item["text"], anchor_node) > 0
        ]
        valid = len(matches) == 1
        range_index = matches[0] if valid else None
        if valid:
            item = ranges[range_index]
            members = item["paragraphs"]
            dividers = [
                member_index for member_index, member in enumerate(members)
                if not member["text"].strip()
                and member["top"] == item["color"]
                and member["top_size"] == "8"
            ]
            expected_color = STYLE_COLORS[style]
            valid = (
                item["color"] == expected_color
                and len({member["indent"] for member in members}) == 1
                and len(dividers) == 1
                and 0 < dividers[0] < len(members) - 1
                and members[0]["top"] == expected_color
                and members[-1]["bottom"] == expected_color
            )
            if valid:
                divider = dividers[0]
                before = "\n".join(member["text"] for member in members[:divider])
                after = "\n".join(member["text"] for member in members[divider + 1 :])
                if scoped:
                    source_candidates = {
                        normalized_text(item)
                        for item in searchable_sources(anchor_node)
                        if normalized_text(item)
                    }
                    valid = (
                        group["member_node_ids"] == [group["anchor_node_id"]]
                        and normalized_text(before) in source_candidates
                    )
                else:
                    cursor = -1
                    for member_id in group["member_node_ids"]:
                        member_node = nodes[member_id]
                        positions = [
                            normalized_text(before).find(normalized_text(source), cursor + 1)
                            for source in searchable_sources(member_node)
                            if normalized_text(source)
                        ]
                        valid_positions = [position for position in positions if position >= 0]
                        position = min(valid_positions, default=-1)
                        if position < 0:
                            valid = False
                            break
                        cursor = max(cursor, position)
                valid = valid and bool(target_re.search(after))
        return valid, range_index

    for group in complete_groups:
        valid, range_index = check_structural_group(group, scoped=False)
        complete_container_checks[group["id"]] = valid
        if valid and range_index is not None:
            complete_range_indexes.add(range_index)

    for group in scoped_anchor_groups:
        valid, range_index = check_structural_group(group, scoped=True)
        scoped_anchor_callout_checks[group["id"]] = valid
        if valid and range_index is not None:
            scoped_range_indexes.add(range_index)

    non_structural_anchor_unboxed: dict[str, bool] = {}
    for group in non_structural_anchor_groups:
        anchor_node = nodes[group["anchor_node_id"]]
        non_structural_anchor_unboxed[group["id"]] = not any(
            source_occurrence_count(item["text"], anchor_node) for item in ranges
        )

    occurrence_evidence: dict[str, dict[str, int]] = {}
    for role in frozen_role_counts:
        role_groups = [group for group in ir.get("semantic_groups", []) if group["role"] == role]
        textual = 0
        visual = 0
        for group in role_groups:
            node = nodes[group["anchor_node_id"]]
            if source_occurrence_count(text, node) > 0:
                textual += 1
            elif node["semantic"]["output"] == "visual-once":
                visual += 1
        occurrence_evidence[role] = {"textual": textual, "visual": visual}

    source_only_counts = {
        node_id: source_occurrence_count(text, node)
        for node_id, node in nodes.items()
        if node.get("semantic", {}).get("output") == "source-only"
        and node.get("source", {}).get("text")
    }
    visual_occurrences = sum(
        item["visual"] for item in occurrence_evidence.values()
    )
    expected_links = sorted(build.get("external_uris", []))
    expected_assets = len(build.get("assets", []))
    expected_native_tables = sum(
        node.get("type") == "table"
        and node.get("semantic", {}).get("output") == "source-only"
        and node.get("source", {})
        .get("text", "")
        .lstrip()
        .lower()
        .startswith("<table")
        for node in nodes.values()
    )
    native_table_count = len(root.xpath("//w:tbl", namespaces=W_NS))
    role_assertions = {
        role: frozen_role_counts[role] == expected
        for role, expected in expected_flags.items()
    }
    if args.expected_problems is not None:
        role_assertions["problem"] = frozen_role_counts.get("problem", 0) == args.expected_problems
    if args.expected_examples is not None:
        role_assertions["example"] = frozen_role_counts.get("example", 0) == args.expected_examples
    if args.expected_tips is not None:
        role_assertions["tip"] = frozen_role_counts.get("tip", 0) == args.expected_tips

    checks = {
        "docx_opens": True,
        "frozen_role_inventory_matches": all(role_assertions.values()),
        "every_role_occurrence_is_evidenced": all(
            evidence["textual"] + evidence["visual"] == frozen_role_counts[role]
            for role, evidence in occurrence_evidence.items()
        ),
        "source_only_nodes_appear_once": all(count == 1 for count in source_only_counts.values()),
        "complete_containers_are_structurally_stable": (
            len(complete_range_indexes) == len(complete_groups)
            and all(complete_container_checks.values())
        ),
        "scoped_anchor_callouts_are_structurally_stable": (
            len(scoped_range_indexes) == len(scoped_anchor_groups)
            and all(scoped_anchor_callout_checks.values())
        ),
        "all_structural_callouts_are_accounted_for": (
            len(ranges) == len(complete_groups) + len(scoped_anchor_groups)
            and complete_range_indexes.isdisjoint(scoped_range_indexes)
            and len(complete_range_indexes | scoped_range_indexes) == len(ranges)
        ),
        "non_structural_anchor_groups_are_not_boxed": all(
            non_structural_anchor_unboxed.values()
        ),
        "no_internal_problem_markers": "V2-PROBLEM-CALLOUT" not in text,
        "no_internal_generic_markers": not any(marker in text for marker in GENERIC_MARKERS),
        "chinese_present": bool(target_re.search(text)),
        "external_links_match": external_links == expected_links,
        "visual_occurrences_are_embedded": len(images) >= max(visual_occurrences, expected_assets),
        "minimum_images_met": len(images) >= args.minimum_images,
        "structured_tables_are_native_word_tables": (
            native_table_count == expected_native_tables
        ),
    }
    if args.expected_links is not None:
        checks["external_link_count_matches"] = len(external_links) == args.expected_links
    problem_groups = [group for group in ir.get("semantic_groups", []) if group["role"] == "problem"]
    problem_ids = [group.get("identifier") for group in problem_groups]
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "profile": profile["id"],
        "profile_file_sha256": sha256_file(
            args.work_dir.expanduser().resolve() / "profile.json"
        ),
        "docx": str(args.docx.resolve()),
        "docx_sha256": sha256_file(args.docx.resolve()),
        "document_ir_sha256": sha256_file(ir_path),
        "build_manifest_sha256": sha256_file(build_path),
        "role_counts": frozen_role_counts,
        "role_occurrence_evidence": occurrence_evidence,
        "complete_container_checks": complete_container_checks,
        "scoped_anchor_callout_checks": scoped_anchor_callout_checks,
        "non_structural_anchor_unboxed": non_structural_anchor_unboxed,
        "container_checks": complete_container_checks,
        "anchor_only_unboxed": non_structural_anchor_unboxed,
        "source_only_occurrence_counts": source_only_counts,
        "problem_count": frozen_role_counts.get("problem", 0),
        "problem_ids": problem_ids,
        "problem_range_count": sum(
            group["role"] == "problem"
            for group in complete_groups + scoped_anchor_groups
        ),
        "example_count": frozen_role_counts.get("example", 0),
        "low_resource_tip_count": frozen_role_counts.get("tip", 0),
        "external_link_count": len(external_links),
        "external_links": external_links,
        "image_count": len(images),
        "native_table_count": native_table_count,
        "expected_native_table_count": expected_native_tables,
        "chinese_character_count": len(target_re.findall(text)),
        "checks": checks,
        "failures": [name for name, value in checks.items() if not value],
    }
    write_report(report, args.output)
    if report["status"] != "passed":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--expected-role", action="append", default=[], metavar="ROLE=COUNT")
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument("--expected-examples", type=int)
    parser.add_argument("--expected-tips", type=int)
    parser.add_argument("--expected-links", type=int)
    parser.add_argument("--minimum-images", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.work_dir and (args.work_dir.expanduser().resolve() / "profile.json").is_file():
            profile = load_work_profile(args.work_dir, args.profile)
        else:
            profile = load_profile(args.profile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if profile.get("schema_version") == 2:
        if args.work_dir is None:
            raise SystemExit("schema V2 DOCX audit requires --work-dir")
        audit_v2(args, profile)
        return
    if args.expected_role:
        raise SystemExit("--expected-role is available only for schema V2 Profiles")
    target_re = target_text_pattern(profile)
    problem_re = re.compile(
        semantic_group(profile, "problem")["source_pattern"], re.I | re.M
    )
    example_re = re.compile(
        semantic_group(profile, "example")["source_pattern"], re.I | re.M
    )
    tip_re = re.compile(semantic_group(profile, "tip")["source_pattern"], re.I | re.M)

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

    problem_ids = [match.groupdict().get("identifier") for match in problem_re.finditer(text)]
    example_ids = [match.groupdict().get("identifier") for match in example_re.finditer(text)]
    tips = len(list(tip_re.finditer(text)))
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
        if problem_re.search(range_text):
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
        "chinese_present": bool(target_re.search(text)),
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
        "profile": profile["id"],
        "docx": str(args.docx.resolve()),
        "problem_count": len(problem_ids),
        "problem_ids": problem_ids,
        "problem_range_count": len(problem_ranges),
        "example_count": len(example_ids),
        "low_resource_tip_count": tips,
        "external_link_count": len(external_links),
        "external_links": external_links,
        "image_count": len(images),
        "chinese_character_count": len(target_re.findall(text)),
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
