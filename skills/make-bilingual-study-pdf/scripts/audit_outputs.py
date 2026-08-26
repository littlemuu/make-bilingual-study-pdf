#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from common import read_json, read_jsonl, sha256_file, write_json
from profile import canonical_profile_sha256, load_work_profile
from build_outputs import (
    load_semantic_model,
    markdown_escape,
    response_marker,
    source_only_markdown_body,
)


MARKER_RE = re.compile(
    r"<!-- bilingual:(?P<mode>[a-z-]+) id=(?P<id>[A-Za-z0-9][A-Za-z0-9._:-]*) "
    r"source_sha256=(?P<hash>[0-9a-f]{64}) -->"
)

GENERIC_OUTPUT_DISPOSITIONS = frozenset(
    {"bilingual", "source-only", "visual-once", "artifact-omitted"}
)
LEGACY_OUTPUT_DISPOSITIONS = frozenset(
    {
        "artifact_omitted",
        "preserved_inside_visual",
        "bilingual_grouped",
        "grouped_with_caption",
        "image_visual",
        "math_visual",
        "bilingual_math_visual",
        "source_code_once",
        "bilingual",
    }
)


def semantic_constraint_failures(
    contract: dict,
    blocks: list[dict],
    node_semantics: dict[str, dict],
) -> tuple[list[str], dict[str, bool]]:
    failures: list[str] = []
    checks: dict[str, bool] = {}
    ordered_roles = [node_semantics[block["id"]].get("role") for block in blocks]

    for constraint in contract.get("constraints", []):
        valid = True
        if constraint == "academic-paper-order-v1":
            positions: dict[str, list[int]] = {}
            for index, role in enumerate(ordered_roles):
                positions.setdefault(role, []).append(index)
            title = positions.get("title", [])
            abstract = positions.get("abstract", [])
            sections = positions.get("section", [])
            references = positions.get("references", [])
            valid = bool(title and abstract and sections and references)
            if valid:
                valid = title[0] < abstract[0] < sections[0] < references[0]
                valid = valid and all(
                    title[0] < position < abstract[0]
                    for position in positions.get("author-affiliation", [])
                )
        elif constraint == "heading-hierarchy-v1":
            seen_section = False
            for role in ordered_roles:
                if role == "section":
                    seen_section = True
                elif role == "subsection" and not seen_section:
                    valid = False
                    break
        elif constraint == "visual-relations-v1":
            valid_parent_roles = {
                "figure",
                "chart",
                "table",
                "table-visual",
                "code",
                "code-algorithm",
            }
            for block in blocks:
                semantic = node_semantics[block["id"]]
                role = semantic.get("role")
                if role not in {
                    "figure-caption",
                    "table-caption",
                    "table-footnote",
                    "code-caption",
                    "code-footnote",
                }:
                    continue
                parent = semantic.get("relations", {}).get("caption_parent")
                if (
                    parent not in node_semantics
                    or node_semantics[parent].get("role") not in valid_parent_roles
                ):
                    valid = False
                    break
        elif constraint == "lecture-proof-order-v1":
            theorem_family = {"theorem", "lemma", "proposition", "corollary"}
            seen_theorem = False
            for role in ordered_roles:
                if role == "section":
                    seen_theorem = False
                elif role in theorem_family:
                    seen_theorem = True
                elif role == "proof" and not seen_theorem:
                    valid = False
                    break
        else:
            valid = False
        checks[constraint] = valid
        if not valid:
            failures.append(f"semantic constraint failed: {constraint}")
    return failures, checks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify deterministic output hashes, block accounting, markers, assets, and links."
    )
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    output_dir = work_dir / "output"
    build_manifest_path = output_dir / "build-manifest.json"
    if not build_manifest_path.is_file():
        raise SystemExit(f"missing build manifest: {build_manifest_path}")
    manifest = read_json(work_dir / "manifest.json")
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    build = read_json(build_manifest_path)
    failures: list[str] = []
    warnings: list[str] = []

    inputs = {
        work_dir / "manifest.json": build["source_manifest_sha256"],
        work_dir / "blocks.jsonl": build["source_blocks_sha256"],
        work_dir / "source-audit.json": build["source_audit_sha256"],
        work_dir / "translation" / "translation-audit.json": build[
            "translation_audit_sha256"
        ],
        work_dir / "translation" / "translations-merged.jsonl": build[
            "translations_merged_sha256"
        ],
    }
    if build.get("profile_file_sha256"):
        inputs[work_dir / "profile.json"] = build["profile_file_sha256"]
    if build.get("document_ir_sha256"):
        inputs[work_dir / "document-ir.json"] = build["document_ir_sha256"]
    for path, expected in inputs.items():
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(f"build input changed: {path.relative_to(work_dir)}")
    profile = None
    try:
        profile = load_work_profile(work_dir)
        if build.get("profile_sha256") != canonical_profile_sha256(profile):
            failures.append("build input changed: canonical profile")
    except ValueError as exc:
        failures.append(f"invalid profile binding: {exc}")

    markdown_path = output_dir / build["markdown"]
    latex_path = output_dir / build["latex"]
    for path, expected in (
        (markdown_path, build["markdown_sha256"]),
        (latex_path, build["latex_sha256"]),
    ):
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(f"generated output changed: {path.name}")

    for asset in build.get("assets", []):
        path = output_dir / asset["path"]
        if not path.is_file() or sha256_file(path) != asset["sha256"]:
            failures.append(f"generated asset missing or changed: {asset['path']}")

    semantic_contract = None
    current_node_semantics: dict[str, dict] = {}
    current_role_inventory: dict[str, dict] = {}
    generic_semantics = False
    if profile is not None:
        try:
            (
                semantic_contract,
                _ir_nodes,
                current_node_semantics,
                current_groups_by_node,
                current_role_inventory,
            ) = load_semantic_model(work_dir, blocks, profile)
            generic_semantics = semantic_contract["source_schema_version"] == 2
            expected_manifest_semantics = {
                block["id"]: {
                    "role": current_node_semantics[block["id"]]["role"],
                    "style": current_node_semantics[block["id"]]["style"],
                    "output": current_node_semantics[block["id"]]["output"],
                    "group_ids": sorted(
                        current_groups_by_node.get(block["id"], set())
                    ),
                    "relations": current_node_semantics[block["id"]]["relations"],
                }
                for block in blocks
            }
            if build.get("node_semantics") != expected_manifest_semantics:
                failures.append("build node semantics are missing, unknown, or stale")
            if build.get("role_inventory") != current_role_inventory:
                failures.append("build role inventory is missing or stale")
            expected_semantic_dispositions = {
                block["id"]: current_node_semantics[block["id"]]["output"]
                for block in blocks
            }
            if build.get("semantic_dispositions") != expected_semantic_dispositions:
                failures.append("build semantic dispositions are missing or stale")
            if dict(Counter(expected_semantic_dispositions.values())) != build.get(
                "semantic_disposition_counts"
            ):
                failures.append("build semantic disposition counts are stale")
        except (KeyError, TypeError, ValueError) as exc:
            failures.append(f"invalid semantic build inputs: {exc}")

    block_by_id = {block["id"]: block for block in blocks}
    if len(block_by_id) != len(blocks):
        failures.append("source blocks contain duplicate IDs")
    dispositions = build.get("dispositions", {})
    if not isinstance(dispositions, dict):
        dispositions = {}
        failures.append("build dispositions must be an object")
    if set(dispositions) != set(block_by_id):
        failures.append("build disposition IDs do not exactly cover source block IDs")
    allowed_dispositions = (
        GENERIC_OUTPUT_DISPOSITIONS if generic_semantics else LEGACY_OUTPUT_DISPOSITIONS
    )
    unknown_dispositions = sorted(set(dispositions.values()) - allowed_dispositions)
    if unknown_dispositions:
        failures.append(f"unknown output dispositions: {unknown_dispositions}")
    if generic_semantics and current_node_semantics:
        mismatched_dispositions = sorted(
            block_id
            for block_id, value in dispositions.items()
            if block_id in current_node_semantics
            and value != current_node_semantics[block_id]["output"]
        )
        if mismatched_dispositions:
            failures.append(
                "output dispositions disagree with semantic policies: "
                f"{mismatched_dispositions}"
            )
    expected_counts = Counter(dispositions.values())
    if dict(expected_counts) != build.get("disposition_counts"):
        failures.append("build disposition counts are stale")

    marker_results = []
    if markdown_path.is_file():
        markdown = markdown_path.read_text(encoding="utf-8")
        markers = list(MARKER_RE.finditer(markdown))
        marker_counts = Counter(match.group("id") for match in markers)
        marker_by_id = {match.group("id"): match for match in markers}
        if generic_semantics:
            expected_marker_ids = {
                block_id
                for block_id, disposition in dispositions.items()
                if disposition != "artifact-omitted"
            }
        else:
            expected_marker_ids = {
                block_id
                for block_id, disposition in dispositions.items()
                if disposition
                in {
                    "bilingual",
                    "bilingual_grouped",
                    "bilingual_math_visual",
                    "grouped_with_caption",
                    "source_code_once",
                    "image_visual",
                    "math_visual",
                }
            }
        missing_markers = sorted(expected_marker_ids - set(marker_counts))
        duplicate_markers = sorted(
            block_id for block_id, count in marker_counts.items() if count != 1
        )
        wrong_hashes = sorted(
            match.group("id")
            for match in markers
            if match.group("id") in block_by_id
            and match.group("hash") != block_by_id[match.group("id")]["source_sha256"]
        )
        unknown_markers = sorted(set(marker_counts) - set(block_by_id))
        if missing_markers:
            failures.append(f"missing Markdown block markers: {missing_markers}")
        if duplicate_markers:
            failures.append(f"duplicate Markdown block markers: {duplicate_markers}")
        if wrong_hashes:
            failures.append(f"wrong source hashes in markers: {wrong_hashes}")
        if unknown_markers:
            failures.append(f"unknown Markdown block markers: {unknown_markers}")
        if generic_semantics:
            expected_modes = {
                "bilingual": {"segment", "grouped"},
                "source-only": {"source-only"},
                "visual-once": {"visual"},
                "artifact-omitted": set(),
            }
            wrong_modes = sorted(
                block_id
                for block_id, disposition in dispositions.items()
                if block_id in marker_by_id
                and marker_by_id[block_id].group("mode")
                not in expected_modes.get(disposition, set())
            )
            artifact_markers = sorted(
                block_id
                for block_id, disposition in dispositions.items()
                if disposition == "artifact-omitted" and block_id in marker_by_id
            )
            if wrong_modes:
                failures.append(f"Markdown markers have wrong modes: {wrong_modes}")
            if artifact_markers:
                failures.append(
                    f"artifact-omitted nodes unexpectedly have markers: {artifact_markers}"
                )

            translations_path = (
                work_dir / "translation" / "translations-merged.jsonl"
            )
            translation_entries = (
                read_jsonl(translations_path) if translations_path.is_file() else []
            )
            translations = {
                item.get("id"): item.get("translation") for item in translation_entries
            }
            if len(translations) != len(translation_entries):
                failures.append("merged translations contain duplicate IDs")
            assets_by_id = {
                item.get("id"): item for item in build.get("assets", [])
            }
            visuals_by_anchor: dict[str, list[dict]] = {}
            for visual in manifest.get("visuals", []):
                visuals_by_anchor.setdefault(visual.get("anchor_id"), []).append(visual)
            content_failures: list[str] = []
            for block_id, disposition in dispositions.items():
                block = block_by_id.get(block_id)
                marker = marker_by_id.get(block_id)
                if block is None or disposition == "artifact-omitted" or marker is None:
                    continue
                marker_end = marker.end()
                if disposition == "source-only":
                    try:
                        rendered_source = source_only_markdown_body(block)
                    except ValueError as exc:
                        content_failures.append(f"{block_id}:unsafe-source:{exc}")
                        continue
                elif block["kind"] == "code":
                    rendered_source = block["source"]
                else:
                    rendered_source = markdown_escape(block["source"])
                source_position = markdown.find(rendered_source, marker_end)
                if disposition == "bilingual":
                    translation = translations.get(block_id)
                    if not isinstance(translation, str):
                        content_failures.append(f"{block_id}:missing-translation")
                        continue
                    target_position = markdown.find(
                        markdown_escape(translation), max(marker_end, source_position)
                    )
                    if source_position < marker_end or target_position <= source_position:
                        content_failures.append(f"{block_id}:source-target-order")
                elif disposition == "source-only":
                    if source_position < marker_end or markdown.count(rendered_source) != 1:
                        content_failures.append(f"{block_id}:source-only-count")
                    if block_id in translations:
                        content_failures.append(f"{block_id}:unexpected-translation")
                elif disposition == "visual-once":
                    visuals = visuals_by_anchor.get(block_id, [])
                    if len(visuals) != 1:
                        content_failures.append(f"{block_id}:visual-ownership")
                        continue
                    asset = assets_by_id.get(visuals[0].get("id"))
                    relative = asset.get("path") if isinstance(asset, dict) else None
                    if not relative or markdown.count(f"]({relative})") != 1:
                        content_failures.append(f"{block_id}:visual-count")
                    if block_id in translations:
                        content_failures.append(f"{block_id}:unexpected-translation")
            if content_failures:
                failures.append(
                    f"semantic output content checks failed: {sorted(content_failures)}"
                )
        missing_uris = sorted(
            uri for uri in manifest.get("external_uris", []) if uri not in markdown
        )
        if missing_uris:
            failures.append(f"external URIs missing from Markdown: {missing_uris}")
        marker_results = [
            {"id": block_id, "count": count}
            for block_id, count in sorted(marker_counts.items())
        ]

    constraint_checks: dict[str, bool] = {}
    if semantic_contract is not None and current_role_inventory:
        actual_node_counts = Counter(
            item.get("role")
            for item in current_node_semantics.values()
            if item.get("role") is not None
        )
        for role, inventory in current_role_inventory.items():
            count = inventory.get("occurrence_count")
            minimum = inventory.get("minimum")
            maximum = inventory.get("maximum")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < minimum
                or (maximum is not None and count > maximum)
            ):
                failures.append(
                    f"semantic role {role} count {count} is outside [{minimum}, {maximum}]"
                )
            if inventory.get("node_count") != actual_node_counts.get(role, 0):
                failures.append(f"semantic role {role} node_count is stale")
        if generic_semantics:
            constraint_failures, constraint_checks = semantic_constraint_failures(
                semantic_contract, blocks, current_node_semantics
            )
            failures.extend(constraint_failures)

    report = {
        "status": "failed" if failures else "passed",
        "profile": build.get("profile_id") or "legacy-unbound",
        "markdown": build.get("markdown"),
        "latex": build.get("latex"),
        "block_count": len(blocks),
        "disposition_counts": dict(expected_counts),
        "marker_count": len(marker_results),
        "asset_count": len(build.get("assets", [])),
        "external_uri_count": len(manifest.get("external_uris", [])),
        "role_inventory": current_role_inventory,
        "semantic_constraint_checks": constraint_checks,
        "warnings": warnings,
        "failures": failures,
    }
    write_json(output_dir / "output-audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
