#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from common import read_json, read_jsonl, sha256_file, write_json


MARKER_RE = re.compile(
    r"<!-- bilingual:(?P<mode>[a-z-]+) id=(?P<id>p\d{3}-b\d{3}) "
    r"source_sha256=(?P<hash>[0-9a-f]{64}) -->"
)


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
    for path, expected in inputs.items():
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(f"build input changed: {path.relative_to(work_dir)}")

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

    block_by_id = {block["id"]: block for block in blocks}
    dispositions = build.get("dispositions", {})
    if set(dispositions) != set(block_by_id):
        failures.append("build disposition IDs do not exactly cover source block IDs")
    expected_counts = Counter(dispositions.values())
    if dict(expected_counts) != build.get("disposition_counts"):
        failures.append("build disposition counts are stale")

    marker_results = []
    if markdown_path.is_file():
        markdown = markdown_path.read_text(encoding="utf-8")
        markers = list(MARKER_RE.finditer(markdown))
        marker_counts = Counter(match.group("id") for match in markers)
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
        missing_uris = sorted(
            uri for uri in manifest.get("external_uris", []) if uri not in markdown
        )
        if missing_uris:
            failures.append(f"external URIs missing from Markdown: {missing_uris}")
        marker_results = [
            {"id": block_id, "count": count}
            for block_id, count in sorted(marker_counts.items())
        ]

    report = {
        "status": "failed" if failures else "passed",
        "markdown": build.get("markdown"),
        "latex": build.get("latex"),
        "block_count": len(blocks),
        "disposition_counts": dict(expected_counts),
        "marker_count": len(marker_results),
        "asset_count": len(build.get("assets", [])),
        "external_uri_count": len(manifest.get("external_uris", [])),
        "warnings": warnings,
        "failures": failures,
    }
    write_json(output_dir / "output-audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
