#!/usr/bin/env python3
"""Audit a V2 bilingual DOCX before PDF rendering and visual review."""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree
from common import json_loads_strict
from audit_outputs import validate_compile_output_binding, validate_output_audit_binding
from profile import load_profile, load_work_profile, profile_contract, semantic_group, target_text_pattern
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    read_artifact_bytes,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
GENERIC_MARKERS = ("V23-CALLOUT-BEGIN", "V23-CALLOUT-END")
STYLE_COLORS = {
    "problem": "D97706",
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

V1_DOCX_AUDIT_CHECKS = frozenset(
    {
        "docx_opens",
        "problem_ids_are_unique",
        "no_internal_problem_markers",
        "chinese_present",
        "minimum_images_met",
        "problem_callout_borders_are_aligned",
        "problem_count_matches",
        "example_count_matches",
        "tip_count_matches",
        "external_link_count_matches",
    }
)
V2_DOCX_AUDIT_CHECKS = frozenset(
    {
        "docx_opens",
        "frozen_role_inventory_matches",
        "every_role_occurrence_is_evidenced",
        "source_only_nodes_appear_once",
        "complete_containers_are_structurally_stable",
        "scoped_anchor_callouts_are_structurally_stable",
        "all_structural_callouts_are_accounted_for",
        "non_structural_anchor_groups_are_not_boxed",
        "no_internal_problem_markers",
        "no_internal_generic_markers",
        "chinese_present",
        "external_links_match",
        "visual_occurrences_are_embedded",
        "minimum_images_met",
        "structured_tables_are_native_word_tables",
        "external_link_count_matches",
    }
)


def _read_work_json(path: Path, work_dir: Path, *, label: str) -> dict[str, Any]:
    value = json_loads_strict(read_artifact_text(path, boundary=work_dir))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _passed_checks_errors(
    report: dict[str, Any],
    *,
    label: str,
    required_checks: frozenset[str] | None = None,
) -> list[str]:
    checks = report.get("checks")
    if not isinstance(checks, dict) or not checks:
        return [f"passed {label} checks must be a non-empty object"]
    errors: list[str] = []
    if any(not isinstance(name, str) or not name for name in checks):
        errors.append(f"{label} check names must be non-empty strings")
    if any(value is not True for value in checks.values()):
        errors.append(f"every passed {label} check must be exactly true")
    if required_checks is not None and set(checks) != required_checks:
        missing = sorted(required_checks - set(checks))
        unexpected = sorted(set(checks) - required_checks)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        errors.append(
            f"{label} checks do not match the producer contract: "
            + "; ".join(details)
        )
    return errors


def _strict_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _role_counts_from_build(build: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    inventory = build.get("role_inventory")
    if not isinstance(inventory, dict):
        return {}, ["build manifest role_inventory must be an object"]
    counts: dict[str, int] = {}
    errors: list[str] = []
    for role, item in inventory.items():
        count = item.get("occurrence_count") if isinstance(item, dict) else None
        if not isinstance(role, str) or not role or not _strict_nonnegative_int(count):
            errors.append(
                "build manifest role_inventory contains an invalid occurrence count"
            )
            continue
        counts[role] = count
    return counts, errors


def _docx_audit_producer_errors(
    report: dict[str, Any],
    build: dict[str, Any],
    docx_path: Path,
    *,
    schema_v2: bool,
    profile: dict[str, Any] | None = None,
    ir: dict[str, Any] | None = None,
) -> list[str]:
    """Recompute the stable, work-mode DOCX audit evidence contract."""
    errors = _passed_checks_errors(
        report,
        label="DOCX audit",
        required_checks=(V2_DOCX_AUDIT_CHECKS if schema_v2 else V1_DOCX_AUDIT_CHECKS),
    )
    output_dir = docx_path.parent
    try:
        markdown_path = work_relative_artifact_path(
            output_dir,
            build.get("markdown"),
            label="build manifest Markdown path",
        )
        expected_docx = output_dir / f"{markdown_path.stem}.docx"
        if docx_path != expected_docx:
            errors.append("audited DOCX path does not match the current Markdown stem")
    except (ArtifactSafetyError, TypeError, ValueError) as exc:
        errors.append(f"build manifest Markdown role is invalid: {exc}")
    try:
        reported_docx = lexical_absolute_path(report.get("docx"))
        if os.path.normcase(os.fspath(reported_docx)) != os.path.normcase(
            os.fspath(docx_path)
        ):
            errors.append("DOCX audit path does not identify the audited DOCX")
    except (TypeError, ValueError, OSError):
        errors.append("DOCX audit path must be a valid absolute path")

    problem_ids = build.get("problem_ids")
    if not isinstance(problem_ids, list) or any(
        not isinstance(item, str) or not item for item in problem_ids
    ):
        errors.append("build manifest problem_ids must be an array of strings")
        problem_ids = []
    role_counts, role_errors = _role_counts_from_build(build)
    errors.extend(role_errors)
    external_uris = build.get("external_uris")
    if not isinstance(external_uris, list) or any(
        not isinstance(item, str) or not item for item in external_uris
    ):
        errors.append("build manifest external_uris must be an array of strings")
        external_uris = []
    expected_assets = build.get("assets")
    if not isinstance(expected_assets, list):
        errors.append("build manifest assets must be an array")
        expected_assets = []

    expected_counts = {
        "problem_count": len(problem_ids),
        "example_count": role_counts.get("example", 0),
        "low_resource_tip_count": role_counts.get("tip", 0),
        "external_link_count": len(external_uris),
    }
    for field, expected in expected_counts.items():
        value = report.get(field)
        if not _strict_nonnegative_int(value) or value != expected:
            errors.append(f"DOCX audit {field} does not match the current build")
    if report.get("problem_ids") != problem_ids:
        errors.append("DOCX audit problem_ids do not match the current build")
    if report.get("external_links") != sorted(external_uris):
        errors.append("DOCX audit external_links do not match the current build")
    if not _strict_nonnegative_int(report.get("image_count")) or report.get(
        "image_count"
    ) < len(expected_assets):
        errors.append("DOCX audit image_count does not cover current build assets")
    if not _strict_nonnegative_int(report.get("chinese_character_count")) or report.get(
        "chinese_character_count"
    ) <= 0:
        errors.append("passed DOCX audit must contain Chinese text evidence")

    if schema_v2:
        if not isinstance(profile, dict) or not isinstance(ir, dict):
            errors.append("V2 DOCX audit validation requires frozen Profile and IR")
            profile = {}
            ir = {}
        if report.get("role_counts") != role_counts:
            errors.append("V2 DOCX audit role_counts do not match the current build")
        occurrence_evidence = report.get("role_occurrence_evidence")
        if not isinstance(occurrence_evidence, dict) or set(
            occurrence_evidence
        ) != set(role_counts):
            errors.append("V2 DOCX audit role occurrence evidence is incomplete")
        else:
            for role, evidence in occurrence_evidence.items():
                if (
                    not isinstance(evidence, dict)
                    or not _strict_nonnegative_int(evidence.get("textual"))
                    or not _strict_nonnegative_int(evidence.get("visual"))
                    or evidence["textual"] + evidence["visual"] != role_counts[role]
                ):
                    errors.append(
                        "V2 DOCX audit role occurrence evidence is invalid"
                    )
                    break
        try:
            contract_roles = profile_contract(profile).get("roles", [])
        except (KeyError, TypeError, ValueError):
            contract_roles = []
        role_specs = {
            item["role"]: item
            for item in contract_roles
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        }
        semantic_groups = ir.get("semantic_groups")
        nodes = ir.get("nodes")
        if not isinstance(semantic_groups, list) or not isinstance(nodes, list):
            errors.append("V2 document IR semantic evidence is invalid")
            semantic_groups = []
            nodes = []
        expected_nested_keys = {
            "complete_container_checks": {
                group.get("id")
                for group in semantic_groups
                if isinstance(group, dict)
                and group.get("membership") == "complete"
                and role_specs.get(group.get("role"), {}).get("grouping")
                == "structural-container"
            },
            "scoped_anchor_callout_checks": {
                group.get("id")
                for group in semantic_groups
                if isinstance(group, dict)
                and group.get("membership") == "anchor-only"
                and role_specs.get(group.get("role"), {}).get("grouping")
                == "structural-container"
            },
            "non_structural_anchor_unboxed": {
                group.get("id")
                for group in semantic_groups
                if isinstance(group, dict)
                and group.get("membership") == "anchor-only"
                and role_specs.get(group.get("role"), {}).get("grouping")
                != "structural-container"
            },
        }
        for field, expected_keys in expected_nested_keys.items():
            value = report.get(field)
            if (
                not isinstance(value, dict)
                or set(value) != expected_keys
                or any(item is not True for item in value.values())
            ):
                errors.append(f"V2 DOCX audit {field} is invalid")
        source_only_counts = report.get("source_only_occurrence_counts")
        expected_source_only = {
            node.get("id"): 1
            for node in nodes
            if isinstance(node, dict)
            and node.get("semantic", {}).get("output") == "source-only"
            and node.get("source", {}).get("text")
        }
        if source_only_counts != expected_source_only:
            errors.append("V2 DOCX audit source-only occurrence evidence is invalid")
        native_count = report.get("native_table_count")
        expected_native_count = report.get("expected_native_table_count")
        frozen_native_count = sum(
            isinstance(node, dict)
            and node.get("type") == "table"
            and node.get("semantic", {}).get("output") == "source-only"
            and str(node.get("source", {}).get("text", ""))
            .lstrip()
            .lower()
            .startswith("<table")
            for node in nodes
        )
        if (
            not _strict_nonnegative_int(native_count)
            or not _strict_nonnegative_int(expected_native_count)
            or native_count != expected_native_count
            or expected_native_count != frozen_native_count
        ):
            errors.append("V2 DOCX audit native table evidence is invalid")
    else:
        if report.get("problem_range_count") != len(problem_ids):
            errors.append("V1 DOCX audit problem range count is stale")
    return errors


def _freeze_v1_expected_counts(args: Any, build: dict[str, Any]) -> None:
    """Make every V1 work audit count check mandatory and build-derived."""
    problem_ids = build.get("problem_ids")
    external_uris = build.get("external_uris")
    role_counts, role_errors = _role_counts_from_build(build)
    if role_errors:
        raise ValueError("; ".join(role_errors))
    if not isinstance(problem_ids, list) or any(
        not isinstance(item, str) or not item for item in problem_ids
    ):
        raise ValueError("build manifest problem_ids must be an array of strings")
    if not isinstance(external_uris, list) or any(
        not isinstance(item, str) or not item for item in external_uris
    ):
        raise ValueError("build manifest external_uris must be an array of strings")
    expected = {
        "expected_problems": len(problem_ids),
        "expected_examples": role_counts.get("example", 0),
        "expected_tips": role_counts.get("tip", 0),
        "expected_links": len(external_uris),
    }
    for attribute, count in expected.items():
        supplied = getattr(args, attribute)
        if supplied is not None and supplied != count:
            raise ValueError(
                f"--{attribute.removeprefix('expected_').replace('_', '-')} "
                "disagrees with the current build manifest"
            )
        setattr(args, attribute, count)


def _bounded_cli_path(boundary: Path, path: Path, *, label: str) -> Path:
    boundary = lexical_absolute_path(boundary)
    candidate = lexical_absolute_path(path)
    try:
        relative = os.path.relpath(candidate, boundary)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} is outside WORK output") from exc
    bounded = work_relative_artifact_path(
        boundary, Path(relative).as_posix(), label=label
    )
    if os.path.normcase(os.fspath(bounded)) != os.path.normcase(os.fspath(candidate)):
        raise ArtifactSafetyError(f"{label} is outside WORK output")
    return bounded


def _validate_build_assets(
    build: dict[str, Any], output_dir: Path, work_dir: Path
) -> None:
    assets = build.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("build manifest assets must be an array")
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            raise ValueError(f"build manifest asset {index} must be an object")
        path = work_relative_artifact_path(
            output_dir,
            item.get("path"),
            label=f"build manifest assets[{index}].path",
        )
        validate_artifact_file(path, boundary=work_dir)
        expected_hash = item.get("sha256")
        if not isinstance(expected_hash, str) or sha256_artifact(
            path, boundary=work_dir
        ) != expected_hash:
            raise ValueError(f"build manifest asset {index} hash does not match")


def _reject_docx_input_aliases(work_dir: Path, docx_path: Path) -> None:
    """Keep an audited DOCX distinct from every frozen or gate artifact."""
    output_dir = work_dir / "output"
    reserved = {
        work_dir / "profile.json",
        work_dir / "manifest.json",
        work_dir / "document-ir.json",
        work_dir / "blocks.jsonl",
        work_dir / "source-audit.json",
        output_dir / "build-manifest.json",
        output_dir / "output-audit.json",
        output_dir / "docx-audit.json",
        output_dir / "compile-audit.json",
        output_dir / "visual-review.json",
        output_dir / "qa-report.json",
    }
    build_path = output_dir / "build-manifest.json"
    if os.path.lexists(build_path):
        build = _read_work_json(build_path, work_dir, label="build manifest")
        for label in ("markdown", "latex"):
            value = build.get(label)
            if value is not None:
                reserved.add(
                    work_relative_artifact_path(
                        output_dir, value, label=f"build manifest {label} path"
                    )
                )
        assets = build.get("assets", [])
        if not isinstance(assets, list):
            raise ArtifactSafetyError("build manifest assets must be an array")
        for index, item in enumerate(assets):
            if not isinstance(item, dict):
                raise ArtifactSafetyError(
                    f"build manifest asset {index} must be an object"
                )
            reserved.add(
                work_relative_artifact_path(
                    output_dir,
                    item.get("path"),
                    label=f"build manifest asset {index} path",
                )
            )
    if docx_path in reserved:
        raise ArtifactSafetyError(
            "audited DOCX must not alias a frozen input or gate artifact"
        )


def _preflight_v2_paths(
    work_dir: Path,
    docx_path: Path,
    audit_path: Path | None,
) -> tuple[Path, Path, Path, Path | None]:
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
    validate_artifact_tree(
        output_dir / "docx-build", boundary=work_dir, allow_missing=True
    )
    docx_path = _bounded_cli_path(output_dir, docx_path, label="audited DOCX path")
    if docx_path.parent != output_dir:
        raise ArtifactSafetyError("audited DOCX must be a direct output child")
    validate_artifact_file(docx_path, boundary=work_dir)
    build = _read_work_json(
        output_dir / "build-manifest.json", work_dir, label="build manifest"
    )
    frozen_markdown = work_relative_artifact_path(
        output_dir,
        build.get("markdown"),
        label="build manifest Markdown path",
    )
    if docx_path != output_dir / f"{frozen_markdown.stem}.docx":
        raise ArtifactSafetyError(
            "audited DOCX must use the current build Markdown stem"
        )
    _reject_docx_input_aliases(work_dir, docx_path)
    default_audit = output_dir / "docx-audit.json"
    validate_artifact_file(default_audit, boundary=work_dir, allow_missing=True)
    if audit_path is not None:
        audit_path = _bounded_cli_path(
            output_dir, audit_path, label="DOCX audit output path"
        )
        if audit_path != default_audit:
            raise ArtifactSafetyError(
                "WORK-mode DOCX audit output must be output/docx-audit.json"
            )
        validate_artifact_file(audit_path, boundary=work_dir, allow_missing=True)
        if audit_path == docx_path:
            raise ArtifactSafetyError("DOCX input and audit output must be distinct")
    return work_dir, output_dir, docx_path, audit_path


def _preflight_v1_paths(
    work_dir: Path, docx_path: Path, audit_path: Path | None
) -> tuple[Path, Path, Path | None]:
    """Bound every V1 audit artifact to WORK before reading or publishing."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, boundary=work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
    docx_path = _bounded_cli_path(output_dir, docx_path, label="audited DOCX path")
    if docx_path.parent != output_dir:
        raise ArtifactSafetyError("audited DOCX must be a direct output child")
    validate_artifact_file(docx_path, boundary=work_dir)
    build = _read_work_json(
        output_dir / "build-manifest.json", work_dir, label="build manifest"
    )
    frozen_markdown = work_relative_artifact_path(
        output_dir,
        build.get("markdown"),
        label="build manifest Markdown path",
    )
    if docx_path != output_dir / f"{frozen_markdown.stem}.docx":
        raise ArtifactSafetyError(
            "audited DOCX must use the current build Markdown stem"
        )
    _reject_docx_input_aliases(work_dir, docx_path)
    if audit_path is not None:
        audit_path = _bounded_cli_path(output_dir, audit_path, label="DOCX audit output path")
        if audit_path != output_dir / "docx-audit.json":
            raise ArtifactSafetyError(
                "WORK-mode DOCX audit output must be output/docx-audit.json"
            )
        if audit_path == docx_path:
            raise ArtifactSafetyError("DOCX input and audit output must be distinct")
        validate_artifact_file(audit_path, boundary=work_dir, allow_missing=True)
    return work_dir, docx_path, audit_path


def _require_current_output_audit(work_dir: Path) -> None:
    """Invalidate downstream gates, then require the current output freeze."""
    output_dir = work_dir / "output"
    for path in (
        output_dir / "docx-audit.json",
        output_dir / "compile-audit.json",
        output_dir / "visual-review.json",
        output_dir / "qa-report.json",
    ):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    for path in (
        output_dir / "docx-audit.json",
        output_dir / "compile-audit.json",
        output_dir / "visual-review.json",
        output_dir / "qa-report.json",
    ):
        remove_artifact_file(path, boundary=work_dir)
    report, _build, errors = validate_output_audit_binding(
        work_dir, output_dir / "output-audit.json"
    )
    if report.get("status") != "passed":
        errors.insert(0, "output audit status is not passed")
    if errors:
        raise ValueError("DOCX audit rejects output freeze: " + "; ".join(errors))


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
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    output_dir = work_dir / "output"
    validate_artifact_directory(output_dir, boundary=work_dir)
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = output_dir / "build-manifest.json"
    for label, path in (
        ("Profile", profile_path),
        ("document IR", ir_path),
        ("build manifest", build_path),
    ):
        try:
            validate_artifact_file(path, boundary=work_dir)
        except ArtifactSafetyError as exc:
            raise ValueError(
                f"schema V2 DOCX audit has an invalid frozen {label}: {exc}"
            ) from exc
    profile = load_work_profile(work_dir, profile_reference)
    ir = _read_work_json(ir_path, work_dir, label="document IR")
    build = _read_work_json(build_path, work_dir, label="build manifest")
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 DOCX audit requires document IR schema_version 2")
    if build.get("profile_id") != profile["id"] or ir.get("profile", {}).get("id") != profile["id"]:
        raise ValueError("frozen Profile ids disagree")
    if build.get("profile_file_sha256") != sha256_artifact(
        profile_path, boundary=work_dir
    ):
        raise ValueError("build manifest does not bind the frozen Profile file")
    if build.get("document_ir_sha256") != sha256_artifact(
        ir_path, boundary=work_dir
    ):
        raise ValueError("build manifest does not bind the frozen document IR")
    if build.get("role_inventory") != ir.get("inventories", {}).get("role_inventory"):
        raise ValueError("build manifest role inventory does not match document IR")
    markdown_path = work_relative_artifact_path(
        output_dir,
        build.get("markdown"),
        label="build manifest markdown path",
    )
    validate_artifact_file(markdown_path, boundary=work_dir)
    if build.get("markdown_sha256") != sha256_artifact(
        markdown_path, boundary=work_dir
    ):
        raise ValueError("build manifest does not bind frozen Markdown bytes")
    _validate_build_assets(build, output_dir, work_dir)
    return profile, ir, build, ir_path, build_path


def write_report(
    report: dict[str, Any], output: Path | None, work_dir: Path | None = None
) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        if work_dir is None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        else:
            atomic_write_text(output, rendered, boundary=work_dir)
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
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
        docx_path = _bounded_cli_path(
            output_dir, docx_path, label="audited DOCX path"
        )
        if docx_path.parent != output_dir or docx_path.suffix.lower() != ".docx":
            raise ArtifactSafetyError(
                "audited DOCX must be a direct output DOCX child"
            )
        audit_path = _bounded_cli_path(
            output_dir,
            audit_path if audit_path is not None else output_dir / "docx-audit.json",
            label="DOCX audit path",
        )
    except ArtifactSafetyError as exc:
        return None, {}, [f"cannot read DOCX audit binding: {exc}"]
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = output_dir / "build-manifest.json"
    required = {
        "frozen Profile": profile_path,
        "document IR": ir_path,
        "build manifest": build_path,
        "DOCX": docx_path,
        "DOCX audit": audit_path,
    }
    missing: list[str] = []
    try:
        for label, path in required.items():
            validate_artifact_file(path, boundary=work_dir, allow_missing=True)
            if not os.path.lexists(path):
                missing.append(label)
    except ArtifactSafetyError as exc:
        return None, {}, [f"cannot read DOCX audit binding: {exc}"]
    if missing:
        return None, {}, [f"missing {label}" for label in missing]

    try:
        profile = _read_work_json(profile_path, work_dir, label="frozen Profile")
        ir = _read_work_json(ir_path, work_dir, label="document IR")
        build = _read_work_json(build_path, work_dir, label="build manifest")
        audit = _read_work_json(audit_path, work_dir, label="DOCX audit")
    except (ArtifactSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {}, [f"cannot read DOCX audit binding: {exc}"]
    expected = {
        "profile": str(profile.get("id", "")),
        "profile_file_sha256": sha256_artifact(profile_path, boundary=work_dir),
        "document_ir_sha256": sha256_artifact(ir_path, boundary=work_dir),
        "build_manifest_sha256": sha256_artifact(build_path, boundary=work_dir),
        "output_audit_sha256": sha256_artifact(
            output_dir / "output-audit.json", boundary=work_dir
        ),
        "docx_sha256": sha256_artifact(docx_path, boundary=work_dir),
    }
    errors: list[str] = []
    if profile.get("schema_version") != 2:
        errors.append("frozen Profile is not schema V2")
    if not isinstance(audit.get("status"), str):
        errors.append("DOCX audit status must be a string")
    if audit.get("status") != "passed":
        errors.append("DOCX audit status is not passed")
    if audit.get("status") == "passed" and audit.get("failures") != []:
        errors.append("passed DOCX audit failures must be an empty array")
    if audit.get("status") == "passed":
        errors.extend(
            _docx_audit_producer_errors(
                audit,
                build,
                docx_path,
                schema_v2=True,
                profile=profile,
                ir=ir,
            )
        )
    try:
        output_audit, _build, output_errors = validate_output_audit_binding(
            work_dir, output_dir / "output-audit.json"
        )
        if output_audit.get("status") != "passed":
            output_errors.insert(0, "output audit status is not passed")
        errors.extend(f"output freeze chain: {item}" for item in output_errors)
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        errors.append(f"cannot validate output freeze chain: {exc}")
    for field, value in expected.items():
        if audit.get(field) != value:
            errors.append(f"DOCX audit {field} does not match current bytes")
    return audit, expected, errors


def validate_docx_audit_binding(
    work_dir: Path,
    docx_path: Path,
    audit_path: Path | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str], list[str]]:
    """Validate a current V1 or V2 DOCX audit and its output freeze chain."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    profile_path = work_dir / "profile.json"
    build_path = output_dir / "build-manifest.json"
    output_audit_path = output_dir / "output-audit.json"
    audit_path = audit_path or output_dir / "docx-audit.json"
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
        docx_path = _bounded_cli_path(
            output_dir, docx_path, label="audited DOCX path"
        )
        audit_path = _bounded_cli_path(
            output_dir, audit_path, label="DOCX audit path"
        )
        for path in (
            profile_path,
            build_path,
            output_audit_path,
            docx_path,
            audit_path,
        ):
            validate_artifact_file(path, boundary=work_dir)
        profile = _read_work_json(profile_path, work_dir, label="frozen Profile")
        if profile.get("schema_version") == 2:
            return validate_v2_docx_audit_binding(
                work_dir, docx_path, audit_path
            )
        audit = _read_work_json(audit_path, work_dir, label="DOCX audit")
        output_audit, build, output_errors = validate_output_audit_binding(
            work_dir, output_audit_path
        )
    except (ArtifactSafetyError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {}, [f"cannot read DOCX audit binding: {exc}"]
    expected = {
        "profile": str(profile.get("id", "")),
        "profile_file_sha256": sha256_artifact(
            profile_path, boundary=work_dir
        ),
        "build_manifest_sha256": sha256_artifact(
            build_path, boundary=work_dir
        ),
        "output_audit_sha256": sha256_artifact(
            output_audit_path, boundary=work_dir
        ),
        "docx_sha256": sha256_artifact(docx_path, boundary=work_dir),
    }
    errors = list(output_errors)
    if output_audit.get("status") != "passed":
        errors.insert(0, "output audit status is not passed")
    if not isinstance(audit.get("status"), str):
        errors.append("DOCX audit status must be a string")
    if audit.get("status") != "passed":
        errors.append("DOCX audit status is not passed")
    if audit.get("status") == "passed" and audit.get("failures") != []:
        errors.append("passed DOCX audit failures must be an empty array")
    if audit.get("status") == "passed":
        errors.extend(
            _docx_audit_producer_errors(
                audit, build, docx_path, schema_v2=False
            )
        )
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
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    docx_name = compile_report.get("docx")
    if not isinstance(docx_name, str) or not docx_name:
        return {}, ["compile gate does not identify the audited DOCX"]
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
        docx_path = work_relative_artifact_path(
            output_dir, docx_name, label="compile gate DOCX path"
        )
        if docx_path.parent != output_dir or docx_path.suffix.lower() != ".docx":
            raise ArtifactSafetyError(
                "compile gate DOCX must be a direct output DOCX child"
            )
        audit_path = _bounded_cli_path(
            output_dir,
            audit_path if audit_path is not None else output_dir / "docx-audit.json",
            label="compile gate DOCX audit path",
        )
    except ArtifactSafetyError as exc:
        return {}, [f"compile gate DOCX path is unsafe: {exc}"]

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
    try:
        validate_artifact_file(audit_path, boundary=work_dir, allow_missing=True)
        if os.path.lexists(audit_path) and compile_report.get(
            "docx_audit_sha256"
        ) != sha256_artifact(audit_path, boundary=work_dir):
            errors.append("compile gate refers to different DOCX audit bytes")
    except ArtifactSafetyError as exc:
        errors.append(f"compile gate DOCX audit path is unsafe: {exc}")
    return expected, errors


def validate_compile_docx_binding(
    work_dir: Path,
    compile_report: dict[str, Any],
    audit_path: Path | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Validate any DOCX-derived compile report against its current audit."""
    work_dir = lexical_absolute_path(work_dir)
    output_dir = work_dir / "output"
    docx_name = compile_report.get("docx")
    if not isinstance(docx_name, str) or not docx_name:
        return {}, ["compile gate does not identify the audited DOCX"]
    compile_status = compile_report.get("automated_status")
    shape_errors: list[str] = []
    if not isinstance(compile_status, str):
        shape_errors.append("compile gate status must be a string")
    if compile_status != "passed":
        shape_errors.append("compile automated gate is not passed")
    if compile_report.get("status") != "passed":
        shape_errors.append("DOCX compile status is not passed")
    if compile_report.get("failures") != []:
        shape_errors.append("passed compile gate failures must be an empty array")
    if compile_status == "passed":
        shape_errors.extend(_passed_checks_errors(compile_report, label="compile gate"))
    try:
        _, compile_output_errors = validate_compile_output_binding(
            work_dir, compile_report
        )
        shape_errors.extend(
            f"compile output binding: {message}"
            for message in compile_output_errors
        )
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        shape_errors.append(f"compile output binding is invalid: {exc}")
    declared_docx_bindings = compile_report.get("docx_audit_bindings")
    expects_v2 = "document_ir_sha256" in compile_report or (
        isinstance(declared_docx_bindings, dict)
        and "document_ir_sha256" in declared_docx_bindings
    )
    if expects_v2:
        expected, errors = validate_v2_compile_docx_binding(
            work_dir, compile_report, audit_path
        )
        return expected, shape_errors + errors
    try:
        docx_path = work_relative_artifact_path(
            output_dir, docx_name, label="compile gate DOCX path"
        )
        if docx_path.parent != output_dir:
            raise ArtifactSafetyError(
                "compile gate DOCX must be a direct output child"
            )
        audit_path = audit_path or output_dir / "docx-audit.json"
        audit, expected, errors = validate_docx_audit_binding(
            work_dir, docx_path, audit_path
        )
    except (ArtifactSafetyError, OSError, ValueError) as exc:
        return {}, shape_errors + [f"compile gate DOCX path is unsafe: {exc}"]
    errors = shape_errors + errors
    for field in (
        "profile",
        "profile_file_sha256",
        "build_manifest_sha256",
        "output_audit_sha256",
        "docx_sha256",
        "document_ir_sha256",
    ):
        if field in expected and compile_report.get(field) != expected[field]:
            errors.append(f"compile gate {field} DOCX binding is stale")
    if compile_report.get("docx_audit_bindings") != expected:
        errors.append("compile gate DOCX audit bindings are stale")
    try:
        validate_artifact_file(audit_path, boundary=work_dir)
        if compile_report.get("docx_audit_sha256") != sha256_artifact(
            audit_path, boundary=work_dir
        ):
            errors.append("compile gate refers to different DOCX audit bytes")
    except ArtifactSafetyError as exc:
        errors.append(f"compile gate DOCX audit path is unsafe: {exc}")
    if audit is None:
        errors.append("compile gate DOCX audit is missing")
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
    mismatches = {
        role: {"expected": count, "frozen": frozen_role_counts[role]}
        for role, count in expected_flags.items()
        if frozen_role_counts[role] != count
    }
    if mismatches:
        raise SystemExit(
            f"role count assertions disagree with frozen IR: {mismatches}"
        )
    # A work-mode audit always evaluates the complete frozen role inventory;
    # command-line assertions may narrow diagnostics but may not narrow the gate.
    expected_flags = dict(frozen_role_counts)

    work_dir = lexical_absolute_path(args.work_dir)
    with zipfile.ZipFile(
        io.BytesIO(read_artifact_bytes(args.docx, boundary=work_dir))
    ) as archive:
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
    if args.expected_links is not None and args.expected_links != len(expected_links):
        raise SystemExit(
            "external link assertion disagrees with the current build manifest"
        )
    args.expected_links = len(expected_links)
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
        "profile_file_sha256": sha256_artifact(
            work_dir / "profile.json", boundary=work_dir
        ),
        "docx": str(args.docx),
        "docx_sha256": sha256_artifact(args.docx, boundary=work_dir),
        "document_ir_sha256": sha256_artifact(ir_path, boundary=work_dir),
        "build_manifest_sha256": sha256_artifact(
            build_path, boundary=work_dir
        ),
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
    if work_dir is not None:
        report.update(
            {
                "profile_file_sha256": sha256_artifact(
                    work_dir / "profile.json", boundary=work_dir
                ),
                "build_manifest_sha256": sha256_artifact(
                    work_dir / "output" / "build-manifest.json",
                    boundary=work_dir,
                ),
                "output_audit_sha256": sha256_artifact(
                    work_dir / "output" / "output-audit.json",
                    boundary=work_dir,
                ),
                "docx_sha256": sha256_artifact(
                    args.docx, boundary=work_dir
                ),
            }
        )
    write_report(report, args.output, work_dir)
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
    requested_work = lexical_absolute_path(args.work_dir) if args.work_dir else None
    try:
        if requested_work is not None:
            validate_artifact_directory(requested_work)
            profile_path = requested_work / "profile.json"
            validate_artifact_file(
                profile_path, boundary=requested_work, allow_missing=True
            )
            if os.path.lexists(profile_path):
                profile = load_work_profile(requested_work, args.profile)
            else:
                profile = load_profile(args.profile)
        else:
            profile = load_profile(args.profile)
    except (ArtifactSafetyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if profile.get("schema_version") == 2:
        if requested_work is None:
            raise SystemExit("schema V2 DOCX audit requires --work-dir")
        try:
            args.work_dir, _, args.docx, args.output = _preflight_v2_paths(
                requested_work, args.docx, args.output
            )
            _require_current_output_audit(args.work_dir)
        except (ArtifactSafetyError, ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        audit_v2(args, profile)
        return
    work_dir: Path | None = None
    if requested_work is not None:
        try:
            work_dir, args.docx, args.output = _preflight_v1_paths(
                requested_work, args.docx, args.output
            )
            frozen_build = _read_work_json(
                work_dir / "output" / "build-manifest.json",
                work_dir,
                label="build manifest",
            )
            _freeze_v1_expected_counts(args, frozen_build)
            _require_current_output_audit(work_dir)
        except (ArtifactSafetyError, ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
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

    docx_payload = (
        read_artifact_bytes(args.docx, boundary=work_dir)
        if work_dir is not None
        else args.docx.read_bytes()
    )
    with zipfile.ZipFile(io.BytesIO(docx_payload)) as archive:
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
        "docx": str(args.docx if work_dir is not None else args.docx.resolve()),
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
    if work_dir is not None:
        report.update(
            {
                "profile_file_sha256": sha256_artifact(
                    work_dir / "profile.json", boundary=work_dir
                ),
                "build_manifest_sha256": sha256_artifact(
                    work_dir / "output" / "build-manifest.json",
                    boundary=work_dir,
                ),
                "output_audit_sha256": sha256_artifact(
                    work_dir / "output" / "output-audit.json",
                    boundary=work_dir,
                ),
                "docx_sha256": sha256_artifact(args.docx, boundary=work_dir),
            }
        )
    write_report(report, args.output, work_dir)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except ArtifactSafetyError as exc:
        raise SystemExit(str(exc)) from exc
