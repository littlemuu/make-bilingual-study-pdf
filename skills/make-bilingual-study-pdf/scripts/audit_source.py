#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path

import pymupdf as fitz
from PIL import Image

from adapters.base import AdapterError
from adapters.mineru import (
    LARGE_RASTER_PAGE_AREA_RATIO,
    RASTER_COVERAGE_METHOD,
    page_raster_coverage_ratios,
)
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
from document_ir import load_adapter_source_evidence, validate_ir_against_sources
from profile import (
    canonical_profile_sha256,
    load_work_profile,
    profile_contract,
    validate_unit_interval_number,
)


def page_overlap(oracle: str, extracted: str) -> tuple[int, int, float]:
    oracle_grams = ngrams(ascii_tokens(oracle), 5)
    extracted_grams = set(ngrams(ascii_tokens(extracted), 5))
    hits = sum(gram in extracted_grams for gram in oracle_grams)
    score = hits / len(oracle_grams) if oracle_grams else 1.0
    return hits, len(oracle_grams), score


def _canonical_json_sha256(value: object) -> str:
    return sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _fully_decode_image(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        image_format = image.format
        width, height = image.size
        image.load()
    return width, height, Image.MIME.get(image_format, "application/octet-stream")


def _coverage_threshold_argument(value: str) -> float:
    try:
        return validate_unit_interval_number(float(value), "coverage threshold")
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _safe_adapter_artifact_path(work_dir: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("artifact path must be a nonempty work-relative string")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError("artifact path must be work-relative")
    root = work_dir.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact path escapes the work directory") from exc
    return path


def audit_adapter_source(
    work_dir: Path,
    manifest: dict,
    blocks: list[dict],
    profile: dict | None = None,
) -> dict:
    """Close the MinerU evidence, disposition, node, and frozen-file chains."""
    failures: list[str] = []
    warnings: list[str] = []
    result = {
        "present": False,
        "adapter": None,
        "evidence_sha256": None,
        "input_count": 0,
        "asset_count": 0,
        "item_count": 0,
        "disposition_counts": {},
        "manual_source_review_required": False,
        "manual_review_pages": [],
        "failures": failures,
        "warnings": warnings,
    }
    if manifest.get("adapter") is None:
        return result
    result["present"] = True
    try:
        evidence, frozen = load_adapter_source_evidence(work_dir, manifest)
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
        failures.append(f"invalid adapter freeze chain: {exc}")
        return result
    assert evidence is not None and frozen is not None
    adapter = manifest["adapter"]
    result["adapter"] = adapter.get("id")
    result["evidence_sha256"] = frozen["sha256"]
    result["input_count"] = len(evidence["inputs"])
    result["asset_count"] = len(evidence["assets"])

    if evidence.get("schema_version") != 1:
        failures.append("unsupported adapter evidence schema_version")
    source = evidence.get("source")
    if not isinstance(source, dict):
        failures.append("adapter evidence source must be an object")
    else:
        if source.get("sha256") != manifest.get("source_sha256"):
            failures.append("adapter evidence source hash does not match manifest")
        if source.get("page_count") != manifest.get("page_count"):
            failures.append("adapter evidence page count does not match manifest")
    mineru = evidence.get("mineru")
    if not isinstance(mineru, dict):
        failures.append("adapter evidence mineru metadata must be an object")
    else:
        if mineru.get("backend") != adapter.get("backend"):
            failures.append("adapter backend does not match evidence")
        if mineru.get("version") != adapter.get("version"):
            failures.append("adapter version does not match evidence")
    if manifest.get("input_artifacts") != evidence.get("inputs"):
        failures.append("manifest input_artifacts do not match adapter evidence")

    input_records = evidence["inputs"]
    input_roles: dict[str, list[dict]] = defaultdict(list)
    input_work_paths: set[str] = set()
    for record in input_records:
        input_roles[record["role"]].append(record)
        work_path = record["work_path"]
        if work_path in input_work_paths:
            failures.append(f"duplicate frozen adapter input work_path: {work_path}")
        input_work_paths.add(work_path)
    for required_role in ("origin", "content", "middle"):
        if len(input_roles[required_role]) != 1:
            failures.append(
                f"adapter evidence requires exactly one {required_role} input"
            )
    if input_roles["origin"] and (
        input_roles["origin"][0].get("sha256") != manifest.get("source_sha256")
    ):
        failures.append("frozen origin input hash does not match source manifest")

    audited_raster_ratios: list[float] | None = None
    if len(input_roles["origin"]) == 1:
        origin_path = work_dir / input_roles["origin"][0]["work_path"]
        try:
            with fitz.open(origin_path) as source_document:
                if source_document.needs_pass:
                    raise ValueError("frozen origin PDF is encrypted")
                if source_document.page_count != manifest.get("page_count"):
                    raise ValueError("frozen origin PDF page count changed")
                audited_raster_ratios = page_raster_coverage_ratios(source_document)
        except (AdapterError, OSError, RuntimeError, ValueError) as exc:
            failures.append(f"cannot audit frozen origin raster geometry: {exc}")

    assets_by_id: dict[str, dict] = {}
    asset_work_paths: set[str] = set()
    for asset in evidence["assets"]:
        asset_id = asset["id"]
        if asset_id in assets_by_id:
            failures.append(f"duplicate adapter asset id: {asset_id}")
            continue
        assets_by_id[asset_id] = asset
        work_path = asset["work_path"]
        if work_path in asset_work_paths:
            failures.append(f"duplicate frozen adapter asset work_path: {work_path}")
        asset_work_paths.add(work_path)
        path = work_dir / work_path
        try:
            width, height, mime = _fully_decode_image(path)
        except (OSError, ValueError) as exc:
            failures.append(f"frozen adapter asset cannot be fully decoded: {work_path}: {exc}")
            continue
        if [width, height] != [asset.get("width"), asset.get("height")]:
            failures.append(f"frozen adapter asset dimensions changed: {work_path}")
        if mime != asset.get("mime"):
            failures.append(f"frozen adapter asset MIME changed: {work_path}")

    content = None
    if len(input_roles["content"]) == 1:
        try:
            content = read_json(work_dir / input_roles["content"][0]["work_path"])
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            failures.append(f"frozen content input is invalid: {exc}")
        if not isinstance(content, list):
            failures.append("frozen content input must be a JSON array")
            content = None

    items = evidence.get("items")
    if not isinstance(items, list):
        failures.append("adapter evidence items must be an array")
        items = []
    result["item_count"] = len(items)
    if content is not None and len(items) != len(content):
        failures.append("every content item must have exactly one disposition")

    block_by_id = {
        block.get("id"): block
        for block in blocks
        if isinstance(block.get("id"), str) and block.get("id")
    }
    adapter_block_ids = {
        block_id
        for block_id, block in block_by_id.items()
        if isinstance(block.get("evidence"), dict)
        and block["evidence"].get("adapter") == adapter.get("id")
    }
    manifest_visuals = manifest.get("visuals", [])
    if not isinstance(manifest_visuals, list):
        failures.append("manifest visuals must be an array")
        manifest_visuals = []
    visual_by_id = {
        visual.get("id"): visual
        for visual in manifest_visuals
        if isinstance(visual, dict)
        and isinstance(visual.get("id"), str)
        and visual.get("id")
    }
    seen_pointers: set[str] = set()
    referenced_nodes: set[str] = set()
    referenced_visuals: set[str] = set()
    disposition_counts: defaultdict[str, int] = defaultdict(int)
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            failures.append(f"adapter evidence item {index} must be an object")
            continue
        pointer = item.get("pointer")
        expected_pointer = f"/{index}"
        if pointer != expected_pointer:
            failures.append(
                f"adapter evidence item pointer must be contiguous: expected {expected_pointer!r}"
            )
        if pointer in seen_pointers:
            failures.append(f"duplicate adapter evidence item pointer: {pointer!r}")
        if isinstance(pointer, str):
            seen_pointers.add(pointer)
        if content is not None and index < len(content):
            source_item = content[index]
            if item.get("item_sha256") != _canonical_json_sha256(source_item):
                failures.append(f"content item hash changed at {expected_pointer}")
            if isinstance(source_item, dict):
                for evidence_field, source_field in (
                    ("raw_type", "type"),
                    ("raw_sub_type", "sub_type"),
                    ("page_idx", "page_idx"),
                ):
                    if item.get(evidence_field) != source_item.get(source_field):
                        failures.append(
                            f"content item {source_field} disagrees with evidence at {expected_pointer}"
                        )
        disposition = item.get("disposition")
        if disposition not in {"emitted", "artifact_omitted"}:
            failures.append(f"invalid content item disposition at {expected_pointer}")
        else:
            disposition_counts[disposition] += 1
        node_ids = item.get("node_ids")
        visual_ids = item.get("visual_ids")
        if not isinstance(node_ids, list) or any(
            not isinstance(node_id, str) or not node_id for node_id in node_ids
        ):
            failures.append(f"invalid node_ids disposition at {expected_pointer}")
            node_ids = []
        if not isinstance(visual_ids, list) or any(
            not isinstance(visual_id, str) or not visual_id for visual_id in visual_ids
        ):
            failures.append(f"invalid visual_ids disposition at {expected_pointer}")
            visual_ids = []
        if len(node_ids) != len(set(node_ids)) or len(visual_ids) != len(set(visual_ids)):
            failures.append(f"duplicate disposition target at {expected_pointer}")
        if not node_ids and not visual_ids:
            failures.append(f"content item has no disposition target at {expected_pointer}")
        if disposition == "artifact_omitted" and not (
            isinstance(item.get("reason"), str) and item["reason"].strip()
        ):
            failures.append(f"artifact_omitted item lacks a reason at {expected_pointer}")
        for node_id in node_ids:
            if node_id in referenced_nodes:
                failures.append(f"node is referenced by multiple dispositions: {node_id}")
            referenced_nodes.add(node_id)
            block = block_by_id.get(node_id)
            if block is None:
                failures.append(f"disposition refers to unknown node: {node_id}")
                continue
            block_evidence = block.get("evidence")
            if not isinstance(block_evidence, dict):
                failures.append(f"adapter node lacks evidence: {node_id}")
                continue
            if block_evidence.get("content_item_pointer") != pointer:
                failures.append(f"node reverse disposition pointer disagrees: {node_id}")
            if block_evidence.get("content_item_sha256") != item.get("item_sha256"):
                failures.append(f"node reverse content hash disagrees: {node_id}")
        for visual_id in visual_ids:
            if visual_id in referenced_visuals:
                failures.append(f"visual is referenced by multiple dispositions: {visual_id}")
            referenced_visuals.add(visual_id)
            if visual_id not in visual_by_id:
                failures.append(f"disposition refers to unknown visual: {visual_id}")

    if referenced_nodes != adapter_block_ids:
        missing = sorted(adapter_block_ids - referenced_nodes)
        extra = sorted(referenced_nodes - adapter_block_ids)
        failures.append(
            f"adapter node disposition inventory is not closed (missing={missing}, extra={extra})"
        )
    if referenced_visuals != set(visual_by_id):
        missing = sorted(set(visual_by_id) - referenced_visuals)
        extra = sorted(referenced_visuals - set(visual_by_id))
        failures.append(
            f"adapter visual disposition inventory is not closed (missing={missing}, extra={extra})"
        )
    result["disposition_counts"] = dict(sorted(disposition_counts.items()))

    for visual_id, visual in visual_by_id.items():
        asset_id = visual.get("asset_id")
        asset = assets_by_id.get(asset_id)
        if asset is None:
            failures.append(f"visual {visual_id} refers to unknown adapter asset: {asset_id!r}")
            continue
        if visual.get("sha256") != asset.get("sha256"):
            failures.append(f"visual {visual_id} hash does not match adapter asset")

    manual = evidence.get("manual_source_review_required")
    manual_pages = evidence.get("manual_review_pages")
    if not isinstance(manual, bool):
        failures.append("manual_source_review_required must be boolean")
        manual = False
    if (
        not isinstance(manual_pages, list)
        or any(
            not isinstance(page, int)
            or isinstance(page, bool)
            or not 1 <= page <= manifest.get("page_count", 0)
            for page in manual_pages
        )
        or len(manual_pages) != len(set(manual_pages))
    ):
        failures.append("manual_review_pages must be unique in-range page numbers")
        manual_pages = []
    if manual != bool(manual_pages):
        failures.append("manual source review flag disagrees with manual_review_pages")
    page_statuses = evidence.get("pages")
    allowed_manual_reasons = {
        "native_oracle_empty",
        "native_oracle_not_substantive_english",
        "document_native_ratio_below_threshold",
        "large_raster_without_native_oracle",
        "adapter_text_without_native_oracle",
    }
    raster_detection = evidence.get("raster_detection")
    if raster_detection != {
        "method": RASTER_COVERAGE_METHOD,
        "large_page_area_ratio": LARGE_RASTER_PAGE_AREA_RATIO,
    }:
        failures.append("adapter raster detection contract is invalid")
    minimum_native_characters = None
    if isinstance(profile, dict):
        value = profile.get("input", {}).get("minimum_text_characters_per_page")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            minimum_native_characters = value
    if not isinstance(page_statuses, list) or len(page_statuses) != manifest.get(
        "page_count"
    ):
        failures.append("adapter page evidence does not cover every source page")
    else:
        page_indices = [
            page.get("page_idx") if isinstance(page, dict) else None
            for page in page_statuses
        ]
        if page_indices != list(range(manifest.get("page_count", 0))):
            failures.append("adapter page evidence indices must be contiguous and ordered")
        for page_index, page in enumerate(page_statuses):
            if not isinstance(page, dict):
                failures.append("adapter page evidence entry must be an object")
                continue
            if page.get("status") not in {
                "native_oracle_available",
                "manual_source_review_required",
            }:
                failures.append("adapter page evidence has an invalid status")
            for field in ("page_size", "source_page_size"):
                size = page.get(field)
                if (
                    not isinstance(size, list)
                    or len(size) != 2
                    or any(
                        not isinstance(value, (int, float))
                        or isinstance(value, bool)
                        or value <= 0
                        for value in size
                    )
                ):
                    failures.append(f"adapter page evidence has invalid {field}")
            for field in ("native_text_characters", "adapter_text_characters"):
                count = page.get(field)
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or count < 0
                ):
                    failures.append(f"adapter page evidence has invalid {field}")
            raster_ratio = page.get("raster_image_area_ratio")
            if (
                not isinstance(raster_ratio, (int, float))
                or isinstance(raster_ratio, bool)
                or not math.isfinite(float(raster_ratio))
                or not 0 <= float(raster_ratio) <= 1
            ):
                failures.append(
                    "adapter page evidence has invalid raster_image_area_ratio"
                )
                raster_ratio = None
            elif (
                audited_raster_ratios is not None
                and float(raster_ratio) != audited_raster_ratios[page_index]
            ):
                failures.append(
                    "adapter page raster coverage disagrees with frozen origin PDF"
                )
            reasons = page.get("manual_review_reasons")
            if (
                not isinstance(reasons, list)
                or any(not isinstance(reason, str) for reason in reasons)
                or len(reasons) != len(set(reasons))
                or any(reason not in allowed_manual_reasons for reason in reasons)
            ):
                failures.append("adapter page evidence has invalid manual review reasons")
                reasons = []
            if (
                raster_ratio is not None
                and minimum_native_characters is not None
                and isinstance(page.get("native_text_characters"), int)
                and not isinstance(page.get("native_text_characters"), bool)
            ):
                expected_large_raster_reason = (
                    page["native_text_characters"] < minimum_native_characters
                    and float(raster_ratio) >= LARGE_RASTER_PAGE_AREA_RATIO
                )
                if expected_large_raster_reason != (
                    "large_raster_without_native_oracle" in reasons
                ):
                    failures.append(
                        "large raster page manual-review reason disagrees with "
                        "independent PDF evidence"
                    )
            expected_manual = page.get("status") == "manual_source_review_required"
            if expected_manual != bool(reasons):
                failures.append(
                    "adapter page status disagrees with its manual review reasons"
                )
        flagged = sorted(
            int(page.get("page_idx")) + 1
            for page in page_statuses
            if isinstance(page, dict)
            and page.get("status") == "manual_source_review_required"
            and isinstance(page.get("page_idx"), int)
        )
        if flagged != sorted(manual_pages):
            failures.append("adapter page statuses disagree with manual_review_pages")
    result["manual_source_review_required"] = manual
    result["manual_review_pages"] = sorted(manual_pages)

    comparison_paths = evidence.get("manual_review_page_comparisons")
    review_contacts = evidence.get("manual_review_contact_sheets")
    if not isinstance(comparison_paths, list):
        failures.append("manual_review_page_comparisons must be an array")
        comparison_paths = []
    if not isinstance(review_contacts, list):
        failures.append("manual_review_contact_sheets must be an array")
        review_contacts = []
    if manifest.get("source_review_pages") != comparison_paths:
        failures.append("manifest source_review_pages do not match adapter evidence")
    if manifest.get("source_review_contact_sheets") != review_contacts:
        failures.append(
            "manifest source_review_contact_sheets do not match adapter evidence"
        )
    if manual:
        comparison_page_numbers: list[int] = []
        if len(comparison_paths) != manifest.get("page_count"):
            failures.append("manual review comparisons must cover every source page")
        for relative_path in comparison_paths:
            try:
                path = _safe_adapter_artifact_path(work_dir, relative_path)
            except ValueError as exc:
                failures.append(f"unsafe manual review comparison path: {exc}")
                continue
            match = re.search(r"page-(\d+)\.png$", relative_path, re.I)
            if not match:
                failures.append(
                    f"manual review comparison lacks a stable page number: {relative_path}"
                )
            else:
                comparison_page_numbers.append(int(match.group(1)))
            if not path.is_file():
                failures.append(f"manual review comparison is missing: {relative_path}")
                continue
            try:
                _fully_decode_image(path)
            except (OSError, ValueError) as exc:
                failures.append(
                    f"manual review comparison cannot be fully decoded: {relative_path}: {exc}"
                )
        if sorted(comparison_page_numbers) != list(
            range(1, manifest.get("page_count", 0) + 1)
        ):
            failures.append("manual review comparison page inventory is not exact")

        if not review_contacts:
            failures.append("manual review contact sheets must be nonempty")
        covered_page_counts: defaultdict[int, int] = defaultdict(int)
        seen_contact_paths: set[str] = set()
        for index, contact in enumerate(review_contacts):
            if not isinstance(contact, dict):
                failures.append(f"manual review contact sheet {index} must be an object")
                continue
            relative_path = contact.get("path")
            if relative_path in seen_contact_paths:
                failures.append(f"duplicate manual review contact sheet: {relative_path}")
            if isinstance(relative_path, str):
                seen_contact_paths.add(relative_path)
            try:
                path = _safe_adapter_artifact_path(work_dir, relative_path)
            except ValueError as exc:
                failures.append(f"unsafe manual review contact sheet path: {exc}")
                continue
            if not path.is_file():
                failures.append(f"manual review contact sheet is missing: {relative_path}")
            else:
                if sha256_file(path) != contact.get("sha256"):
                    failures.append(
                        f"manual review contact sheet hash changed: {relative_path}"
                    )
                try:
                    _fully_decode_image(path)
                except (OSError, ValueError) as exc:
                    failures.append(
                        f"manual review contact sheet cannot be fully decoded: {relative_path}: {exc}"
                    )
            first_page = contact.get("first_page")
            last_page = contact.get("last_page")
            if (
                not isinstance(first_page, int)
                or isinstance(first_page, bool)
                or not isinstance(last_page, int)
                or isinstance(last_page, bool)
                or first_page < 1
                or last_page < first_page
                or last_page > manifest.get("page_count", 0)
            ):
                failures.append(
                    f"manual review contact sheet has an invalid page range: {relative_path}"
                )
                continue
            for page_number in range(first_page, last_page + 1):
                covered_page_counts[page_number] += 1
        if covered_page_counts != {
            page_number: 1
            for page_number in range(1, manifest.get("page_count", 0) + 1)
        }:
            failures.append("manual review contact sheets do not exactly cover every page")
    elif comparison_paths or review_contacts:
        failures.append("manual review artifacts must be empty when review is not required")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit extracted source blocks against an independent Poppler text oracle."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--minimum-global-coverage", type=_coverage_threshold_argument)
    parser.add_argument("--warn-page-below", type=_coverage_threshold_argument)
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
    ir_role_inventory: dict[str, dict] = {}
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
                inventories = read_json(ir_path).get("inventories", {})
                ir_role_counts = inventories.get("semantic_role_counts", {})
                ir_role_inventory = inventories.get("role_inventory", {})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            failures.append(f"invalid profile binding: {exc}")
    if profile and profile.get("schema_version") == 2:
        contract = profile_contract(profile)
        for role, policy in contract["role_inventory"].items():
            item = ir_role_inventory.get(role)
            if not isinstance(item, dict):
                failures.append(f"document IR lacks declared role inventory: {role}")
                continue
            count = item.get("occurrence_count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                failures.append(f"document IR has invalid occurrence count for role: {role}")
                continue
            if count < policy["minimum"]:
                failures.append(
                    f"semantic role {role} occurrence count {count} below minimum {policy['minimum']}"
                )
            if policy["maximum"] is not None and count > policy["maximum"]:
                failures.append(
                    f"semantic role {role} occurrence count {count} above maximum {policy['maximum']}"
                )
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

    adapter_audit = audit_adapter_source(work_dir, manifest, blocks, profile)
    failures.extend(adapter_audit["failures"])
    warnings.extend(adapter_audit["warnings"])

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
    visual_entries = manifest.get("visuals", [])
    if not isinstance(visual_entries, list):
        failures.append("manifest visuals must be an array")
        visual_entries = []
    for visual in visual_entries:
        if not isinstance(visual, dict):
            failures.append("manifest visual entry must be an object")
            continue
        visual_id = visual.get("id")
        if not visual_id or visual_id in seen_visual_ids:
            failures.append(f"missing or duplicate visual id: {visual_id!r}")
            continue
        seen_visual_ids.add(visual_id)
        try:
            visual_path = (
                _safe_adapter_artifact_path(work_dir, visual.get("path"))
                if adapter_audit["present"]
                else work_dir / visual.get("path", "")
            )
        except (TypeError, ValueError) as exc:
            failures.append(f"unsafe visual crop path for {visual_id}: {exc}")
            visual_path = None
        missing_contained = sorted(
            set(visual.get("contained_block_ids", [])) - block_ids
        )
        if visual_path is None or not visual_path.is_file():
            failures.append(f"missing visual crop: {visual.get('path')}")
        elif adapter_audit["present"]:
            expected_visual_hash = visual.get("sha256")
            if expected_visual_hash is not None and sha256_file(
                visual_path
            ) != expected_visual_hash:
                failures.append(f"visual crop hash changed: {visual.get('path')}")
            try:
                _fully_decode_image(visual_path)
            except (OSError, ValueError) as exc:
                failures.append(
                    f"visual crop cannot be fully decoded: {visual.get('path')}: {exc}"
                )
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
                "exists": visual_path is not None and visual_path.is_file(),
                "sha256": visual.get("sha256"),
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

    status = (
        "failed"
        if failures
        else "manual_source_review_required"
        if adapter_audit["manual_source_review_required"]
        else "passed"
    )
    report = {
        "status": status,
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
        "adapter_evidence_sha256": adapter_audit["evidence_sha256"],
        "adapter_source": {
            key: value
            for key, value in adapter_audit.items()
            if key not in {"failures", "warnings"}
        },
        "semantic_role_counts": ir_role_counts,
        "semantic_role_inventory": ir_role_inventory,
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
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
