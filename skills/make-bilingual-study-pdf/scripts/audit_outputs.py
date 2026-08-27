#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from common import json_loads_strict
from profile import canonical_profile_sha256, load_work_profile, profile_contract
from build_outputs import (
    load_semantic_model,
    markdown_escape,
    response_marker,
    source_only_markdown_body,
)
from safe_artifacts import (
    ArtifactSafetyError,
    artifact_size,
    atomic_write_text,
    lexical_absolute_path,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
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

FIXED_OUTPUT_FILE_NAMES = frozenset(
    {
        "build-manifest.json",
        "output-audit.json",
        "docx-audit.json",
        "compile-audit.json",
        "visual-review.json",
        "qa-report.json",
    }
)
FIXED_OUTPUT_DIRECTORY_NAMES = frozenset(
    {"assets", "build", "docx-build", "pdf-renders", "contact"}
)


def _read_json(path: Path, work_dir: Path) -> Any:
    return json_loads_strict(read_artifact_text(path, boundary=work_dir))


def _read_jsonl(path: Path, work_dir: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        read_artifact_text(path, boundary=work_dir).splitlines(), 1
    ):
        if not raw.strip():
            continue
        try:
            value = json_loads_strict(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid JSONL object at {path}:{line_number}")
        values.append(value)
    return values


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    return os.path.lexists(path)


def _atomic_write_json(path: Path, value: object, work_dir: Path) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )


def _portable_role_key(path: Path, output_dir: Path) -> str:
    return path.relative_to(output_dir).as_posix().casefold()


def validate_build_artifact_roles(
    work_dir: Path, build: dict[str, Any]
) -> dict[str, Path | list[tuple[dict[str, Any], Path]] | None]:
    """Resolve distinct output roles without allowing fixed gate aliases."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    assets_dir = output_dir / "assets"
    roles: dict[str, Path | list[tuple[dict[str, Any], Path]] | None] = {
        "markdown": None,
        "latex": None,
        "assets": [],
    }
    claimed_paths: dict[str, str] = {}

    for field, label in (
        ("markdown", "build Markdown path"),
        ("latex", "build LaTeX path"),
    ):
        value = build.get(field)
        if value is None:
            continue
        path = work_relative_artifact_path(output_dir, value, label=label)
        if path.parent != output_dir:
            raise ArtifactSafetyError(
                "build Markdown and LaTeX paths must be direct output children"
            )
        name_key = path.name.casefold()
        if name_key in FIXED_OUTPUT_FILE_NAMES:
            raise ArtifactSafetyError(f"build {field} path aliases a fixed gate artifact")
        if name_key in FIXED_OUTPUT_DIRECTORY_NAMES:
            raise ArtifactSafetyError(f"build {field} path aliases a fixed output directory")
        role_key = _portable_role_key(path, output_dir)
        if role_key in claimed_paths:
            raise ArtifactSafetyError(
                f"build artifact roles {claimed_paths[role_key]} and {field} alias"
            )
        claimed_paths[role_key] = field
        roles[field] = path

    assets = build.get("assets", [])
    if not isinstance(assets, list):
        raise ArtifactSafetyError("build assets metadata must be an array")
    asset_roles: list[tuple[dict[str, Any], Path]] = []
    seen_ids: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ArtifactSafetyError(
                f"build asset metadata entry {index} must be an object"
            )
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ArtifactSafetyError(f"build asset metadata entry {index} has an invalid id")
        id_key = asset_id.casefold()
        if id_key in seen_ids:
            raise ArtifactSafetyError(f"build asset id is duplicated: {asset_id}")
        seen_ids.add(id_key)
        value = asset.get("path")
        path = work_relative_artifact_path(
            output_dir, value, label=f"build asset path {index}"
        )
        try:
            relative_asset = path.relative_to(assets_dir)
        except ValueError as exc:
            raise ArtifactSafetyError(
                "build asset paths must stay inside output/assets"
            ) from exc
        if not relative_asset.parts:
            raise ArtifactSafetyError("build asset path must name a file")
        role_key = _portable_role_key(path, output_dir)
        if role_key in claimed_paths:
            raise ArtifactSafetyError(
                f"build artifact roles {claimed_paths[role_key]} and asset {asset_id} alias"
            )
        claimed_paths[role_key] = f"asset {asset_id}"
        asset_roles.append((asset, path))
    roles["assets"] = asset_roles
    return roles


def current_output_artifact_bindings(
    work_dir: Path,
    build_manifest_path: Path,
    build: dict[str, Any],
) -> dict[str, Any]:
    """Return the portable paths and current hashes frozen by output audit."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    validate_artifact_file(build_manifest_path, boundary=work_dir)
    roles = validate_build_artifact_roles(work_dir, build)

    bindings: dict[str, Any] = {}
    for field, label in (
        ("markdown", "build Markdown path"),
        ("latex", "build LaTeX path"),
    ):
        path = roles[field]
        if path is None:
            continue
        assert isinstance(path, Path)
        value = build[field]
        validate_artifact_file(path, boundary=work_dir)
        bindings[field] = {
            "path": value,
            "sha256": sha256_artifact(path, boundary=work_dir),
        }

    asset_bindings: list[dict[str, str]] = []
    asset_roles = roles["assets"]
    assert isinstance(asset_roles, list)
    for asset, path in asset_roles:
        value = asset["path"]
        validate_artifact_file(path, boundary=work_dir)
        asset_bindings.append(
            {
                "path": value,
                "sha256": sha256_artifact(path, boundary=work_dir),
            }
        )
    bindings["assets"] = asset_bindings
    return bindings


def validate_output_audit_binding(
    work_dir: Path,
    audit_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Validate a passed output audit against the current build and output bytes."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    build_manifest_path = output_dir / "build-manifest.json"
    audit_path = audit_path or output_dir / "output-audit.json"
    report = _read_json(audit_path, work_dir)
    build = _read_json(build_manifest_path, work_dir)
    if not isinstance(report, dict) or not isinstance(build, dict):
        raise ValueError("output audit and build manifest must be JSON objects")

    errors: list[str] = []
    from audit_translation import validate_translation_audit_binding

    translation_audit_path = work_dir / "translation" / "translation-audit.json"
    try:
        _translation_report, translation_errors = validate_translation_audit_binding(
            work_dir, translation_audit_path
        )
        errors.extend(
            f"translation freeze chain: {message}" for message in translation_errors
        )
        current_translation_hash = sha256_artifact(
            translation_audit_path, boundary=work_dir
        )
        if build.get("translation_audit_sha256") != current_translation_hash:
            errors.append("build translation audit binding is missing or stale")
    except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
        errors.append(f"translation freeze chain is invalid: {exc}")
    if not isinstance(report.get("status"), str) or report.get("status") != "passed":
        errors.append("output audit status is not passed")
    if report.get("failures") != []:
        errors.append("passed output audit failures must be an empty array")
    constraint_checks = report.get("semantic_constraint_checks")
    if (
        not isinstance(constraint_checks, dict)
        or any(
            not isinstance(name, str) or not name or result is not True
            for name, result in constraint_checks.items()
        )
    ):
        errors.append("passed output audit semantic constraint checks are invalid")
    else:
        try:
            profile = load_work_profile(work_dir)
            if profile.get("schema_version") == 2:
                expected_constraints = set(profile_contract(profile)["constraints"])
                if set(constraint_checks) != expected_constraints:
                    errors.append(
                        "passed output audit semantic constraint inventory is incomplete"
                    )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"cannot validate output Profile constraints: {exc}")
    try:
        current_bindings = current_output_artifact_bindings(
            work_dir, build_manifest_path, build
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return report, build, [f"current output artifacts are invalid: {exc}"]

    expected_manifest_hash = report.get("build_manifest_sha256")
    expected_bindings = report.get("artifact_bindings")
    if expected_manifest_hash != sha256_artifact(
        build_manifest_path, boundary=work_dir
    ):
        errors.append("output audit is bound to a different build manifest")
    if expected_bindings != current_bindings:
        errors.append("output audit artifact bindings are missing or stale")
    if report.get("markdown") != build.get("markdown"):
        errors.append("output audit Markdown role is missing or stale")
    if report.get("latex") != build.get("latex"):
        errors.append("output audit LaTeX role is missing or stale")
    assets = build.get("assets")
    expected_asset_count = len(assets) if isinstance(assets, list) else None
    if (
        not isinstance(report.get("asset_count"), int)
        or isinstance(report.get("asset_count"), bool)
        or report.get("asset_count") != expected_asset_count
    ):
        errors.append("output audit asset count is missing or stale")
    if report.get("block_count") != build.get("block_count"):
        errors.append("output audit block count is missing or stale")
    if report.get("disposition_counts") != build.get("disposition_counts"):
        errors.append("output audit disposition counts are missing or stale")
    return report, build, errors


def validate_compile_output_binding(
    work_dir: Path,
    compile_report: dict[str, Any],
    output_audit_path: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Validate a compile report against the current audited output snapshot."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    build_manifest_path = output_dir / "build-manifest.json"
    output_audit_path = output_audit_path or output_dir / "output-audit.json"

    if not isinstance(compile_report, dict):
        return {}, ["compile report must be a JSON object"]

    output_audit, build, errors = validate_output_audit_binding(
        work_dir, output_audit_path
    )
    errors = list(errors)
    if output_audit.get("status") != "passed":
        errors.insert(0, "output audit status is not passed")

    expected = {
        "build_manifest_sha256": sha256_artifact(
            build_manifest_path, boundary=work_dir
        ),
        "output_audit_sha256": sha256_artifact(
            output_audit_path, boundary=work_dir
        ),
    }
    for field, current_hash in expected.items():
        if compile_report.get(field) != current_hash:
            errors.append(f"compile report {field} binding is missing or stale")

    automated_status = compile_report.get("automated_status")
    if not isinstance(automated_status, str):
        errors.append("compile automated_status must be a string")
    elif automated_status != "passed":
        errors.append("compile automated_status is not passed")
    if compile_report.get("failures") != []:
        errors.append("passed compile report failures must be an empty array")

    checks = compile_report.get("checks")
    if not isinstance(checks, dict) or not checks:
        errors.append("passed compile report checks must be a non-empty object")
        checks = {}
    else:
        if any(not isinstance(name, str) or not name for name in checks):
            errors.append("compile check names must be non-empty strings")
        if any(value is not True for value in checks.values()):
            errors.append("every passed compile check must be exactly true")

    docx_mode = any(
        key in compile_report
        for key in ("docx", "docx_audit_sha256", "docx_audit_bindings")
    )
    expected_status = "passed" if docx_mode else "needs_visual_review"
    if not isinstance(compile_report.get("status"), str):
        errors.append("compile status must be a string")
    elif compile_report.get("status") != expected_status:
        errors.append(
            f"compile status does not match {'DOCX' if docx_mode else 'TeX'} provenance"
        )

    def strict_int(value: Any, *, minimum: int = 0) -> bool:
        return type(value) is int and value >= minimum

    page_count = compile_report.get("page_count")
    if not strict_int(page_count, minimum=1):
        errors.append("compile page_count must be a positive integer")

    if docx_mode:
        required_checks = {
            "pdf_created",
            "all_pages_rendered",
            "all_renders_decodable",
            "contact_sheets_complete",
            "all_pages_a4",
            "no_apparently_blank_pages",
            "chinese_extractable",
            "all_fonts_embedded",
            "requested_cjk_font_resolved_exactly",
            "expected_cjk_font_embedded",
        }
        declared_docx_bindings = compile_report.get("docx_audit_bindings")
        is_v2_docx = "document_ir_sha256" in compile_report or (
            isinstance(declared_docx_bindings, dict)
            and "document_ir_sha256" in declared_docx_bindings
        )
        required_checks.add(
            "all_textual_role_occurrences_present"
            if is_v2_docx
            else "problem_count_matches"
        )
        if is_v2_docx:
            required_checks.update(
                {
                    "visual_role_occurrences_present",
                    "profile_target_text_extractable",
                }
            )
        missing_checks = sorted(required_checks - set(checks))
        unexpected_checks = sorted(set(checks) - required_checks)
        if missing_checks or unexpected_checks:
            details = []
            if missing_checks:
                details.append("missing " + ", ".join(missing_checks))
            if unexpected_checks:
                details.append("unexpected " + ", ".join(unexpected_checks))
            errors.append(
                "DOCX compile checks do not match the producer contract: "
                + "; ".join(details)
            )
        rendered_count = compile_report.get("rendered_page_count")
        if not strict_int(rendered_count, minimum=1) or rendered_count != page_count:
            errors.append("DOCX compile rendered_page_count does not match page_count")
        if compile_report.get("invalid_renders") != []:
            errors.append("passed DOCX compile invalid_renders must be empty")
        if compile_report.get("blank_pages") != []:
            errors.append("passed DOCX compile blank_pages must be empty")
        if not strict_int(compile_report.get("font_count"), minimum=1):
            errors.append("DOCX compile font_count must be a positive integer")
        if not strict_int(compile_report.get("pdf_image_count")):
            errors.append("DOCX compile pdf_image_count must be a nonnegative integer")
        if not strict_int(compile_report.get("minimum_page_text_characters")):
            errors.append(
                "DOCX compile minimum_page_text_characters must be a nonnegative integer"
            )
        minimum_nonwhite_fraction = compile_report.get("minimum_nonwhite_fraction")
        if (
            isinstance(minimum_nonwhite_fraction, bool)
            or not isinstance(minimum_nonwhite_fraction, (int, float))
            or not math.isfinite(minimum_nonwhite_fraction)
            or minimum_nonwhite_fraction < 0
            or minimum_nonwhite_fraction > 1
        ):
            errors.append(
                "DOCX compile minimum_nonwhite_fraction must be finite and between zero and one"
            )
        for field in (
            "requested_cjk_font",
            "resolved_cjk_family",
            "resolved_cjk_file",
        ):
            if not isinstance(compile_report.get(field), str) or not compile_report.get(
                field
            ):
                errors.append(f"DOCX compile {field} must be a non-empty string")
        if is_v2_docx:
            inventory = build.get("role_inventory")
            expected_role_counts: dict[str, int] = {}
            if not isinstance(inventory, dict):
                errors.append("build manifest role_inventory must be an object")
            else:
                for role, item in inventory.items():
                    count = item.get("occurrence_count") if isinstance(item, dict) else None
                    if not isinstance(role, str) or not role or not strict_int(count):
                        errors.append(
                            "build manifest role_inventory contains an invalid occurrence count"
                        )
                        continue
                    expected_role_counts[role] = count
            if compile_report.get("role_counts") != expected_role_counts:
                errors.append("DOCX compile role_counts do not match the current build")
            expected_occurrences: dict[str, bool] = {}
            expected_presence_counts = {
                role: 0 for role in expected_role_counts
            }
            try:
                ir_path = work_dir / "document-ir.json"
                validate_artifact_file(ir_path, boundary=work_dir)
                ir = json_loads_strict(read_artifact_text(ir_path, boundary=work_dir))
                if not isinstance(ir, dict):
                    raise ValueError("document IR must be a JSON object")
                nodes = ir.get("nodes")
                groups = ir.get("semantic_groups")
                if not isinstance(nodes, list) or not isinstance(groups, list):
                    raise ValueError("document IR semantic evidence must be arrays")
                nodes_by_id = {
                    node.get("id"): node
                    for node in nodes
                    if isinstance(node, dict) and isinstance(node.get("id"), str)
                }
                for group in groups:
                    if not isinstance(group, dict):
                        raise ValueError("document IR semantic group must be an object")
                    group_id = group.get("id")
                    role = group.get("role")
                    anchor = nodes_by_id.get(group.get("anchor_node_id"))
                    if (
                        not isinstance(group_id, str)
                        or not group_id
                        or role not in expected_presence_counts
                        or not isinstance(anchor, dict)
                    ):
                        raise ValueError("document IR semantic group binding is invalid")
                    output = anchor.get("semantic", {}).get("output")
                    if output in {"visual-once", "artifact-omitted"}:
                        continue
                    expected_occurrences[group_id] = True
                    expected_presence_counts[role] += 1
            except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
                errors.append(f"V2 DOCX compile IR evidence is invalid: {exc}")
            if compile_report.get("occurrence_presence") != expected_occurrences:
                errors.append(
                    "V2 DOCX compile occurrence_presence does not exactly cover current textual groups"
                )
            if compile_report.get("role_presence_counts") != expected_presence_counts:
                errors.append(
                    "V2 DOCX compile role_presence_counts do not match current textual groups"
                )
        else:
            problem_ids = build.get("problem_ids")
            if not isinstance(problem_ids, list) or any(
                not isinstance(item, str) or not item for item in problem_ids
            ):
                errors.append("build manifest problem_ids must be an array of strings")
            elif compile_report.get("problem_count") != len(problem_ids):
                errors.append("V1 DOCX compile problem_count does not match the current build")
    else:
        required_checks = {
            "latexmk_succeeded",
            "pdf_created",
            "no_missing_characters",
            "no_undefined_references",
            "all_pages_rendered",
            "no_apparently_blank_pages",
            "all_problem_ids_present",
            "chinese_text_extractable",
        }
        missing_checks = sorted(required_checks - set(checks))
        unexpected_checks = sorted(set(checks) - required_checks)
        if missing_checks or unexpected_checks:
            details = []
            if missing_checks:
                details.append("missing " + ", ".join(missing_checks))
            if unexpected_checks:
                details.append("unexpected " + ", ".join(unexpected_checks))
            errors.append(
                "TeX compile checks do not match the producer contract: "
                + "; ".join(details)
            )
        rendered_count = compile_report.get("rendered_pages")
        if not strict_int(rendered_count, minimum=1) or rendered_count != page_count:
            errors.append("TeX compile rendered_pages does not match page_count")
        ink_ratios = compile_report.get("ink_ratios")
        if (
            not isinstance(ink_ratios, list)
            or len(ink_ratios) != page_count
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or value > 1
                for value in ink_ratios
            )
        ):
            errors.append("TeX compile ink_ratios do not cover every page")
        if compile_report.get("blank_pages") != []:
            errors.append("passed TeX compile blank_pages must be empty")
        if not strict_int(compile_report.get("missing_character_count")) or compile_report.get(
            "missing_character_count"
        ) != 0:
            errors.append("passed TeX compile missing_character_count must be zero")
        if not strict_int(compile_report.get("undefined_reference_count")) or compile_report.get(
            "undefined_reference_count"
        ) != 0:
            errors.append("passed TeX compile undefined_reference_count must be zero")
        frozen_problem_ids = build.get("problem_ids")
        if not isinstance(frozen_problem_ids, list) or any(
            not isinstance(item, str) or not item for item in frozen_problem_ids
        ):
            errors.append("build manifest problem_ids must be an array of strings")
        elif compile_report.get("problem_ids_expected") != frozen_problem_ids:
            errors.append(
                "TeX compile problem_ids_expected do not match the current build"
            )
        if compile_report.get("problem_ids_missing") != []:
            errors.append("passed TeX compile problem_ids_missing must be empty")
        source_contains_cjk = compile_report.get("source_contains_cjk")
        extracted_text_contains_cjk = compile_report.get(
            "extracted_text_contains_cjk"
        )
        if type(source_contains_cjk) is not bool or type(
            extracted_text_contains_cjk
        ) is not bool:
            errors.append("TeX compile CJK extraction evidence must be boolean")
        elif source_contains_cjk and not extracted_text_contains_cjk:
            errors.append("TeX compile Chinese text extraction evidence is stale")

    expected_pdf_path: Path | None = None
    if docx_mode:
        try:
            docx_path = work_relative_artifact_path(
                output_dir,
                compile_report.get("docx"),
                label="compile report DOCX path",
            )
            if docx_path.parent != output_dir or docx_path.suffix.lower() != ".docx":
                raise ArtifactSafetyError(
                    "DOCX compile input must be a direct output DOCX child"
                )
            validate_artifact_file(docx_path, boundary=work_dir)
            if compile_report.get("docx_sha256") != sha256_artifact(
                docx_path, boundary=work_dir
            ):
                errors.append("compile report DOCX bytes are missing or stale")
            expected_pdf_path = output_dir / f"{docx_path.stem}.pdf"
        except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"compile report DOCX provenance is invalid: {exc}")
    else:
        latex_name = build.get("latex")
        try:
            latex_path = work_relative_artifact_path(
                output_dir, latex_name, label="build manifest LaTeX path"
            )
            if latex_path.parent != output_dir or latex_path.suffix.lower() != ".tex":
                raise ArtifactSafetyError(
                    "TeX compile input must be a direct output TeX child"
                )
            expected_pdf_path = output_dir / "build" / f"{latex_path.stem}.pdf"
        except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"compile report TeX provenance is invalid: {exc}")

    if expected_pdf_path is not None:
        expected_pdf_name = expected_pdf_path.relative_to(output_dir).as_posix()
        if compile_report.get("pdf") != expected_pdf_name:
            errors.append("compile report PDF path does not match its compile provenance")
        try:
            validate_artifact_file(expected_pdf_path, boundary=work_dir)
            if artifact_size(expected_pdf_path, boundary=work_dir) <= 0:
                errors.append("compile report PDF is empty")
            if compile_report.get("pdf_sha256") != sha256_artifact(
                expected_pdf_path, boundary=work_dir
            ):
                errors.append("compile report PDF bytes are missing or stale")
        except (ArtifactSafetyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"compile report PDF provenance is invalid: {exc}")
    return expected, errors


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

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, work_dir, allow_missing=False)
    assets_dir = output_dir / "assets"
    validate_artifact_tree(assets_dir, work_dir, allow_missing=False)
    build_manifest_path = output_dir / "build-manifest.json"
    output_audit_path = output_dir / "output-audit.json"
    validate_artifact_file(
        build_manifest_path, boundary=work_dir, allow_missing=True
    )
    validate_artifact_file(
        output_audit_path, boundary=work_dir, allow_missing=True
    )
    if not os.path.lexists(build_manifest_path):
        raise SystemExit(f"missing build manifest: {build_manifest_path}")
    build = _read_json(build_manifest_path, work_dir)
    if not isinstance(build, dict):
        raise SystemExit("build manifest must be a JSON object")
    try:
        build_roles = validate_build_artifact_roles(work_dir, build)
    except (ArtifactSafetyError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid build artifact roles: {exc}") from exc

    # Role aliases are rejected before an existing audit or generated artifact
    # can be removed. Later content failures still invalidate the old pass.
    remove_artifact_file(output_audit_path, boundary=work_dir)
    manifest = _read_json(work_dir / "manifest.json", work_dir)
    blocks = _read_jsonl(work_dir / "blocks.jsonl", work_dir)
    failures: list[str] = []
    warnings: list[str] = []

    # A byte-identical translation-audit file is not sufficient if one of the
    # source or translation artifacts it certifies has drifted. Revalidate the
    # complete recursive freeze chain before this producer can publish a new
    # passing output audit.
    try:
        from audit_translation import validate_translation_audit_binding

        _translation_report, translation_errors = validate_translation_audit_binding(
            work_dir, work_dir / "translation" / "translation-audit.json"
        )
        failures.extend(
            f"translation freeze chain: {message}"
            for message in translation_errors
        )
    except (ArtifactSafetyError, KeyError, OSError, TypeError, ValueError) as exc:
        failures.append(f"translation freeze chain is invalid: {exc}")

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
        if not _artifact_exists(path, work_dir) or sha256_artifact(
            path, boundary=work_dir
        ) != expected:
            failures.append(f"build input changed: {path.relative_to(work_dir)}")
    profile = None
    try:
        profile = load_work_profile(work_dir)
        if build.get("profile_sha256") != canonical_profile_sha256(profile):
            failures.append("build input changed: canonical profile")
    except ValueError as exc:
        failures.append(f"invalid profile binding: {exc}")

    markdown_path = build_roles["markdown"]
    latex_path = build_roles["latex"]
    if not isinstance(markdown_path, Path) or not isinstance(latex_path, Path):
        raise SystemExit("build Markdown and LaTeX roles must name output files")
    for path, expected in (
        (markdown_path, build["markdown_sha256"]),
        (latex_path, build["latex_sha256"]),
    ):
        if not _artifact_exists(path, work_dir) or sha256_artifact(
            path, boundary=work_dir
        ) != expected:
            failures.append(f"generated output changed: {path.name}")

    asset_roles = build_roles["assets"]
    assert isinstance(asset_roles, list)
    for asset, path in asset_roles:
        if not _artifact_exists(path, work_dir) or sha256_artifact(
            path, boundary=work_dir
        ) != asset.get("sha256"):
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
    if _artifact_exists(markdown_path, work_dir):
        markdown = read_artifact_text(markdown_path, boundary=work_dir)
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
                _read_jsonl(translations_path, work_dir)
                if _artifact_exists(translations_path, work_dir)
                else []
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
    if not failures:
        report["build_manifest_sha256"] = sha256_artifact(
            build_manifest_path, boundary=work_dir
        )
        report["artifact_bindings"] = current_output_artifact_bindings(
            work_dir, build_manifest_path, build
        )
    _atomic_write_json(output_audit_path, report, work_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
