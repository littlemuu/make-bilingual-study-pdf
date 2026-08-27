#!/usr/bin/env python3
"""Build the V2 editable DOCX from audited English-first bilingual Markdown."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from docx import Document

from docx_ast import transform
import docx_style
from audit_outputs import validate_output_audit_binding
from common import json_loads_strict
from html_table import validate_table_html
from profile import load_profile, load_work_profile, profile_contract
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_copy_file,
    atomic_publish_with_writer,
    atomic_write_text,
    lexical_absolute_path,
    prepare_artifact_directory,
    read_artifact_bytes,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _read_work_json(path: Path, work_dir: Path, *, label: str) -> dict[str, Any]:
    value = json_loads_strict(read_artifact_text(path, boundary=work_dir))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _bounded_cli_path(boundary: Path, path: Path, *, label: str) -> Path:
    """Return a lexical CLI path only when it is below the supplied boundary."""
    boundary = lexical_absolute_path(boundary)
    candidate = lexical_absolute_path(path)
    try:
        relative = os.path.relpath(candidate, boundary)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} is outside WORK output") from exc
    bounded = work_relative_artifact_path(
        boundary,
        Path(relative).as_posix(),
        label=label,
    )
    if os.path.normcase(os.fspath(bounded)) != os.path.normcase(os.fspath(candidate)):
        raise ArtifactSafetyError(f"{label} is outside WORK output")
    return bounded


def _path_is_within(path: Path, boundary: Path) -> bool:
    try:
        common = os.path.commonpath(
            (os.fspath(lexical_absolute_path(path)), os.fspath(lexical_absolute_path(boundary)))
        )
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(
        os.fspath(lexical_absolute_path(boundary))
    )


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


def _preflight_v2_output(
    work_dir: Path, output_docx: Path
) -> tuple[Path, Path, Path, Path]:
    """Validate the complete DOCX publication surface without mutating it."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    output_dir = work_dir / "output"
    build_dir = output_dir / "docx-build"
    audit_path = output_dir / "docx-audit.json"
    output_docx = _bounded_cli_path(
        output_dir, output_docx, label="output DOCX path"
    )
    if output_docx.parent != output_dir:
        raise ArtifactSafetyError("output DOCX must be a direct output child")
    validate_artifact_tree(output_dir, boundary=work_dir, allow_missing=False)
    validate_artifact_tree(build_dir, boundary=work_dir, allow_missing=True)
    validate_artifact_file(output_docx, boundary=work_dir, allow_missing=True)
    validate_artifact_file(audit_path, boundary=work_dir, allow_missing=True)
    return work_dir, output_dir, build_dir, output_docx


