#!/usr/bin/env python3
"""Render a V2 DOCX to PDF, render every page, and run automated PDF checks."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import fitz

from extract_pdf import invalid_pngs, repair_truncated_renders
from common import json_loads_strict
from audit_docx import (
    validate_docx_audit_binding,
    validate_v2_docx_audit_binding,
)
from audit_outputs import validate_output_audit_binding
from profile import load_work_profile, profile_contract, target_text_pattern
from visual_utils import make_contact_sheets
from safe_artifacts import (
    ArtifactSafetyError,
    artifact_size,
    atomic_copy_file,
    atomic_write_text,
    clear_artifact_directory,
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


CJK_RE = re.compile(r"[\u3400-\u9fff]")
PROBLEM_RE = re.compile(r"Problem \(([^)]+)\)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    if not os.path.lexists(path):
        return False
    validate_artifact_file(path, boundary=work_dir)
    return True


def _work_cli_path(value: Path, work_dir: Path, *, label: str) -> Path:
    candidate = lexical_absolute_path(value)
    try:
        candidate.relative_to(work_dir)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} must stay inside WORK") from exc
    return candidate


def _reject_compile_aliases(
    work_dir: Path,
    source: Path,
    target: Path,
    build: dict[str, Any] | None = None,
) -> None:
    """Keep compile inputs/outputs distinct from frozen metadata and gates."""
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
    if build is not None:
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
    if source in reserved:
        raise ArtifactSafetyError(
            "DOCX compile input must not alias a frozen input or gate artifact"
        )
    if target in reserved or target == source:
        raise ArtifactSafetyError(
            "compiled PDF must not alias its DOCX input or a frozen gate artifact"
        )


def _atomic_json(path: Path, value: object, work_dir: Path) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )


def _fail_work_binding(
    work_dir: Path,
    audit_output: Path,
    *,
    stage: str,
    message: str,
    cause: BaseException,
    details: dict[str, str] | None = None,
) -> None:
    """Invalidate downstream approval reports and publish a failed compile gate."""
    output_dir = work_dir / "output"
    remove_artifact_file(output_dir / "visual-review.json", boundary=work_dir)
    remove_artifact_file(output_dir / "qa-report.json", boundary=work_dir)
    report = {
        "status": "failed",
        "automated_status": "failed",
        "stage": stage,
        "failures": [f"{message}: {cause}"],
        "warnings": [],
    }
    if details:
        report.update(details)
    _atomic_json(audit_output, report, work_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1) from cause


def _publish_flat_directory(source: Path, target: Path, work_dir: Path) -> None:
    validate_artifact_tree(source, work_dir, allow_missing=False)
    prepare_artifact_directory(target, boundary=work_dir)
    with os.scandir(source) as iterator:
        paths = sorted((Path(entry.path) for entry in iterator), key=lambda item: item.name)
    for path in paths:
        validate_artifact_file(path, boundary=work_dir)
        atomic_copy_file(
            path,
            target / path.name,
            boundary=work_dir,
            source_boundary=work_dir,
        )


def normalized_font_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def font_stem_family(path: str) -> str:
    stem = normalized_font_name(Path(path).stem)
    return re.sub(
        r"(?:thin|extralight|light|regular|medium|semibold|demibold|bold|extrabold|black|italic|oblique)+$",
        "",
        stem,
    )


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


def load_v2_context(work_dir: Path, docx_path: Path) -> dict[str, Any]:
    work_dir = lexical_absolute_path(work_dir)
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = work_dir / "output" / "build-manifest.json"
    missing = [
        str(path)
        for path in (profile_path, ir_path, build_path)
        if not _artifact_exists(path, work_dir)
    ]
    if missing:
        raise ValueError(f"schema V2 PDF compile is missing frozen artifacts: {missing}")
    profile = load_work_profile(work_dir)
    if profile.get("schema_version") != 2:
        build = json_loads_strict(read_artifact_text(build_path, boundary=work_dir))
        if not isinstance(build, dict):
            raise ValueError("build manifest must be a JSON object")
        docx_audit_path = work_dir / "output" / "docx-audit.json"
        docx_audit, docx_audit_bindings, binding_errors = (
            validate_docx_audit_binding(work_dir, docx_path, docx_audit_path)
        )
        if binding_errors:
            raise ValueError(
                "schema V1 PDF compile rejects DOCX audit: "
                + "; ".join(binding_errors)
            )
        return {
            "schema_version": 1,
            "profile": profile,
            "build": build,
            "docx_audit": docx_audit,
            "docx_audit_path": docx_audit_path,
            "docx_audit_bindings": docx_audit_bindings,
        }
    ir = json_loads_strict(read_artifact_text(ir_path, boundary=work_dir))
    build = json_loads_strict(read_artifact_text(build_path, boundary=work_dir))
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 PDF compile requires document IR schema_version 2")
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
    docx_audit_path = work_dir / "output" / "docx-audit.json"
    docx_audit, docx_audit_bindings, binding_errors = validate_v2_docx_audit_binding(
        work_dir, docx_path, docx_audit_path
    )
    if binding_errors:
        raise ValueError("schema V2 PDF compile rejects DOCX audit: " + "; ".join(binding_errors))
    return {
        "schema_version": 2,
        "work_dir": work_dir,
        "profile": profile,
        "ir": ir,
        "build": build,
        "ir_path": ir_path,
        "build_path": build_path,
        "docx_audit": docx_audit,
        "docx_audit_path": docx_audit_path,
        "docx_audit_bindings": docx_audit_bindings,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--expected-role", action="append", default=[], metavar="ROLE=COUNT")
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--cjk-font", default="Noto Sans S Chinese")
    args = parser.parse_args()

    context: dict[str, Any] | None = None
    work_dir: Path | None = None
    compile_output_bindings: dict[str, str] = {}
    if args.work_dir is not None:
        try:
            work_dir = lexical_absolute_path(args.work_dir)
            validate_artifact_directory(work_dir)
            validate_artifact_tree(work_dir, work_dir, allow_missing=False)
            output_dir = work_dir / "output"
            validate_artifact_tree(output_dir, work_dir, allow_missing=False)
            source = _work_cli_path(args.docx, work_dir, label="DOCX path")
            target = _work_cli_path(args.pdf, work_dir, label="PDF path")
            render_dir = _work_cli_path(
                args.render_dir, work_dir, label="render directory"
            )
            audit_output = _work_cli_path(
                args.audit_output, work_dir, label="compile audit path"
            )
            if source.parent != output_dir or target.parent != output_dir:
                raise ArtifactSafetyError(
                    "WORK-mode DOCX and PDF must be direct output children"
                )
            if source.suffix.lower() != ".docx" or target != source.with_suffix(
                ".pdf"
            ):
                raise ArtifactSafetyError(
                    "WORK-mode PDF must use the compiled DOCX stem in output"
                )
            if render_dir != output_dir / "pdf-renders":
                raise ArtifactSafetyError(
                    "WORK-mode render directory must be output/pdf-renders"
                )
            if audit_output != output_dir / "compile-audit.json":
                raise ArtifactSafetyError(
                    "WORK-mode compile audit must be output/compile-audit.json"
                )
            _reject_compile_aliases(work_dir, source, target)
            validate_artifact_file(source, boundary=work_dir)
            for path in (
                target,
                audit_output,
                output_dir / "visual-review.json",
                output_dir / "qa-report.json",
            ):
                validate_artifact_file(path, boundary=work_dir, allow_missing=True)
            for directory in (
                output_dir / "build",
                render_dir,
                audit_output.parent / "contact",
            ):
                validate_artifact_tree(directory, work_dir, allow_missing=True)
            try:
                output_audit_path = output_dir / "output-audit.json"
                output_audit, frozen_build, binding_errors = (
                    validate_output_audit_binding(
                        work_dir, output_audit_path
                    )
                )
                if output_audit.get("status") != "passed":
                    binding_errors.insert(0, "output audit is not passed")
                if binding_errors:
                    raise ValueError("; ".join(binding_errors))
                compile_output_bindings = {
                    "build_manifest_sha256": sha256_artifact(
                        output_dir / "build-manifest.json", boundary=work_dir
                    ),
                    "output_audit_sha256": sha256_artifact(
                        output_audit_path, boundary=work_dir
                    ),
                }
            except (
                ArtifactSafetyError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
            ) as exc:
                # The complete WORK/output tree and every fixed mutation target
                # were checked above. Invalidate only reports; preserve the
                # DOCX and existing PDF bytes.
                _fail_work_binding(
                    work_dir,
                    audit_output,
                    stage="output-audit",
                    message="output audit binding is missing, invalid, or stale",
                    cause=exc,
                )
            _reject_compile_aliases(work_dir, source, target, frozen_build)
            try:
                context = load_v2_context(work_dir, source)
            except (
                ArtifactSafetyError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
            ) as exc:
                _fail_work_binding(
                    work_dir,
                    audit_output,
                    stage="docx-audit",
                    message="V2 DOCX freeze binding is missing, invalid, or stale",
                    cause=exc,
                    details=compile_output_bindings,
                )
            expected_role_flags = parse_expected_roles(args.expected_role)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if args.expected_role:
            raise SystemExit("--expected-role requires --work-dir")
        expected_role_flags = {}
        source = args.docx.resolve()
        target = args.pdf.resolve()
        render_dir = args.render_dir.resolve()
        audit_output = args.audit_output.resolve()
    if context and context["schema_version"] == 1 and expected_role_flags:
        raise SystemExit("--expected-role is available only for schema V2 Profiles")
    if context and context["schema_version"] == 1:
        frozen_problem_ids = context["build"].get("problem_ids")
        if not isinstance(frozen_problem_ids, list) or any(
            not isinstance(item, str) or not item for item in frozen_problem_ids
        ):
            raise SystemExit(
                "build manifest problem_ids must be an array of strings"
            )
        frozen_problem_count = len(frozen_problem_ids)
        if (
            args.expected_problems is not None
            and args.expected_problems != frozen_problem_count
        ):
            raise SystemExit(
                "--expected-problems disagrees with the current build manifest"
            )
        args.expected_problems = frozen_problem_count

    office = shutil.which("libreoffice") or shutil.which("soffice")
    if not office:
        raise SystemExit("LibreOffice is required to render the editable DOCX")
    commands = {
        command: shutil.which(command)
        for command in ("pdftoppm", "pdffonts", "fc-match")
    }
    for command, resolved in commands.items():
        if resolved is None:
            raise SystemExit(f"{command} is required for PDF QA")

    font_match = subprocess.run(
        ["fc-match", "--format", "%{family}\n%{file}\n", args.cjk_font],
        check=True,
        capture_output=True,
        text=True,
    )
    font_lines = font_match.stdout.splitlines()
    resolved_cjk_family = font_lines[0].strip() if font_lines else ""
    resolved_cjk_file = font_lines[1].strip() if len(font_lines) > 1 else ""
    requested_name = normalized_font_name(args.cjk_font)
    resolved_name = normalized_font_name(resolved_cjk_family)
    exact_cjk_font = bool(
        requested_name
        and resolved_name
        and (requested_name in resolved_name or resolved_name in requested_name)
    )
    if not exact_cjk_font or not resolved_cjk_file:
        report = {
            "status": "failed",
            "automated_status": "failed",
            "requested_cjk_font": args.cjk_font,
            "resolved_cjk_family": resolved_cjk_family,
            "resolved_cjk_file": resolved_cjk_file,
            "checks": {"requested_cjk_font_resolved_exactly": False},
            "failures": ["requested_cjk_font_resolved_exactly"],
            "remediation": (
                "Install/configure the requested CJK font or set FONTCONFIG_FILE to a "
                "project fonts.conf, then rerun. Font fallback is not accepted."
            ),
        }
        if work_dir is not None:
            report.update(compile_output_bindings)
            remove_artifact_file(
                work_dir / "output" / "visual-review.json", boundary=work_dir
            )
            remove_artifact_file(
                work_dir / "output" / "qa-report.json", boundary=work_dir
            )
            _atomic_json(audit_output, report, work_dir)
        else:
            audit_output.parent.mkdir(parents=True, exist_ok=True)
            audit_output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(1)

    if work_dir is None:
        target.parent.mkdir(parents=True, exist_ok=True)
    temporary_parent = work_dir / "output" if work_dir is not None else None
    with tempfile.TemporaryDirectory(
        prefix=".docx-pdf-stage-", dir=temporary_parent
    ) as temporary:
        temp = Path(temporary)
        profile = temp / "profile"
        profile.mkdir()
        config = profile / "xdg_config"
        cache = profile / "xdg_cache"
        config.mkdir()
        cache.mkdir()
        environment = os.environ.copy()
        environment["HOME"] = str(profile)
        environment["XDG_CONFIG_HOME"] = str(config)
        environment["XDG_CACHE_HOME"] = str(cache)
        profile_argument = f"-env:UserInstallation={profile.resolve().as_uri()}"
        conversion = subprocess.run(
            [
                office,
                profile_argument,
                "--invisible",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(temp),
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        generated = temp / f"{source.stem}.pdf"
        generated_exists = (
            _artifact_exists(generated, work_dir)
            if work_dir is not None
            else generated.is_file()
        )
        if not generated_exists:
            raise SystemExit(
                "LibreOffice did not produce the expected PDF\n"
                f"exit={conversion.returncode}\nstdout={conversion.stdout}\nstderr={conversion.stderr}"
            )
        if work_dir is not None:
            for path in (
                audit_output,
                work_dir / "output" / "visual-review.json",
                work_dir / "output" / "qa-report.json",
            ):
                remove_artifact_file(path, boundary=work_dir)
            atomic_copy_file(
                generated,
                target,
                boundary=work_dir,
                source_boundary=work_dir,
            )
        else:
            shutil.copy2(generated, target)

    stage_root: Path | None = None
    if work_dir is not None:
        stage_root = Path(
            tempfile.mkdtemp(prefix=".docx-render-stage-", dir=work_dir / "output")
        )
        active_render_dir = prepare_artifact_directory(
            stage_root / "pdf-renders", boundary=work_dir
        )
        active_contact_dir = prepare_artifact_directory(
            stage_root / "contact", boundary=work_dir
        )
    else:
        render_dir.mkdir(parents=True, exist_ok=True)
        for old in render_dir.glob("page-*.png"):
            old.unlink()
        active_render_dir = render_dir
        active_contact_dir = audit_output.parent / "contact"
    subprocess.run(
        [
            commands["pdftoppm"],
            "-png",
            "-r",
            str(args.dpi),
            str(target),
            str(active_render_dir / "page"),
        ],
        check=True,
    )
    if work_dir is not None:
        validate_artifact_tree(active_render_dir, work_dir, allow_missing=False)
    renders = sorted(active_render_dir.glob("page-*.png"))
    if work_dir is not None:
        for render in renders:
            validate_artifact_file(render, boundary=work_dir)
    repair_truncated_renders(commands["pdftoppm"], target, renders, args.dpi)
    invalid_renders = invalid_pngs(renders)
    contact_sheets = (
        make_contact_sheets(renders, active_contact_dir) if renders else []
    )
    if work_dir is not None:
        validate_artifact_tree(active_render_dir, work_dir, allow_missing=False)
        validate_artifact_tree(active_contact_dir, work_dir, allow_missing=False)

    document = fitz.open(target)
    page_sizes = []
    text_lengths = []
    nonwhite_fractions = []
    full_text = []
    pdf_image_count = 0
    for page in document:
        text = page.get_text()
        full_text.append(text)
        page_sizes.append([round(page.rect.width, 2), round(page.rect.height, 2)])
        text_lengths.append(len(re.sub(r"\s+", "", text)))
        pdf_image_count += len(page.get_images(full=True))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY, alpha=False)
        nonwhite_fractions.append(
            round(sum(value < 248 for value in pixmap.samples) / len(pixmap.samples), 6)
        )
    document.close()
    joined = "\n".join(full_text)
    problem_ids = PROBLEM_RE.findall(joined)
    role_counts: dict[str, int] | None = None
    occurrence_presence: dict[str, bool] = {}
    role_presence_counts: dict[str, int] = {}
    if context and context["schema_version"] == 2:
        profile = context["profile"]
        ir = context["ir"]
        contract = profile_contract(profile)
        role_specs = {item["role"]: item for item in contract["roles"]}
        inventory = ir["inventories"]["role_inventory"]
        role_counts = {
            role: item["occurrence_count"] for role, item in inventory.items()
        }
        unknown = sorted(set(expected_role_flags) - set(role_counts))
        if unknown:
            raise SystemExit(f"--expected-role names unknown Profile roles: {unknown}")
        mismatches = {
            role: {"expected": expected, "frozen": role_counts[role]}
            for role, expected in expected_role_flags.items()
            if role_counts[role] != expected
        }
        if mismatches:
            raise SystemExit(f"role count assertions disagree with frozen IR: {mismatches}")
        if args.expected_problems is not None and role_counts.get("problem", 0) != args.expected_problems:
            raise SystemExit(
                "Problem count alias disagrees with frozen IR: "
                f"{args.expected_problems} != {role_counts.get('problem', 0)}"
            )
        normalized_pdf = normalized_text(joined)
        nodes = {node["id"]: node for node in ir.get("nodes", [])}
        role_presence_counts = {role: 0 for role in role_counts}
        for group in ir.get("semantic_groups", []):
            role = group["role"]
            anchor = nodes[group["anchor_node_id"]]
            output = anchor.get("semantic", {}).get("output", role_specs[role]["output"])
            if output in {"visual-once", "artifact-omitted"}:
                continue
            identifier = group.get("identifier")
            needles = (
                [normalized_text(str(identifier))]
                if identifier
                else [normalized_text(item) for item in searchable_sources(anchor)]
            )
            present = any(needle and needle in normalized_pdf for needle in needles)
            occurrence_presence[group["id"]] = present
            if present:
                role_presence_counts[role] += 1
    font_run = subprocess.run(
        [commands["pdffonts"], str(target)], check=True, capture_output=True, text=True
    )
    font_rows = [line for line in font_run.stdout.splitlines()[2:] if line.strip()]
    fonts_embedded = all(re.search(r"\s+yes\s+(?:yes|no)\s+", row) for row in font_rows)
    resolved_font_stem = font_stem_family(resolved_cjk_file)
    cjk_font_embedded = bool(resolved_font_stem) and any(
        resolved_font_stem in normalized_font_name(row) for row in font_rows
    )
    blank_pages = [
        index + 1
        for index, (characters, fraction) in enumerate(zip(text_lengths, nonwhite_fractions))
        if characters < 15 and fraction < 0.002
    ]
    checks = {
        "pdf_created": (
            artifact_size(target, boundary=work_dir) > 0
            if work_dir is not None
            else target.is_file() and target.stat().st_size > 0
        ),
        "all_pages_rendered": len(renders) == len(page_sizes),
        "all_renders_decodable": not invalid_renders,
        "contact_sheets_complete": bool(contact_sheets)
        and contact_sheets[0]["first_page"] == 1
        and contact_sheets[-1]["last_page"] == len(page_sizes),
        "all_pages_a4": all(
            abs(width - 595.28) < 1.0 and abs(height - 841.89) < 1.0
            for width, height in page_sizes
        ),
        "no_apparently_blank_pages": not blank_pages,
        "chinese_extractable": bool(CJK_RE.search(joined)),
        "all_fonts_embedded": fonts_embedded,
        "requested_cjk_font_resolved_exactly": exact_cjk_font,
        "expected_cjk_font_embedded": cjk_font_embedded,
    }
    if context and context["schema_version"] == 2:
        visual_occurrences = sum(
            nodes[group["anchor_node_id"]].get("semantic", {}).get("output")
            == "visual-once"
            for group in context["ir"].get("semantic_groups", [])
        )
        checks["all_textual_role_occurrences_present"] = all(occurrence_presence.values())
        checks["visual_role_occurrences_present"] = (
            len(context["build"].get("assets", [])) >= visual_occurrences
            and pdf_image_count >= visual_occurrences
        )
        checks["profile_target_text_extractable"] = bool(
            target_text_pattern(context["profile"]).search(joined)
        )
    elif args.expected_problems is not None:
        checks["problem_count_matches"] = len(problem_ids) == args.expected_problems
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "automated_status": "passed" if all(checks.values()) else "failed",
        "docx": source.name,
        "pdf": target.name,
        "docx_sha256": (
            sha256_artifact(source, boundary=work_dir)
            if work_dir is not None
            else sha256(source)
        ),
        "pdf_sha256": (
            sha256_artifact(target, boundary=work_dir)
            if work_dir is not None
            else sha256(target)
        ),
        "page_count": len(page_sizes),
        "rendered_page_count": len(renders),
        "contact_sheets": contact_sheets,
        "invalid_renders": [path.name for path in invalid_renders],
        "problem_count": len(problem_ids),
        "font_count": len(font_rows),
        "pdf_image_count": pdf_image_count,
        "requested_cjk_font": args.cjk_font,
        "resolved_cjk_family": resolved_cjk_family,
        "resolved_cjk_file": resolved_cjk_file,
        "blank_pages": blank_pages,
        "minimum_page_text_characters": min(text_lengths) if text_lengths else 0,
        "minimum_nonwhite_fraction": min(nonwhite_fractions) if nonwhite_fractions else 0,
        "checks": checks,
        "warnings": [],
        "failures": [name for name, value in checks.items() if not value],
    }
    if work_dir is not None:
        report.update(compile_output_bindings)
    if context and context.get("docx_audit_bindings"):
        report.update(context["docx_audit_bindings"])
        report.update(
            {
                "profile": context["profile"]["id"],
                "docx_audit_sha256": sha256_artifact(
                    context["docx_audit_path"], boundary=work_dir
                ),
                "docx_audit_bindings": context["docx_audit_bindings"],
            }
        )
    if context and context["schema_version"] == 2:
        report.update(
            {
                "profile": context["profile"]["id"],
                "role_counts": role_counts,
                "role_presence_counts": role_presence_counts,
                "occurrence_presence": occurrence_presence,
                "document_ir_sha256": sha256_artifact(
                    context["ir_path"], boundary=work_dir
                ),
                "build_manifest_sha256": sha256_artifact(
                    context["build_path"], boundary=work_dir
                ),
                "docx_audit_sha256": sha256_artifact(
                    context["docx_audit_path"], boundary=work_dir
                ),
                "docx_audit_bindings": context["docx_audit_bindings"],
            }
        )
    if work_dir is not None:
        for directory in (render_dir, audit_output.parent / "contact"):
            if validate_artifact_tree(
                directory, work_dir, allow_missing=True
            ) is not None:
                clear_artifact_directory(directory, boundary=work_dir)
        _publish_flat_directory(active_render_dir, render_dir, work_dir)
        _publish_flat_directory(
            active_contact_dir, audit_output.parent / "contact", work_dir
        )
        _atomic_json(audit_output, report, work_dir)
        if stage_root is not None:
            clear_artifact_directory(
                stage_root, boundary=work_dir, remove_directory=True
            )
    else:
        audit_output.parent.mkdir(parents=True, exist_ok=True)
        audit_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
