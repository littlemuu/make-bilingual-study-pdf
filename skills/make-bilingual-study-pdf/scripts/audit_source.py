#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
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
from safe_artifacts import (
    ArtifactSafetyError,
    lexical_absolute_path,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


def current_source_pdf_binding(manifest: dict[str, object]) -> tuple[Path, str]:
    """Validate and hash the exact current source PDF named by a manifest."""
    value = manifest.get("source_pdf")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("source manifest does not identify a valid source PDF path")
    if not Path(value).expanduser().is_absolute():
        raise ValueError("source manifest PDF path must be absolute")
    source_pdf = lexical_absolute_path(value)
    if source_pdf.suffix.lower() != ".pdf":
        raise ValueError("source manifest path is not a PDF")
    validate_artifact_file(
        source_pdf, boundary=source_pdf.parent, allow_missing=True
    )
    if not os.path.lexists(source_pdf):
        raise ValueError(f"source PDF no longer exists: {source_pdf}")
    current_hash = sha256_artifact(source_pdf, boundary=source_pdf.parent)
    expected_hash = manifest.get("source_sha256")
    if (
        not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
        or current_hash != expected_hash
    ):
        raise ValueError("source PDF hash changed after extraction")
    return source_pdf, current_hash


def current_manifest_visual_bindings(
    work_dir: Path, manifest: dict[str, object]
) -> list[dict[str, object]]:
    """Validate and bind manifest visuals inside the dedicated WORK/visuals tree."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    visual_entries = manifest.get("visuals", [])
    if not isinstance(visual_entries, list):
        raise ValueError("source manifest visuals must be an array")
    if not visual_entries:
        validate_artifact_tree(work_dir / "visuals", work_dir, allow_missing=True)
        return []

    visual_root = work_dir / "visuals"
    validate_artifact_tree(visual_root, work_dir, allow_missing=False)
    visual_bindings: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, entry in enumerate(visual_entries):
        if not isinstance(entry, dict):
            raise ValueError(f"source visual {index + 1} must be an object")
        visual_id = entry.get("id")
        if not isinstance(visual_id, str) or not visual_id:
            raise ValueError(f"source visual {index + 1} has an invalid id")
        id_key = visual_id.casefold()
        if id_key in seen_ids:
            raise ValueError(f"source visual id is duplicated: {visual_id}")
        seen_ids.add(id_key)

        relative = entry.get("path")
        path = work_relative_artifact_path(
            work_dir,
            relative,
            label="source visual path",
        )
        try:
            visual_relative = path.relative_to(visual_root)
        except ValueError as exc:
            raise ValueError("source visuals must stay inside WORK/visuals") from exc
        if not visual_relative.parts:
            raise ValueError("source visual path must name a file below WORK/visuals")
        path_key = visual_relative.as_posix().casefold()
        if path_key in seen_paths:
            raise ValueError(f"source visual path is duplicated: {relative}")
        seen_paths.add(path_key)
        validate_artifact_file(path, boundary=visual_root, allow_missing=True)
        if not os.path.lexists(path):
            raise ValueError(f"source visual is missing: {relative}")
        current_hash = sha256_artifact(path, boundary=visual_root)
        declared_hash = entry.get("sha256")
        if declared_hash is not None and declared_hash != current_hash:
            raise ValueError(f"source visual manifest binding is stale: {relative}")
        try:
            width, height, mime = _fully_decode_image(path)
        except (ArtifactSafetyError, OSError, ValueError) as exc:
            raise ValueError(
                f"source visual cannot be fully decoded: {relative}: {exc}"
            ) from exc
        for field, actual in (("width", width), ("height", height), ("mime", mime)):
            if field in entry and entry.get(field) != actual:
                raise ValueError(
                    f"source visual declared {field} is stale: {relative}"
                )
        contained_block_ids = entry.get("contained_block_ids", [])
        if (
            not isinstance(contained_block_ids, list)
            or any(not isinstance(value, str) or not value for value in contained_block_ids)
            or len(contained_block_ids) != len(set(contained_block_ids))
        ):
            raise ValueError(
                f"source visual {index + 1} contained_block_ids must be a unique string array"
            )
        visual_bindings.append(
            {
                "id": visual_id,
                "path": relative,
                "exists": True,
                "sha256": current_hash,
                "contained_blocks": len(contained_block_ids),
            }
        )
    return visual_bindings


def current_adapter_manual_review_binding(
    work_dir: Path, manifest: dict[str, object]
) -> dict[str, object]:
    """Return the closed manual-review state frozen by current adapter evidence."""
    evidence, _frozen = load_adapter_source_evidence(work_dir, manifest)
    if evidence is None:
        return {
            "manual_source_review_required": False,
            "manual_review_pages": [],
            "adapter_page_statuses": [],
        }

    page_count = manifest.get("page_count")
    manual = evidence.get("manual_source_review_required")
    manual_pages = evidence.get("manual_review_pages")
    pages = evidence.get("pages")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("source manifest page_count is invalid")
    if not isinstance(manual, bool):
        raise ValueError("adapter manual_source_review_required must be boolean")
    if (
        not isinstance(manual_pages, list)
        or any(
            not isinstance(page, int)
            or isinstance(page, bool)
            or not 1 <= page <= page_count
            for page in manual_pages
        )
        or len(manual_pages) != len(set(manual_pages))
    ):
        raise ValueError("adapter manual_review_pages must be unique in-range integers")
    if not isinstance(pages, list) or len(pages) != page_count:
        raise ValueError("adapter page evidence does not cover every source page")

    statuses: list[str] = []
    flagged: list[int] = []
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("page_idx") != page_index:
            raise ValueError("adapter page evidence indices must be contiguous and ordered")
        status = page.get("status")
        if status not in {
            "native_oracle_available",
            "manual_source_review_required",
        }:
            raise ValueError("adapter page evidence has an invalid status")
        statuses.append(status)
        if status == "manual_source_review_required":
            flagged.append(page_index + 1)
    if sorted(manual_pages) != flagged or manual != bool(flagged):
        raise ValueError("adapter manual-review state is internally inconsistent")
    return {
        "manual_source_review_required": manual,
        "manual_review_pages": sorted(manual_pages),
        "adapter_page_statuses": statuses,
    }


def _source_artifact_contract(manifest: dict[str, object]) -> dict[str, object]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("source manifest artifacts must be an object")
    expected = {
        "profile": "profile.json",
        "document_ir": "document-ir.json",
        "blocks": "blocks.jsonl",
        "oracle": "oracle.txt",
        "oracle_layout": "oracle-layout.txt",
        "renders": "renders/page-*.png",
        "source_contact": "source-contact/contact-*.png",
    }
    for field, value in expected.items():
        if artifacts.get(field) != value:
            raise ValueError(
                f"source manifest artifacts.{field} must be canonical {value}"
            )
    return artifacts


def _source_render_inventory(
    work_dir: Path, page_count: int, *, decode: bool
) -> list[Path]:
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("source manifest page_count is invalid")
    renders_dir = work_dir / "renders"
    validate_artifact_tree(renders_dir, work_dir, allow_missing=False)
    by_page: dict[int, Path] = {}
    with os.scandir(renders_dir) as iterator:
        entries = list(iterator)
    for entry in entries:
        match = re.fullmatch(r"page-(\d+)\.png", entry.name)
        if match is None:
            raise ValueError(f"unexpected source render entry: {entry.name}")
        page_number = int(match.group(1))
        if page_number in by_page:
            raise ValueError(
                f"source render page {page_number} has duplicate filenames"
            )
        render_path = renders_dir / entry.name
        validate_artifact_file(render_path, boundary=renders_dir)
        by_page[page_number] = render_path
    expected_pages = set(range(1, page_count + 1))
    if set(by_page) != expected_pages:
        raise ValueError("source render inventory is not the exact ordered page set")
    ordered = [by_page[page_number] for page_number in range(1, page_count + 1)]
    if decode:
        for path in ordered:
            try:
                _fully_decode_image(path)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"source render cannot be fully decoded: {path.name}: {exc}"
                ) from exc
    return ordered


def _source_contact_bindings(
    work_dir: Path, manifest: dict[str, object], *, decode: bool
) -> list[str]:
    page_count = manifest.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("source manifest page_count is invalid")
    entries = manifest.get("source_contact_sheets")
    if not isinstance(entries, list) or not entries:
        raise ValueError("source contact sheets must be a non-empty array")

    contact_root = work_dir / "source-contact"
    validate_artifact_tree(contact_root, work_dir, allow_missing=False)
    seen_paths: set[str] = set()
    covered_pages: list[int] = []
    paths: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"source contact sheet {index + 1} must be an object")
        relative = entry.get("path")
        path = work_relative_artifact_path(
            work_dir, relative, label="source contact-sheet path"
        )
        try:
            contact_relative = path.relative_to(contact_root)
        except ValueError as exc:
            raise ValueError(
                "source contact sheets must stay inside WORK/source-contact"
            ) from exc
        if len(contact_relative.parts) != 1 or re.fullmatch(
            r"contact-(\d+)\.png", contact_relative.name
        ) is None:
            raise ValueError("source contact sheet path is not canonical")
        key = contact_relative.as_posix().casefold()
        if key in seen_paths:
            raise ValueError(f"source contact sheet path is duplicated: {relative}")
        seen_paths.add(key)
        validate_artifact_file(path, boundary=contact_root, allow_missing=True)
        if not os.path.lexists(path):
            raise ValueError(f"source contact sheet is missing: {relative}")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or sha256_artifact(path, boundary=contact_root) != expected_hash
        ):
            raise ValueError(f"source contact sheet binding is stale: {relative}")
        if decode:
            try:
                _fully_decode_image(path)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"source contact sheet cannot be fully decoded: {relative}: {exc}"
                ) from exc
        first_page = entry.get("first_page")
        last_page = entry.get("last_page")
        if (
            not isinstance(first_page, int)
            or isinstance(first_page, bool)
            or not isinstance(last_page, int)
            or isinstance(last_page, bool)
            or not 1 <= first_page <= last_page <= page_count
        ):
            raise ValueError(f"source contact sheet page range is invalid: {relative}")
        covered_pages.extend(range(first_page, last_page + 1))
        paths.append(relative)
    if sorted(covered_pages) != list(range(1, page_count + 1)):
        raise ValueError(
            "source contact sheets must cover every source page exactly once"
        )
    return paths


def _current_source_text_metrics(
    work_dir: Path,
    manifest: dict[str, object],
    blocks: list[dict[str, object]],
) -> dict[str, object]:
    page_count = manifest.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("source manifest page_count is invalid")
    oracle_text = read_artifact_text(
        work_dir / "oracle.txt", boundary=work_dir, encoding="utf-8"
    )
    oracle_pages = oracle_text.split("\f")
    if oracle_pages and not oracle_pages[-1].strip():
        oracle_pages.pop()
    if len(oracle_pages) != page_count:
        raise ValueError("source oracle does not cover exactly the manifest page count")

    blocks_by_page: dict[int, list[dict[str, object]]] = defaultdict(list)
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError(f"source block {index + 1} must be an object")
        page = block.get("page")
        if (
            not isinstance(page, int)
            or isinstance(page, bool)
            or not 1 <= page <= page_count
        ):
            raise ValueError(f"source block {index + 1} has an invalid page")
        source = block.get("source")
        if not isinstance(source, str):
            raise ValueError(f"source block {index + 1} has invalid source text")
        blocks_by_page[page].append(block)

    total_hits = 0
    total_grams = 0
    page_results: list[dict[str, object]] = []
    for page_number, oracle in enumerate(oracle_pages, 1):
        page_blocks = blocks_by_page.get(page_number, [])
        extracted = "\n".join(
            str(block["source"])
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
    oracle_problem_ids = sorted(set(problem_ids(oracle_text)))
    extracted_problem_ids = sorted(
        set(problem_ids("\n".join(str(block["source"]) for block in blocks)))
    )
    return {
        "page_count": page_count,
        "page_results": page_results,
        "rendered_pages": page_count,
        "global_fivegram_coverage": round(
            total_hits / total_grams if total_grams else 1.0, 4
        ),
        "problem_ids": {
            "oracle": oracle_problem_ids,
            "extracted": extracted_problem_ids,
            "missing": sorted(set(oracle_problem_ids) - set(extracted_problem_ids)),
            "extra": sorted(set(extracted_problem_ids) - set(oracle_problem_ids)),
        },
    }


def current_source_audit_bindings(work_dir: Path) -> dict[str, object]:
    """Return the exact current inputs and visual evidence a passed audit certifies."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    oracle_path = work_dir / "oracle.txt"
    oracle_layout_path = work_dir / "oracle-layout.txt"
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    for label, path in (
        ("source manifest", manifest_path),
        ("source blocks", blocks_path),
        ("source text oracle", oracle_path),
        ("source layout oracle", oracle_layout_path),
        ("frozen Profile file", profile_path),
        ("document IR", ir_path),
    ):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        if not os.path.lexists(path):
            raise ValueError(f"{label} is missing")

    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("source manifest must be a JSON object")
    _source_artifact_contract(manifest)
    profile = load_work_profile(work_dir)
    ir_failures = validate_ir_against_sources(work_dir, profile)
    if ir_failures:
        raise ValueError(
            "current document IR does not pass source validation: "
            + "; ".join(ir_failures)
        )
    _source_pdf, source_pdf_sha256 = current_source_pdf_binding(manifest)

    adapter = manifest.get("adapter")
    evidence_reference = adapter.get("evidence") if isinstance(adapter, dict) else None
    evidence_path = (
        work_relative_artifact_path(
            work_dir,
            evidence_reference,
            label="adapter evidence path",
        )
        if evidence_reference is not None
        else work_dir / "adapter-evidence.json"
    )
    validate_artifact_file(evidence_path, boundary=work_dir, allow_missing=True)
    evidence_exists = os.path.lexists(evidence_path)
    if evidence_reference is not None and not evidence_exists:
        raise ValueError("declared adapter evidence is missing")
    evidence_hash = (
        sha256_artifact(evidence_path, boundary=work_dir)
        if evidence_exists
        else None
    )

    page_count = manifest.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ValueError("source manifest page_count is invalid")
    contact_paths = _source_contact_bindings(work_dir, manifest, decode=True)
    render_paths = _source_render_inventory(work_dir, page_count, decode=True)
    renders_dir = work_dir / "renders"
    render_bindings: list[dict[str, str]] = []
    for render_path in render_paths:
        validate_artifact_file(render_path, boundary=renders_dir)
        render_bindings.append(
            {
                "path": render_path.relative_to(work_dir).as_posix(),
                "sha256": sha256_artifact(render_path, boundary=renders_dir),
            }
        )

    visual_bindings = current_manifest_visual_bindings(work_dir, manifest)
    blocks = read_jsonl(blocks_path)
    text_metrics = _current_source_text_metrics(work_dir, manifest, blocks)
    adapter_audit = audit_adapter_source(work_dir, manifest, blocks, profile)
    adapter_failures = adapter_audit.get("failures")
    if not isinstance(adapter_failures, list) or adapter_failures:
        raise ValueError(
            "current adapter source evidence does not pass a complete audit: "
            + "; ".join(str(item) for item in adapter_failures or [])
        )
    adapter_binding = {
        key: value
        for key, value in adapter_audit.items()
        if key not in {"failures", "warnings"}
    }
    manual_binding = current_adapter_manual_review_binding(work_dir, manifest)
    if any(adapter_binding.get(field) != value for field, value in manual_binding.items()):
        raise ValueError("adapter source audit disagrees with manual-review binding")

    return {
        "profile": profile["id"],
        "source_manifest_sha256": sha256_artifact(
            manifest_path, boundary=work_dir
        ),
        "source_blocks_sha256": sha256_artifact(blocks_path, boundary=work_dir),
        "oracle_sha256": sha256_artifact(oracle_path, boundary=work_dir),
        "oracle_layout_sha256": sha256_artifact(
            oracle_layout_path, boundary=work_dir
        ),
        "source_pdf_sha256": source_pdf_sha256,
        "profile_sha256": canonical_profile_sha256(profile),
        "profile_file_sha256": sha256_artifact(profile_path, boundary=work_dir),
        "document_ir_sha256": sha256_artifact(ir_path, boundary=work_dir),
        "adapter_evidence_sha256": evidence_hash,
        "adapter_source": adapter_binding,
        "source_contact_sheets": contact_paths,
        "source_renders": render_bindings,
        "visuals": visual_bindings,
        **manual_binding,
        **text_metrics,
    }


def validate_source_audit_binding(
    work_dir: Path, audit_path: Path | None = None
) -> tuple[dict[str, object] | None, list[str]]:
    """Validate a passed source audit against every current frozen source input."""
    work_dir = lexical_absolute_path(work_dir)
    audit_path = audit_path or work_dir / "source-audit.json"
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_file(audit_path, boundary=work_dir, allow_missing=True)
    except ArtifactSafetyError as exc:
        return None, [f"source audit path is unsafe: {exc}"]
    if not os.path.lexists(audit_path):
        return None, ["source audit is missing"]
    try:
        report = read_json(audit_path)
    except (ArtifactSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, [f"source audit cannot be read: {exc}"]
    if not isinstance(report, dict):
        return None, ["source audit must be a JSON object"]

    errors: list[str] = []
    if not isinstance(report.get("status"), str) or report.get("status") != "passed":
        errors.append("source audit status is not passed")
    if report.get("failures") != []:
        errors.append("passed source audit failures must be an empty array")

    adapter_source = report.get("adapter_source")
    if not isinstance(adapter_source, dict):
        errors.append("passed source audit adapter_source must be an object")
    elif adapter_source.get("manual_source_review_required") is not False:
        errors.append("passed source audit cannot require manual source review")

    problem_inventory = report.get("problem_ids")
    if not isinstance(problem_inventory, dict):
        errors.append("passed source audit problem_ids must be an object")
    else:
        for field in ("oracle", "extracted", "missing", "extra"):
            value = problem_inventory.get(field)
            if (
                not isinstance(value, list)
                or any(not isinstance(item, str) or not item for item in value)
                or len(value) != len(set(value))
            ):
                errors.append(
                    f"passed source audit problem_ids.{field} must be a unique string array"
                )
        if problem_inventory.get("missing") != [] or problem_inventory.get("extra") != []:
            errors.append("passed source audit cannot report missing or extra problem IDs")
        if problem_inventory.get("oracle") != problem_inventory.get("extracted"):
            errors.append("passed source audit problem inventories disagree")

    if report.get("manual_source_review_required") is not False:
        errors.append("passed source audit manual-review binding must be false")
    if report.get("manual_review_pages") != []:
        errors.append("passed source audit manual_review_pages must be empty")
    statuses = report.get("adapter_page_statuses")
    if not isinstance(statuses, list) or any(
        status != "native_oracle_available" for status in statuses
    ):
        errors.append("passed source audit adapter page statuses are invalid")

    global_coverage_value = report.get("global_fivegram_coverage")
    if (
        not isinstance(global_coverage_value, (int, float))
        or isinstance(global_coverage_value, bool)
        or not math.isfinite(float(global_coverage_value))
        or not 0 <= float(global_coverage_value) <= 1
    ):
        errors.append(
            "source audit global_fivegram_coverage must be a finite number in [0, 1]"
        )
        global_coverage = None
    else:
        global_coverage = float(global_coverage_value)
    minimum_coverage: float | None = None
    try:
        minimum_coverage = validate_unit_interval_number(
            report.get("minimum_global_coverage"),
            "source audit minimum_global_coverage",
        )
        if global_coverage is not None and global_coverage < minimum_coverage:
            errors.append("passed source audit coverage is below its minimum")
    except ValueError as exc:
        errors.append(str(exc))

    page_results = report.get("page_results")
    total_hits = 0
    total_grams = 0
    if not isinstance(page_results, list) or not page_results:
        errors.append("passed source audit page_results must be a nonempty array")
    else:
        for page_number, page in enumerate(page_results, 1):
            if not isinstance(page, dict) or page.get("page") != page_number:
                errors.append("passed source audit page results must be contiguous and ordered")
                continue
            hits = page.get("matched_fivegrams")
            grams = page.get("oracle_fivegrams")
            block_count = page.get("block_count")
            if (
                not isinstance(hits, int)
                or isinstance(hits, bool)
                or not isinstance(grams, int)
                or isinstance(grams, bool)
                or hits < 0
                or grams < hits
            ):
                errors.append(f"passed source audit page {page_number} counts are invalid")
                continue
            if (
                not isinstance(block_count, int)
                or isinstance(block_count, bool)
                or block_count < 0
            ):
                errors.append(
                    f"passed source audit page {page_number} block_count is invalid"
                )
            page_coverage_value = page.get("coverage")
            if (
                not isinstance(page_coverage_value, (int, float))
                or isinstance(page_coverage_value, bool)
                or not math.isfinite(float(page_coverage_value))
                or not 0 <= float(page_coverage_value) <= 1
            ):
                errors.append(
                    f"source audit page {page_number} coverage must be a finite number in [0, 1]"
                )
            else:
                page_coverage = float(page_coverage_value)
                expected_page_coverage = hits / grams if grams else 1.0
                if page_coverage != round(expected_page_coverage, 4):
                    errors.append(
                        f"passed source audit page {page_number} coverage is inconsistent"
                    )
            total_hits += hits
            total_grams += grams
        expected_global_coverage = total_hits / total_grams if total_grams else 1.0
        if global_coverage is not None and global_coverage != round(
            expected_global_coverage, 4
        ):
            errors.append("passed source audit global coverage is inconsistent")
        if report.get("rendered_pages") != len(page_results):
            errors.append("passed source audit rendered page count is inconsistent")
    try:
        current_bindings = current_source_audit_bindings(work_dir)
    except (ArtifactSafetyError, KeyError, OSError, TypeError, ValueError) as exc:
        errors.append(f"current source freeze chain is invalid: {exc}")
        return report, errors
    page_count = current_bindings["page_count"]
    if not isinstance(page_results, list) or len(page_results) != page_count:
        errors.append("passed source audit page results do not cover every source page")
    if report.get("rendered_pages") != page_count:
        errors.append("passed source audit rendered page count does not match the source")
    source_renders = current_bindings.get("source_renders")
    if not isinstance(source_renders, list) or len(source_renders) != page_count:
        errors.append("current source render binding does not cover every source page")
    current_adapter = current_bindings.get("adapter_source")
    if isinstance(current_adapter, dict) and current_adapter.get("present") is True:
        if not isinstance(statuses, list) or len(statuses) != page_count:
            errors.append("adapter page statuses do not cover every source page")
    elif statuses != []:
        errors.append("native source audit must not report adapter page statuses")
    try:
        profile = load_work_profile(work_dir)
        profile_minimum = validate_unit_interval_number(
            profile.get("qa", {}).get("minimum_global_fivegram_coverage"),
            "frozen Profile minimum_global_fivegram_coverage",
        )
        if minimum_coverage is not None and minimum_coverage < profile_minimum:
            errors.append(
                "passed source audit minimum coverage is below the frozen Profile"
            )
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"cannot validate frozen Profile source threshold: {exc}")
    for field, current_value in current_bindings.items():
        if field not in report or report.get(field) != current_value:
            errors.append(f"source audit {field} binding is missing or stale")

    return report, errors


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
    validate_artifact_file(path, boundary=path.parent)
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
    return work_relative_artifact_path(
        work_dir, value, label="adapter artifact path"
    )


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
        "adapter_page_statuses": [],
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
        origin_path = _safe_adapter_artifact_path(
            work_dir, input_roles["origin"][0]["work_path"]
        )
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
        try:
            path = _safe_adapter_artifact_path(work_dir, work_path)
            validate_artifact_file(path, boundary=work_dir)
        except (ArtifactSafetyError, ValueError) as exc:
            failures.append(f"unsafe frozen adapter asset: {work_path}: {exc}")
            continue
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
            content_path = _safe_adapter_artifact_path(
                work_dir, input_roles["content"][0]["work_path"]
            )
            validate_artifact_file(content_path, boundary=work_dir)
            content = read_json(content_path)
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
    result["adapter_page_statuses"] = [
        page.get("status") if isinstance(page, dict) else None
        for page in page_statuses
    ] if isinstance(page_statuses, list) else []

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
            try:
                validate_artifact_file(
                    path, boundary=work_dir, allow_missing=True
                )
            except ArtifactSafetyError as exc:
                failures.append(
                    f"unsafe manual review comparison {relative_path}: {exc}"
                )
                continue
            if not os.path.lexists(path):
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
            try:
                validate_artifact_file(
                    path, boundary=work_dir, allow_missing=True
                )
            except ArtifactSafetyError as exc:
                failures.append(
                    f"unsafe manual review contact sheet {relative_path}: {exc}"
                )
                continue
            if not os.path.lexists(path):
                failures.append(f"manual review contact sheet is missing: {relative_path}")
            else:
                if sha256_artifact(path, boundary=work_dir) != contact.get("sha256"):
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

    work_dir = lexical_absolute_path(args.work_dir)
    report_path = work_dir / "source-audit.json"
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_file(
            report_path, boundary=work_dir, allow_missing=True
        )
        remove_artifact_file(report_path, boundary=work_dir)
    except ArtifactSafetyError as exc:
        raise SystemExit(f"unsafe source-audit work path: {exc}") from exc
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    oracle_path = work_dir / "oracle.txt"
    oracle_layout_path = work_dir / "oracle-layout.txt"
    for path in (manifest_path, blocks_path, oracle_path, oracle_layout_path):
        try:
            validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        except ArtifactSafetyError as exc:
            raise SystemExit(f"unsafe required artifact: {exc}") from exc
        if not os.path.lexists(path):
            raise SystemExit(f"missing required artifact: {path}")

    manifest = read_json(manifest_path)
    blocks = read_jsonl(blocks_path)
    oracle_text = read_artifact_text(
        oracle_path, boundary=work_dir, encoding="utf-8"
    )
    oracle_pages = oracle_text.split("\f")
    if oracle_pages and not oracle_pages[-1].strip():
        oracle_pages.pop()

    failures: list[str] = []
    warnings: list[str] = []
    profile = None
    ir_role_counts: dict[str, int] = {}
    ir_role_inventory: dict[str, dict] = {}
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
        validate_artifact_file(
            ir_path, boundary=work_dir, allow_missing=True
        )
        if os.path.lexists(ir_path):
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
    profile_minimum_global_coverage = (
        profile.get("qa", {}).get("minimum_global_fivegram_coverage", 0.95)
        if profile
        else 0.95
    )
    minimum_global_coverage = profile_minimum_global_coverage
    if args.minimum_global_coverage is not None:
        if args.minimum_global_coverage < profile_minimum_global_coverage:
            failures.append(
                "--minimum-global-coverage cannot lower the frozen Profile minimum"
            )
        else:
            minimum_global_coverage = args.minimum_global_coverage
    warn_page_below = (
        args.warn_page_below
        if args.warn_page_below is not None
        else profile.get("qa", {}).get("warn_page_below", 0.75)
        if profile
        else 0.75
    )
    source_pdf_sha256 = None
    try:
        _source_pdf, source_pdf_sha256 = current_source_pdf_binding(manifest)
    except (ArtifactSafetyError, KeyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"invalid source PDF binding: {exc}")

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

    renders_dir = work_dir / "renders"
    try:
        renders = _source_render_inventory(
            work_dir, manifest.get("page_count"), decode=True
        )
    except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"invalid source render evidence: {exc}")
        renders = []
    if len(renders) != manifest.get("page_count"):
        failures.append(
            f"rendered page count {len(renders)} != manifest {manifest.get('page_count')}"
        )
    try:
        contact_results = _source_contact_bindings(
            work_dir, manifest, decode=True
        )
    except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"invalid source contact-sheet evidence: {exc}")
        contact_results = []

    visual_results = []
    seen_visual_ids: set[str] = set()
    block_ids = {block.get("id") for block in blocks}
    visual_entries = manifest.get("visuals", [])
    if not isinstance(visual_entries, list):
        failures.append("manifest visuals must be an array")
        visual_entries = []
    try:
        current_manifest_visual_bindings(work_dir, manifest)
    except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"invalid source visual freeze tree: {exc}")
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
                else work_relative_artifact_path(
                    work_dir, visual.get("path"), label="visual crop path"
                )
            )
            try:
                relative_visual = visual_path.relative_to(work_dir / "visuals")
            except ValueError as exc:
                raise ValueError("visual crops must stay inside WORK/visuals") from exc
            if not relative_visual.parts:
                raise ValueError("visual crop path must name a file below WORK/visuals")
        except (TypeError, ValueError) as exc:
            failures.append(f"unsafe visual crop path for {visual_id}: {exc}")
            visual_path = None
        missing_contained = sorted(
            set(visual.get("contained_block_ids", [])) - block_ids
        )
        if visual_path is not None:
            try:
                validate_artifact_file(
                    visual_path, boundary=work_dir, allow_missing=True
                )
            except ArtifactSafetyError as exc:
                failures.append(
                    f"unsafe visual crop {visual.get('path')}: {exc}"
                )
                visual_path = None
        current_visual_hash = None
        if visual_path is None or not os.path.lexists(visual_path):
            failures.append(f"missing visual crop: {visual.get('path')}")
        else:
            current_visual_hash = sha256_artifact(
                visual_path, boundary=work_dir
            )
        if visual_path is not None and os.path.lexists(visual_path):
            expected_visual_hash = visual.get("sha256")
            if expected_visual_hash is not None and current_visual_hash != expected_visual_hash:
                failures.append(f"visual crop hash changed: {visual.get('path')}")
            try:
                width, height, mime = _fully_decode_image(visual_path)
                for field, actual in (
                    ("width", width),
                    ("height", height),
                    ("mime", mime),
                ):
                    if field in visual and visual.get(field) != actual:
                        failures.append(
                            f"visual crop declared {field} changed: {visual.get('path')}"
                        )
            except (ArtifactSafetyError, OSError, ValueError) as exc:
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
                "exists": visual_path is not None and os.path.lexists(visual_path),
                "sha256": current_visual_hash,
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

    try:
        current_source_audit_bindings(work_dir)
    except (ArtifactSafetyError, KeyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"invalid source audit freeze inputs: {exc}")

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
        "source_manifest_sha256": sha256_artifact(
            manifest_path, boundary=work_dir
        ),
        "source_blocks_sha256": sha256_artifact(
            blocks_path, boundary=work_dir
        ),
        "oracle_sha256": sha256_artifact(oracle_path, boundary=work_dir),
        "oracle_layout_sha256": sha256_artifact(
            oracle_layout_path, boundary=work_dir
        ),
        "source_pdf_sha256": source_pdf_sha256,
        "profile_sha256": canonical_profile_sha256(profile) if profile else None,
        "profile_file_sha256": (
            sha256_artifact(work_dir / "profile.json", boundary=work_dir)
            if profile
            else None
        ),
        "document_ir_sha256": (
            sha256_artifact(work_dir / "document-ir.json", boundary=work_dir)
            if os.path.lexists(work_dir / "document-ir.json")
            else None
        ),
        "adapter_evidence_sha256": adapter_audit["evidence_sha256"],
        "manual_source_review_required": adapter_audit[
            "manual_source_review_required"
        ],
        "manual_review_pages": adapter_audit["manual_review_pages"],
        "adapter_page_statuses": adapter_audit["adapter_page_statuses"],
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
        "page_count": manifest.get("page_count"),
        "rendered_pages": len(renders),
        "source_renders": [
            {
                "path": path.relative_to(work_dir).as_posix(),
                "sha256": sha256_artifact(path, boundary=renders_dir),
            }
            for path in renders
        ],
        "source_contact_sheets": contact_results,
        "page_results": page_results,
        "warnings": warnings,
        "failures": failures,
    }
    write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