def _preflight_v1_paths(
    work_dir: Path,
    source: Path,
    output_docx: Path,
    build_dir: Path | None,
    resource_path: Path,
    reference_doc: Path | None,
) -> tuple[Path, Path, Path, Path | None, Path, Path | None]:
    """Validate every V1 build input and publication path inside WORK."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, boundary=work_dir, allow_missing=False)

    source = _bounded_cli_path(work_dir, source, label="input Markdown path")
    output_docx = _bounded_cli_path(
        work_dir, output_docx, label="output DOCX path"
    )
    resource_path = _bounded_cli_path(
        work_dir, resource_path, label="DOCX resource path"
    )
    validate_artifact_file(source, boundary=work_dir)
    validate_artifact_file(output_docx, boundary=work_dir, allow_missing=True)
    validate_artifact_directory(resource_path, boundary=work_dir)
    validate_artifact_tree(resource_path, boundary=work_dir, allow_missing=False)
    if build_dir is not None:
        build_dir = _bounded_cli_path(
            work_dir, build_dir, label="DOCX build directory"
        )
        validate_artifact_tree(build_dir, boundary=work_dir, allow_missing=True)
        canonical_build_dir = work_dir / "output" / "docx-build"
        if build_dir != canonical_build_dir:
            raise ArtifactSafetyError(
                "WORK-mode DOCX build directory must be output/docx-build"
            )
        if build_dir in source.parents or (
            reference_doc is not None and build_dir in reference_doc.parents
        ) or resource_path == build_dir or build_dir in resource_path.parents:
            raise ArtifactSafetyError(
                "DOCX build inputs must stay outside the staging directory"
            )

    if reference_doc is not None:
        reference_doc = _bounded_cli_path(
            work_dir, reference_doc, label="reference DOCX path"
        )
        validate_artifact_file(reference_doc, boundary=work_dir)
    return work_dir, source, output_docx, build_dir, resource_path, reference_doc


def _reject_build_output_aliases(
    work_dir: Path,
    source: Path,
    output_docx: Path,
    *,
    build_dir: Path | None,
    reference_doc: Path | None,
    explicit_profile_path: Path | None,
) -> None:
    """Prevent DOCX publication over frozen inputs and gate reports."""
    output_dir = work_dir / "output"
    reserved = {
        source,
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
    scratch_roles = set()
    if build_dir is not None:
        scratch_roles = {
            build_dir / "source.json",
            build_dir / "grouped.json",
            build_dir / "raw.docx",
            build_dir / "styled.docx",
            build_dir / "reference.docx",
        }
    protected_inputs = set(reserved)
    if reference_doc is not None:
        if reference_doc in reserved or reference_doc in scratch_roles:
            raise ArtifactSafetyError(
                "reference DOCX must not alias a frozen input, gate, or scratch artifact"
            )
        protected_inputs.add(reference_doc)
    canonical_profile = work_dir / "profile.json"
    if explicit_profile_path is not None and explicit_profile_path != canonical_profile:
        if explicit_profile_path in reserved or explicit_profile_path in scratch_roles:
            raise ArtifactSafetyError(
                "custom Profile must not alias a frozen input, gate, or scratch artifact"
            )
        protected_inputs.add(explicit_profile_path)
    if output_docx in protected_inputs or output_docx in scratch_roles:
        raise ArtifactSafetyError(
            "output DOCX must not alias a frozen input or gate artifact"
        )


def _require_current_output_audit(
    work_dir: Path,
    source: Path,
    output_docx: Path,
    resource_path: Path,
    reference_doc: Path | None,
) -> dict[str, Any]:
    """Require the exact frozen build roles before invalidating downstream gates."""
    output_dir = work_dir / "output"
    downstream = (
        output_dir / "docx-audit.json",
        output_dir / "compile-audit.json",
        output_dir / "visual-review.json",
        output_dir / "qa-report.json",
    )
    for path in downstream:
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    report, build, errors = validate_output_audit_binding(
        work_dir, output_dir / "output-audit.json"
    )
    if report.get("status") != "passed":
        errors.insert(0, "output audit status is not passed")
    if errors:
        for path in downstream:
            remove_artifact_file(path, boundary=work_dir)
        raise ValueError("DOCX build rejects output freeze: " + "; ".join(errors))

    frozen_markdown = work_relative_artifact_path(
        output_dir,
        build.get("markdown"),
        label="build manifest Markdown path",
    )
    validate_artifact_file(frozen_markdown, boundary=work_dir)
    if frozen_markdown != source:
        raise ValueError("input Markdown is not the current build-manifest Markdown")
    expected_markdown_hash = build.get("markdown_sha256")
    if not isinstance(expected_markdown_hash, str) or expected_markdown_hash != sha256_artifact(
        frozen_markdown, boundary=work_dir
    ):
        raise ValueError("build manifest Markdown hash is missing or stale")
    if resource_path != output_dir:
        raise ValueError("WORK DOCX resource path must be the canonical output directory")
    validate_artifact_tree(resource_path, boundary=work_dir, allow_missing=False)
    if reference_doc is not None:
        raise ValueError("--reference-doc is not supported in WORK gate mode")
    expected_docx = output_dir / f"{frozen_markdown.stem}.docx"
    if output_docx != expected_docx:
        raise ValueError("output DOCX name must match the frozen Markdown stem")

    for path in downstream:
        remove_artifact_file(path, boundary=work_dir)
    return build


def materialize_html_tables(ast: dict[str, Any], pandoc: str = "pandoc") -> dict[str, Any]:
    """Turn validated raw HTML tables into native Pandoc Table nodes."""

    blocks = ast.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("Pandoc AST blocks must be an array")
    materialized: list[dict[str, Any]] = []
    for block in blocks:
        if not (
            isinstance(block, dict)
            and block.get("t") == "RawBlock"
            and isinstance(block.get("c"), list)
            and len(block["c"]) == 2
            and block["c"][0] == "html"
            and isinstance(block["c"][1], str)
            and block["c"][1].lstrip().lower().startswith("<table")
        ):
            materialized.append(block)
            continue
        table_html = validate_table_html(block["c"][1])
        completed = subprocess.run(
            [pandoc, "--from", "html", "--to", "json"],
            input=table_html,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
        )
        parsed = json.loads(completed.stdout)
        parsed_blocks = parsed.get("blocks")
        if not isinstance(parsed_blocks, list) or not parsed_blocks or any(
            not isinstance(item, dict) or item.get("t") != "Table"
            for item in parsed_blocks
        ):
            raise ValueError("Pandoc did not materialize HTML as a native Table")
        materialized.extend(parsed_blocks)
    result = dict(ast)
    result["blocks"] = materialized
    return result


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


def load_v2_context(
    work_dir: Path, profile_reference: str | Path | None, source: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path]:
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    output_dir = work_dir / "output"
    validate_artifact_directory(output_dir, boundary=work_dir)
    required = {
        "profile": work_dir / "profile.json",
        "ir": work_dir / "document-ir.json",
        "build": output_dir / "build-manifest.json",
    }
    for label, path in required.items():
        try:
            validate_artifact_file(path, boundary=work_dir)
        except ArtifactSafetyError as exc:
            raise ValueError(
                f"schema V2 DOCX build has an invalid frozen {label}: {exc}"
            ) from exc
    profile = load_work_profile(work_dir, profile_reference)
    ir = _read_work_json(required["ir"], work_dir, label="document IR")
    build = _read_work_json(required["build"], work_dir, label="build manifest")
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 DOCX build requires document IR schema_version 2")
    if ir.get("profile", {}).get("id") != profile["id"]:
        raise ValueError("document IR Profile id does not match frozen Profile")
    if build.get("profile_id") != profile["id"]:
        raise ValueError("build manifest Profile id does not match frozen Profile")
    checks = {
        "profile_file_sha256": sha256_artifact(required["profile"], boundary=work_dir),
        "document_ir_sha256": sha256_artifact(required["ir"], boundary=work_dir),
    }
    for field, actual in checks.items():
        if build.get(field) != actual:
            raise ValueError(f"build manifest {field} does not match frozen artifact")
    markdown_name = build.get("markdown")
    frozen_markdown = work_relative_artifact_path(
        output_dir,
        markdown_name,
        label="build manifest markdown path",
    )
    validate_artifact_file(frozen_markdown, boundary=work_dir)
    if frozen_markdown != lexical_absolute_path(source):
        raise ValueError("input Markdown is not the frozen build-manifest Markdown")
    if build.get("markdown_sha256") != sha256_artifact(
        frozen_markdown, boundary=work_dir
    ):
        raise ValueError("frozen Markdown hash does not match build manifest")
    _validate_build_assets(build, output_dir, work_dir)
    ir_inventory = ir.get("inventories", {}).get("role_inventory")
    if not isinstance(ir_inventory, dict) or build.get("role_inventory") != ir_inventory:
        raise ValueError("build manifest role inventory does not match document IR")
    build_dir = output_dir / "docx-build"
    return profile, ir, build, build_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert audited English-first Simplified-Chinese Markdown to an A4 DOCX. "
            "Every Problem block is regrouped as complete English, a separator, then "
            "complete Chinese before styling."
        )
    )
    parser.add_argument("input_markdown", type=Path)
    parser.add_argument("output_docx", type=Path)
    parser.add_argument("--resource-path", type=Path)
    parser.add_argument("--reference-doc", type=Path)
    parser.add_argument("--profile", help="built-in profile id or path to profile JSON")
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument(
        "--expected-role",
        action="append",
        default=[],
        metavar="ROLE=COUNT",
        help="repeatable schema V2 assertion against the frozen IR role inventory",
    )
    parser.add_argument("--title")
    parser.add_argument("--header-label")
    parser.add_argument("--footer-label")
    parser.add_argument("--latin-font")
    parser.add_argument("--cjk-font")
    parser.add_argument("--code-font")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="optional schema V1 staging directory inside --work-dir",
    )
    args = parser.parse_args()

    source = lexical_absolute_path(args.input_markdown)
    output = lexical_absolute_path(args.output_docx)
    requested_work = lexical_absolute_path(args.work_dir) if args.work_dir else None
    explicit_profile_path = None
    if args.profile is not None:
        profile_candidate = lexical_absolute_path(args.profile)
        if os.path.lexists(profile_candidate):
            explicit_profile_path = profile_candidate
    try:
        if requested_work is not None:
            validate_artifact_directory(requested_work)
            profile_path = requested_work / "profile.json"
            validate_artifact_file(
                profile_path, boundary=requested_work, allow_missing=True
            )
            if os.path.lexists(profile_path):
                requested_profile = load_work_profile(requested_work, args.profile)
            else:
                requested_profile = load_profile(args.profile)
        else:
            requested_profile = load_profile(args.profile)
        expected_role_flags = parse_expected_roles(args.expected_role)
    except (ArtifactSafetyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    profile = requested_profile
    if profile.get("schema_version") == 1 and expected_role_flags:
        raise SystemExit("--expected-role is available only for schema V2 Profiles")
    defaults = profile["render"]["docx"]
    args.title = args.title or defaults["title"]
    args.header_label = args.header_label or defaults["header_label"]
    args.footer_label = args.footer_label or defaults["footer_label"]
    args.latin_font = args.latin_font or defaults["latin_font"]
    args.cjk_font = args.cjk_font or defaults["cjk_font"]
    args.code_font = args.code_font or defaults["code_font"]
    temp_context = None
    ir: dict[str, Any] | None = None
    build_manifest: dict[str, Any] | None = None
    resource_path = lexical_absolute_path(args.resource_path or source.parent)
    reference_doc = (
        lexical_absolute_path(args.reference_doc) if args.reference_doc else None
    )
    if requested_profile.get("schema_version") == 2:
        if requested_work is None:
            raise SystemExit("schema V2 DOCX build requires --work-dir")
        if args.build_dir is not None:
            raise SystemExit("--build-dir is available only for schema V1 Profiles")
        try:
            requested_work, _, build_dir, output = _preflight_v2_output(
                requested_work, output
            )
            profile, ir, build_manifest, work = load_v2_context(
                requested_work, args.profile, source
            )
        except (ArtifactSafetyError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        assert work == build_dir
    elif requested_work is not None:
        try:
            (
                requested_work,
                source,
                output,
                work,
                resource_path,
                reference_doc,
            ) = _preflight_v1_paths(
                requested_work,
                source,
                output,
                lexical_absolute_path(args.build_dir) if args.build_dir else None,
                resource_path,
                reference_doc,
            )
        except ArtifactSafetyError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if not source.is_file():
            raise SystemExit(f"input Markdown not found: {source}")
        temp_context = tempfile.TemporaryDirectory(prefix="bilingual-docx-")
        work = Path(temp_context.name)
    if requested_work is not None:
        try:
            _reject_build_output_aliases(
                requested_work,
                source,
                output,
                build_dir=work,
                reference_doc=reference_doc,
                explicit_profile_path=explicit_profile_path,
            )
            frozen_build = _require_current_output_audit(
                requested_work,
                source,
                output,
                resource_path,
                reference_doc,
            )
            if build_manifest is None:
                build_manifest = frozen_build
        except (ArtifactSafetyError, ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        if work is None:
            prepare_artifact_directory(output.parent, boundary=requested_work)
            temp_context = tempfile.TemporaryDirectory(
                prefix=".docx-build-", dir=output.parent
            )
            work = lexical_absolute_path(temp_context.name)
            validate_artifact_directory(work, boundary=requested_work)
    if shutil.which("pandoc") is None:
        raise SystemExit("pandoc is required to build the DOCX")

    if requested_profile.get("schema_version") == 2:
        prepare_artifact_directory(work, boundary=requested_work)
    elif requested_work is not None:
        prepare_artifact_directory(work, boundary=requested_work)
        prepare_artifact_directory(output.parent, boundary=requested_work)
    artifact_boundary = requested_work if requested_work is not None else work
    ast_path = work / "source.json"
    grouped_path = work / "grouped.json"
    raw_docx = work / "raw.docx"
    styled_docx = work / "styled.docx"

    atomic_publish_with_writer(
        ast_path,
        lambda staged: run(
            [
                "pandoc",
                str(source),
                "--from",
                "markdown",
                "--to",
                "json",
                "--output",
                str(staged),
            ]
        ),
        boundary=artifact_boundary,
    )
    ast = json_loads_strict(read_artifact_text(ast_path, boundary=artifact_boundary))
    if not isinstance(ast, dict):
        raise SystemExit("Pandoc AST must be a JSON object")
    try:
        ast = materialize_html_tables(ast)
        grouped = transform(
            ast,
            profile,
            semantic_groups=(ir or {}).get("semantic_groups") if ir is not None else None,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    problem_count = int(grouped.get("meta", {}).get("v2-problem-group-count", {}).get("c", "0"))
    if profile["schema_version"] == 1 and args.expected_problems is not None and problem_count != args.expected_problems:
        raise SystemExit(
            f"Problem grouping count {problem_count} does not match expected {args.expected_problems}"
        )
    role_counts: dict[str, int] = {}
    complete_role_counts: dict[str, int] = {}
    anchor_only_role_counts: dict[str, int] = {}
    if profile["schema_version"] == 2:
        inventory = ir["inventories"]["role_inventory"]
        role_counts = {role: item["occurrence_count"] for role, item in inventory.items()}
        structural_roles = {
            item["role"]
            for item in profile_contract(profile)["roles"]
            if item["grouping"] == "structural-container"
        }
        complete_role_counts = {
            role: item["membership_counts"].get("complete", 0)
            for role, item in inventory.items()
            if role in structural_roles
        }
        anchor_only_role_counts = {
            role: item["membership_counts"].get("anchor-only", 0)
            for role, item in inventory.items()
            if role in structural_roles
        }
        unknown = sorted(set(expected_role_flags) - set(role_counts))
        if unknown:
            raise SystemExit(f"--expected-role names unknown Profile roles: {unknown}")
        mismatches = {
            role: {"expected": count, "frozen": role_counts[role]}
            for role, count in expected_role_flags.items()
            if role_counts[role] != count
        }
        if mismatches:
            raise SystemExit(f"role count assertions disagree with frozen IR: {mismatches}")
        if args.expected_problems is not None and role_counts.get("problem", 0) != args.expected_problems:
            raise SystemExit(
                "Problem count alias disagrees with frozen IR: "
                f"{args.expected_problems} != {role_counts.get('problem', 0)}"
            )
    atomic_write_text(
        grouped_path,
        json.dumps(grouped, ensure_ascii=False, allow_nan=False) + "\n",
        boundary=artifact_boundary,
    )

    if profile["schema_version"] == 2:
        validate_artifact_tree(
            resource_path, boundary=requested_work, allow_missing=False
        )
    command = [
        "pandoc",
        str(grouped_path),
        "--from",
        "json",
        "--to",
        "docx",
        "--resource-path",
        str(resource_path),
    ]
    if reference_doc is not None:
        if profile["schema_version"] == 2:
            reference_boundary = requested_work
            validate_artifact_file(reference_doc, boundary=reference_boundary)
        elif requested_work is not None:
            reference_boundary = requested_work
        else:
            reference_boundary = None
        if requested_work is not None:
            staged_reference = work / "reference.docx"
            atomic_copy_file(
                reference_doc,
                staged_reference,
                boundary=artifact_boundary,
                source_boundary=reference_boundary,
            )
            reference_doc = staged_reference
        command.extend(["--reference-doc", str(reference_doc)])
    atomic_publish_with_writer(
        raw_docx,
        lambda staged: run(command + ["--output", str(staged)]),
        boundary=artifact_boundary,
    )

    docx_style.LATIN_FONT = args.latin_font
    docx_style.CJK_FONT = args.cjk_font
    docx_style.CODE_FONT = args.code_font
    docx_style.configure_profile(profile)
    document = Document(
        io.BytesIO(read_artifact_bytes(raw_docx, boundary=artifact_boundary))
    )
    style_report = docx_style.apply_styles(
        document,
        document_title=args.title,
        header_label=args.header_label,
        footer_label=args.footer_label,
    )
    atomic_publish_with_writer(
        styled_docx,
        document.save,
        boundary=artifact_boundary,
    )

    with zipfile.ZipFile(
        io.BytesIO(read_artifact_bytes(styled_docx, boundary=artifact_boundary))
    ) as archive:
        document_xml = archive.read("word/document.xml")
    marker_count = document_xml.count(b"V2-PROBLEM-CALLOUT")
    generic_marker_count = document_xml.count(b"V23-CALLOUT")
    if marker_count:
        raise SystemExit(f"internal Problem markers remain in DOCX: {marker_count}")
    if generic_marker_count:
        raise SystemExit(f"internal generic callout markers remain in DOCX: {generic_marker_count}")
    if profile["schema_version"] == 1 and style_report["problem_callouts"] != problem_count:
        raise SystemExit(
            "styled Problem callout count does not match transformed Problem count: "
            f"{style_report['problem_callouts']} != {problem_count}"
        )
    if profile["schema_version"] == 1 and (
        style_report["problem_numbering_origins_explicit"]
        != style_report["problem_numbered_paragraphs"]
    ):
        raise SystemExit(
            "numbered Problem paragraphs do not all have an explicit stable border origin: "
            f"{style_report['problem_numbering_origins_explicit']} != "
            f"{style_report['problem_numbered_paragraphs']}"
        )
    if profile["schema_version"] == 1 and style_report["problem_legacy_horizontal_rules"]:
        raise SystemExit(
            "legacy VML horizontal rules remain in Problem callouts: "
            f"{style_report['problem_legacy_horizontal_rules']}"
        )

    if profile["schema_version"] == 2:
        styled = style_report.get("role_callouts", {})
        styled_complete = style_report.get("complete_role_callouts", {})
        styled_anchor = style_report.get("anchor_only_role_callouts", {})
        complete_mismatches = {
            role: {"expected": expected, "styled": styled_complete.get(role, 0)}
            for role, expected in complete_role_counts.items()
            if styled_complete.get(role, 0) != expected
        }
        anchor_mismatches = {
            role: {"expected": expected, "styled": styled_anchor.get(role, 0)}
            for role, expected in anchor_only_role_counts.items()
            if styled_anchor.get(role, 0) != expected
        }
        total_mismatches = {
            role: {
                "expected": complete_role_counts[role] + anchor_only_role_counts[role],
                "styled": styled.get(role, 0),
            }
            for role in complete_role_counts
            if styled.get(role, 0)
            != complete_role_counts[role] + anchor_only_role_counts[role]
        }
        if complete_mismatches or anchor_mismatches or total_mismatches:
            raise SystemExit(
                "styled structural callout counts changed: "
                f"complete={complete_mismatches}, anchor-only={anchor_mismatches}, "
                f"total={total_mismatches}"
            )

    if profile["schema_version"] == 2:
        audit_path = requested_work / "output" / "docx-audit.json"
        remove_artifact_file(audit_path, boundary=requested_work, missing_ok=True)
        atomic_copy_file(
            styled_docx,
            output,
            boundary=requested_work,
            source_boundary=requested_work,
        )
    elif requested_work is not None:
        atomic_copy_file(
            styled_docx,
            output,
            boundary=requested_work,
            source_boundary=requested_work,
        )
    else:
        prepare_artifact_directory(output.parent)
        atomic_copy_file(
            styled_docx,
            output,
            boundary=output.parent,
            source_boundary=work,
        )

    report = {
        "status": "passed",
        "profile": profile["id"],
        "output": str(output),
        "problem_groups": problem_count,
        **style_report,
        "internal_problem_markers": marker_count,
        "internal_generic_markers": generic_marker_count,
    }
    if profile["schema_version"] == 2:
        report.update(
            {
                "role_counts": role_counts,
                "complete_structural_role_counts": complete_role_counts,
                "anchor_only_structural_role_counts": anchor_only_role_counts,
                "document_ir_sha256": build_manifest["document_ir_sha256"],
                "build_manifest_sha256": sha256_artifact(
                    requested_work / "output" / "build-manifest.json",
                    boundary=requested_work,
                ),
            }
        )
    print(json.dumps(report, ensure_ascii=False))
    if temp_context is not None:
        temp_context.cleanup()


if __name__ == "__main__":
    try:
        main()
    except ArtifactSafetyError as exc:
        raise SystemExit(str(exc)) from exc
