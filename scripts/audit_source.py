#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from common import (
    ascii_tokens,
    ngrams,
    problem_ids,
    read_json,
    read_jsonl,
    sha256_file,
    sha256_text,
    write_json,
)
from document_ir import validate_ir_against_sources
from profile import canonical_profile_sha256, load_work_profile


def page_overlap(oracle: str, extracted: str) -> tuple[int, int, float]:
    oracle_grams = ngrams(ascii_tokens(oracle), 5)
    extracted_grams = set(ngrams(ascii_tokens(extracted), 5))
    hits = sum(gram in extracted_grams for gram in oracle_grams)
    score = hits / len(oracle_grams) if oracle_grams else 1.0
    return hits, len(oracle_grams), score


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit extracted source blocks against an independent Poppler text oracle."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--minimum-global-coverage", type=float)
    parser.add_argument("--warn-page-below", type=float)
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    oracle_path = work_dir / "oracle.txt"
    for path in (manifest_path, blocks_path, oracle_path):
        if not path.is_file():
            raise SystemExit(f"missing required artifact: {path}")

    manifest = read_json(manifest_path)
    blocks = read_jsonl(blocks_path)
    oracle_text = oracle_path.read_text(encoding="utf-8")
    oracle_pages = oracle_text.split("\f")
    if oracle_pages and not oracle_pages[-1].strip():
        oracle_pages.pop()

    failures: list[str] = []
    warnings: list[str] = []
    profile = None
    ir_role_counts: dict[str, int] = {}
    if manifest.get("profile"):
        try:
            profile = load_work_profile(work_dir)
            expected_binding = {
                "id": profile["id"],
                "sha256": canonical_profile_sha256(profile),
            }
            if manifest.get("profile") != expected_binding:
                failures.append("manifest profile binding is stale or mismatched")
            failures.extend(validate_ir_against_sources(work_dir, profile))
            ir_path = work_dir / "document-ir.json"
            if ir_path.is_file():
                ir_role_counts = read_json(ir_path).get("inventories", {}).get(
                    "semantic_role_counts", {}
                )
        except (ValueError, KeyError, FileNotFoundError) as exc:
            failures.append(f"invalid profile binding: {exc}")
    minimum_global_coverage = (
        args.minimum_global_coverage
        if args.minimum_global_coverage is not None
        else profile.get("qa", {}).get("minimum_global_fivegram_coverage", 0.95)
        if profile
        else 0.95
    )
    warn_page_below = (
        args.warn_page_below
        if args.warn_page_below is not None
        else profile.get("qa", {}).get("warn_page_below", 0.75)
        if profile
        else 0.75
    )
    source_pdf = Path(manifest["source_pdf"])
    if not source_pdf.is_file():
        failures.append(f"source PDF no longer exists: {source_pdf}")
    elif sha256_file(source_pdf) != manifest["source_sha256"]:
        failures.append("source PDF hash changed after extraction")

    if len(oracle_pages) != manifest["page_count"]:
        failures.append(
            f"oracle page count {len(oracle_pages)} != manifest {manifest['page_count']}"
        )

    seen_ids: set[str] = set()
    blocks_by_page: dict[int, list[dict]] = defaultdict(list)
    corrupt_hashes: list[str] = []
    corrupt_protected_spans: list[str] = []
    for block in blocks:
        block_id = block.get("id")
        if not block_id:
            failures.append("block without id")
            continue
        if block_id in seen_ids:
            failures.append(f"duplicate block id: {block_id}")
        seen_ids.add(block_id)
        source = block.get("source", "")
        if sha256_text(source) != block.get("source_sha256"):
            corrupt_hashes.append(block_id)
        previous_end = -1
        for span in block.get("protected_spans", []):
            start = span.get("start")
            end = span.get("end")
            if (
                not isinstance(start, int)
                or not isinstance(end, int)
                or start < previous_end
                or start < 0
                or end <= start
                or end > len(source)
                or source[start:end] != span.get("text")
                or span.get("role") not in {"code", "math"}
            ):
                corrupt_protected_spans.append(block_id)
                break
            previous_end = end
        blocks_by_page[int(block["page"])].append(block)
    if corrupt_hashes:
        failures.append(f"source hashes changed for blocks: {corrupt_hashes}")
    if corrupt_protected_spans:
        failures.append(
            "invalid protected font spans for blocks: "
            f"{sorted(set(corrupt_protected_spans))}"
        )

    total_hits = 0
    total_grams = 0
    page_results = []
    for page_number, oracle in enumerate(oracle_pages, start=1):
        page_blocks = blocks_by_page.get(page_number, [])
        extracted = "\n".join(
            block.get("source", "")
            for block in page_blocks
            if block.get("kind") not in {"artifact", "image"}
        )
        hits, grams, score = page_overlap(oracle, extracted)
        total_hits += hits
        total_grams += grams
        page_results.append(
            {
                "page": page_number,
                "coverage": round(score, 4),
                "oracle_fivegrams": grams,
                "matched_fivegrams": hits,
                "block_count": len(page_blocks),
            }
        )
        if len(ascii_tokens(oracle)) >= 20 and not page_blocks:
            failures.append(f"page {page_number} has oracle text but no extracted blocks")
        if score < warn_page_below:
            warnings.append(
                f"page {page_number} five-gram coverage {score:.3f} requires visual review"
            )

    global_coverage = total_hits / total_grams if total_grams else 1.0
    if global_coverage < minimum_global_coverage:
        failures.append(
            f"global five-gram coverage {global_coverage:.4f} below "
            f"{minimum_global_coverage:.4f}"
        )

    oracle_problem_ids = set(problem_ids(oracle_text))
    extracted_problem_ids = set(
        problem_ids("\n".join(block.get("source", "") for block in blocks))
    )
    missing_problem_ids = sorted(oracle_problem_ids - extracted_problem_ids)
    extra_problem_ids = sorted(extracted_problem_ids - oracle_problem_ids)
    if missing_problem_ids:
        failures.append(f"missing Problem ids: {missing_problem_ids}")
    if extra_problem_ids:
        warnings.append(f"extra Problem ids: {extra_problem_ids}")

    renders = sorted((work_dir / "renders").glob("page-*.png"))
    if len(renders) != manifest["page_count"]:
        failures.append(
            f"rendered page count {len(renders)} != manifest {manifest['page_count']}"
        )
    contact_results = []
    covered_pages: set[int] = set()
    for contact in manifest.get("source_contact_sheets", []):
        path = work_dir / contact.get("path", "")
        if not path.is_file() or sha256_file(path) != contact.get("sha256"):
            failures.append(f"source contact sheet missing or changed: {contact.get('path')}")
        covered_pages.update(range(contact["first_page"], contact["last_page"] + 1))
        contact_results.append(contact.get("path"))
    if covered_pages != set(range(1, manifest["page_count"] + 1)):
        failures.append("source contact sheets do not cover every source page")

    visual_results = []
    seen_visual_ids: set[str] = set()
    block_ids = {block.get("id") for block in blocks}
    for visual in manifest.get("visuals", []):
        visual_id = visual.get("id")
        if not visual_id or visual_id in seen_visual_ids:
            failures.append(f"missing or duplicate visual id: {visual_id!r}")
            continue
        seen_visual_ids.add(visual_id)
        visual_path = work_dir / visual.get("path", "")
        missing_contained = sorted(
            set(visual.get("contained_block_ids", [])) - block_ids
        )
        if not visual_path.is_file():
            failures.append(f"missing visual crop: {visual.get('path')}")
        if visual.get("anchor_id") not in block_ids:
            failures.append(
                f"visual {visual_id} has unknown anchor: {visual.get('anchor_id')}"
            )
        if missing_contained:
            failures.append(
                f"visual {visual_id} has unknown contained blocks: {missing_contained}"
            )
        visual_results.append(
            {
                "id": visual_id,
                "path": visual.get("path"),
                "exists": visual_path.is_file(),
                "contained_blocks": len(visual.get("contained_block_ids", [])),
            }
        )

    links = manifest.get("links", [])
    external_uris = {item.get("uri") for item in links if item.get("uri")}
    if len(external_uris) != manifest.get("external_uri_count", 0):
        failures.append("external URI inventory does not match manifest count")
    linked_ids = {link_id for block in blocks for link_id in block.get("links", [])}
    manifest_link_ids = {item.get("id") for item in links}
    unknown_link_ids = sorted(linked_ids - manifest_link_ids)
    if unknown_link_ids:
        failures.append(f"blocks refer to unknown links: {unknown_link_ids}")

    report = {
        "status": "failed" if failures else "passed",
        "profile": profile["id"] if profile else "legacy-unbound",
        "source_manifest_sha256": sha256_file(manifest_path),
        "source_blocks_sha256": sha256_file(blocks_path),
        "profile_sha256": canonical_profile_sha256(profile) if profile else None,
        "profile_file_sha256": (
            sha256_file(work_dir / "profile.json")
            if (work_dir / "profile.json").is_file()
            else None
        ),
        "document_ir_sha256": (
            sha256_file(work_dir / "document-ir.json")
            if (work_dir / "document-ir.json").is_file()
            else None
        ),
        "semantic_role_counts": ir_role_counts,
        "global_fivegram_coverage": round(global_coverage, 4),
        "minimum_global_coverage": minimum_global_coverage,
        "problem_ids": {
            "oracle": sorted(oracle_problem_ids),
            "extracted": sorted(extracted_problem_ids),
            "missing": missing_problem_ids,
            "extra": extra_problem_ids,
        },
        "external_uri_count": manifest.get("external_uri_count", 0),
        "visuals": visual_results,
        "unresolved_visual_anchors": manifest.get("unresolved_visual_anchors", []),
        "rendered_pages": len(renders),
        "source_contact_sheets": contact_results,
        "page_results": page_results,
        "warnings": warnings,
        "failures": failures,
    }
    report_path = work_dir / "source-audit.json"
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
