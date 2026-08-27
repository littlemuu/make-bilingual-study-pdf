#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from safe_artifacts import (
    ArtifactSafetyError,
    atomic_publish_with_writer,
    lexical_absolute_path,
    prepare_artifact_directory,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)

from common import json_loads_strict


def validate_visual_review_binding(
    work_dir: Path,
    visual_report: dict[str, Any],
    compile_report: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate a visual pass against the current compile and frozen outputs."""
    # Local imports keep compile entry points free of import-order cycles.
    from audit_docx import validate_compile_docx_binding
    from audit_outputs import validate_compile_output_binding

    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    compile_path = output_dir / "compile-audit.json"
    errors: list[str] = []
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_tree(output_dir, work_dir, allow_missing=False)
        validate_artifact_file(compile_path, boundary=work_dir)
        current_compile = json_loads_strict(
            read_artifact_text(compile_path, boundary=work_dir)
        )
    except (ArtifactSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"cannot validate visual compile gate: {exc}"]
    if not isinstance(current_compile, dict):
        return {}, ["compile audit must be a JSON object"]
    if compile_report is not None and compile_report != current_compile:
        errors.append("visual review compile metadata is not the current compile audit")
    compile_report = current_compile

    compile_status = compile_report.get("automated_status")
    if not isinstance(compile_status, str):
        errors.append("compile gate status must be a string")
    if compile_status != "passed":
        errors.append("compile automated gate is not passed")
    if compile_status == "passed" and compile_report.get("failures") != []:
        errors.append("passed compile gate failures must be an empty array")
    try:
        _, binding_errors = validate_compile_output_binding(
            work_dir, compile_report, output_dir / "output-audit.json"
        )
        errors.extend(f"compile output freeze chain: {item}" for item in binding_errors)
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        errors.append(f"cannot validate compile output binding: {exc}")

    schema_v2 = "docx_audit_bindings" in compile_report
    profile_path = work_dir / "profile.json"
    try:
        validate_artifact_file(profile_path, boundary=work_dir, allow_missing=True)
        if os.path.lexists(profile_path):
            profile = json_loads_strict(
                read_artifact_text(profile_path, boundary=work_dir)
            )
            if not isinstance(profile, dict):
                errors.append("frozen Profile must be a JSON object")
            else:
                schema_v2 = schema_v2 or profile.get("schema_version") == 2
    except (ArtifactSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"cannot validate frozen Profile for visual review: {exc}")
    if schema_v2 or "docx" in compile_report or "docx_audit_bindings" in compile_report:
        try:
            _, binding_errors = validate_compile_docx_binding(
                work_dir, compile_report, output_dir / "docx-audit.json"
            )
            errors.extend(f"DOCX freeze chain: {item}" for item in binding_errors)
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            errors.append(f"cannot validate DOCX freeze chain: {exc}")

    if not isinstance(visual_report.get("status"), str):
        errors.append("visual review status must be a string")
    if visual_report.get("status") != "passed":
        errors.append("visual review status is not passed")
    if visual_report.get("status") == "passed" and visual_report.get(
        "failures"
    ) != []:
        errors.append("passed visual review failures must be an empty array")
    page_count = compile_report.get("page_count")
    if type(page_count) is not int or page_count < 1:
        errors.append("compile page_count must be a positive integer")
        page_count = 0
    reviewed_pages = visual_report.get("reviewed_pages")
    if (
        not isinstance(reviewed_pages, list)
        or any(type(item) is not int for item in reviewed_pages)
        or reviewed_pages != list(range(1, page_count + 1))
    ):
        errors.append("visual review must cover every page exactly once in order")
    if visual_report.get("page_count") != page_count:
        errors.append("visual review page_count does not match compile QA")
    if visual_report.get("compile_audit_sha256") != sha256_artifact(
        compile_path, boundary=work_dir
    ):
        errors.append("visual review is bound to different compile-audit bytes")

    pdf_name = compile_report.get("pdf")
    try:
        pdf_path = work_relative_artifact_path(
            output_dir, pdf_name, label="visual compiled PDF path"
        )
        if pdf_path.parent not in {output_dir, output_dir / "build"}:
            raise ArtifactSafetyError(
                "compiled PDF must be a direct output or output/build child"
            )
        validate_artifact_file(pdf_path, boundary=work_dir)
        pdf_hash = sha256_artifact(pdf_path, boundary=work_dir)
        if pdf_hash != compile_report.get("pdf_sha256"):
            errors.append("compiled PDF changed after automated QA")
    except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
        errors.append(f"compiled PDF path is invalid: {exc}")
        pdf_hash = None
    if visual_report.get("pdf") != pdf_name:
        errors.append("visual review refers to a different PDF path")
    if visual_report.get("pdf_sha256") != compile_report.get("pdf_sha256"):
        errors.append("visual review refers to a different PDF hash")

    contacts = compile_report.get("contact_sheets")
    expected_paths: list[str] = []
    expected_hashes: dict[str, str] = {}
    seen_contact_paths: set[str] = set()
    covered_pages: list[int] = []
    if not isinstance(contacts, list) or not contacts:
        errors.append("compile gate has no contact-sheet evidence")
    else:
        for index, item in enumerate(contacts):
            if not isinstance(item, dict):
                errors.append(f"compile contact sheet {index} must be an object")
                continue
            relative = item.get("path")
            expected_hash = item.get("sha256")
            first_page = item.get("first_page")
            last_page = item.get("last_page")
            try:
                contact_path = work_relative_artifact_path(
                    output_dir, relative, label=f"compile contact sheet {index} path"
                )
                contact_path.relative_to(output_dir / "contact")
                validate_artifact_file(contact_path, boundary=work_dir)
                if (
                    not isinstance(expected_hash, str)
                    or sha256_artifact(contact_path, boundary=work_dir)
                    != expected_hash
                ):
                    errors.append(f"compile contact sheet {index} is missing or changed")
            except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
                errors.append(f"compile contact sheet {index} is invalid: {exc}")
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                continue
            if relative in seen_contact_paths:
                errors.append("compile contact-sheet paths must be unique")
                continue
            seen_contact_paths.add(relative)
            expected_paths.append(relative)
            expected_hashes[relative] = expected_hash
            if (
                type(first_page) is not int
                or type(last_page) is not int
                or first_page < 1
                or last_page < first_page
                or last_page > page_count
            ):
                errors.append(f"compile contact sheet {index} has an invalid page range")
            else:
                covered_pages.extend(range(first_page, last_page + 1))
    if covered_pages != list(range(1, page_count + 1)):
        errors.append("compile contact sheets do not cover every page exactly once")
    if visual_report.get("contact_sheets_inspected") != expected_paths:
        errors.append("visual review did not inspect every compiled contact sheet")
    if visual_report.get("contact_sheets_sha256") != expected_hashes:
        errors.append("visual review contact-sheet hashes do not match compile QA")
    if not isinstance(visual_report.get("notes"), str) or not visual_report[
        "notes"
    ].strip():
        errors.append("visual review has no concrete inspection notes")
    return compile_report, errors


def image_ink_ratio(path: Path) -> float:
    validate_artifact_file(path, boundary=path.parent)
    with Image.open(path) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()
        nonwhite = sum(histogram[:248])
        return nonwhite / max(1, image.width * image.height)


def make_contact_sheets(
    page_paths: list[Path], contact_dir: Path, columns: int = 4, rows: int = 4
) -> list[dict[str, Any]]:
    boundary = contact_dir.parent
    validate_artifact_tree(contact_dir, boundary)
    contact_dir = prepare_artifact_directory(contact_dir, boundary=boundary)
    for old in contact_dir.glob("contact-*.png"):
        remove_artifact_file(old, boundary=boundary)
    thumb_width = 210
    label_height = 24
    gap = 12
    per_sheet = columns * rows
    results: list[dict[str, Any]] = []
    for sheet_index in range(0, len(page_paths), per_sheet):
        group = page_paths[sheet_index : sheet_index + per_sheet]
        thumbnails: list[Image.Image] = []
        for page_number, path in enumerate(group, start=sheet_index + 1):
            validate_artifact_file(path, boundary=path.parent)
            with Image.open(path) as original:
                image = original.convert("RGB")
                height = round(image.height * thumb_width / image.width)
                image.thumbnail((thumb_width, height), Image.Resampling.LANCZOS)
                tile = Image.new("RGB", (thumb_width, height + label_height), "white")
                tile.paste(image, ((thumb_width - image.width) // 2, label_height))
                ImageDraw.Draw(tile).text((6, 5), f"Page {page_number}", fill="black")
                thumbnails.append(tile)
        tile_height = max(tile.height for tile in thumbnails)
        actual_columns = min(columns, len(thumbnails))
        actual_rows = math.ceil(len(thumbnails) / actual_columns)
        canvas = Image.new(
            "RGB",
            (
                actual_columns * thumb_width + (actual_columns + 1) * gap,
                actual_rows * tile_height + (actual_rows + 1) * gap,
            ),
            "#d9dde5",
        )
        for tile_index, tile in enumerate(thumbnails):
            x = gap + (tile_index % actual_columns) * (thumb_width + gap)
            y = gap + (tile_index // actual_columns) * (tile_height + gap)
            bordered = ImageOps.expand(tile, border=1, fill="#8b93a1")
            canvas.paste(bordered, (x, y))
        filename = f"contact-{sheet_index // per_sheet + 1:03d}.png"
        path = contact_dir / filename
        atomic_publish_with_writer(path, canvas.save, boundary=boundary)
        results.append(
            {
                "path": f"{contact_dir.name}/{filename}",
                "sha256": sha256_artifact(path, boundary=boundary),
                "first_page": sheet_index + 1,
                "last_page": sheet_index + len(group),
            }
        )
    return results
