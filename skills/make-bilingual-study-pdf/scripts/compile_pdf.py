#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import os
from pathlib import Path
from typing import Any

from audit_outputs import validate_output_audit_binding
from common import contains_cjk
from safe_artifacts import (
    ArtifactSafetyError,
    artifact_size,
    atomic_copy_file,
    atomic_write_text,
    clear_artifact_directory,
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
from visual_utils import image_ink_ratio, make_contact_sheets


CJK_FONT_CANDIDATES = [
    "Noto Serif CJK SC",
    "Source Han Serif SC",
    "FandolSong",
    "SimSun",
    "Microsoft YaHei",
    "Noto Sans CJK SC",
]


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required command not found: {name}")
    return path


def command_output(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return completed.stdout


def find_cjk_font() -> str | None:
    fc_list = shutil.which("fc-list")
    if fc_list:
        output = command_output([fc_list, ":", "family"])
        families = {item.strip() for line in output.splitlines() for item in line.split(",")}
        for candidate in CJK_FONT_CANDIDATES:
            if candidate in families:
                return candidate
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich:
        for filename, family in (
            ("FandolSong-Regular.otf", "FandolSong"),
            ("NotoSerifCJKsc-Regular.otf", "Noto Serif CJK SC"),
        ):
            completed = subprocess.run(
                [kpsewhich, filename],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if completed.stdout.strip():
                return family
    return None


def parse_pdf_pages(pdfinfo_text: str) -> int:
    match = re.search(r"^Pages:\s+(\d+)\s*$", pdfinfo_text, re.M)
    if not match:
        raise RuntimeError("pdfinfo did not report a page count")
    return int(match.group(1))


def write_failure(
    audit_path: Path,
    work_dir: Path,
    stage: str,
    failures: list[str],
    details: dict[str, Any] | None = None,
) -> None:
    report = {
        "status": "failed",
        "automated_status": "failed",
        "stage": stage,
        "failures": failures,
        "warnings": [],
    }
    if details:
        report.update(details)
    atomic_write_text(
        audit_path,
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    if not os.path.lexists(path):
        return False
    validate_artifact_file(path, boundary=work_dir)
    return True


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


def _publish_compile_stage(
    *,
    work_dir: Path,
    compile_audit_path: Path,
    visual_review_path: Path,
    qa_report_path: Path,
    build_dir: Path,
    renders_dir: Path,
    contact_dir: Path,
    stage_build: Path,
    stage_renders: Path,
    stage_contact: Path,
    report: dict[str, Any],
    force: bool,
) -> None:
    # The compile gate must disappear before the first mutation of any final
    # directory. A failed or partial publication may leave new bytes, but it can
    # never leave an earlier passed audit describing those mixed generations.
    remove_artifact_file(compile_audit_path, boundary=work_dir)
    remove_artifact_file(visual_review_path, boundary=work_dir)
    remove_artifact_file(qa_report_path, boundary=work_dir)
    if force and validate_artifact_tree(
        build_dir, work_dir, allow_missing=True
    ) is not None:
        clear_artifact_directory(build_dir, boundary=work_dir)
    for directory in (renders_dir, contact_dir):
        if validate_artifact_tree(directory, work_dir, allow_missing=True) is not None:
            clear_artifact_directory(directory, boundary=work_dir)
    prepare_artifact_directory(build_dir, boundary=work_dir)
    _publish_flat_directory(stage_build, build_dir, work_dir)
    _publish_flat_directory(stage_renders, renders_dir, work_dir)
    _publish_flat_directory(stage_contact, contact_dir, work_dir)
    atomic_write_text(
        compile_audit_path,
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile XeLaTeX, render every page, and run automated PDF QA."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--render-dpi", type=int, default=110)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not 90 <= args.render_dpi <= 200:
        raise SystemExit("--render-dpi must be between 90 and 200")

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, work_dir, allow_missing=False)
    output_audit_path = output_dir / "output-audit.json"
    build_manifest_path = output_dir / "build-manifest.json"
    compile_audit_path = output_dir / "compile-audit.json"
    visual_review_path = output_dir / "visual-review.json"
    qa_report_path = output_dir / "qa-report.json"
    for path in (
        output_audit_path,
        build_manifest_path,
        compile_audit_path,
        visual_review_path,
        qa_report_path,
    ):
        if os.path.lexists(path):
            validate_artifact_file(path, boundary=work_dir)
    try:
        output_audit, build, binding_errors = validate_output_audit_binding(
            work_dir, output_audit_path
        )
        if output_audit.get("status") != "passed":
            binding_errors.insert(0, "output audit is not passed")
        if binding_errors:
            raise ValueError("; ".join(binding_errors))
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        # All fixed output paths and the complete output tree were preflighted
        # above, so downstream gates can now be invalidated without following
        # an unsafe entry. Keep every compiled deliverable untouched.
        remove_artifact_file(visual_review_path, boundary=work_dir)
        remove_artifact_file(qa_report_path, boundary=work_dir)
        write_failure(
            compile_audit_path,
            work_dir,
            "output-audit",
            [f"output audit binding is missing, invalid, or stale: {exc}"],
        )
        raise SystemExit(1) from exc
    compile_output_bindings = {
        "build_manifest_sha256": sha256_artifact(
            build_manifest_path, boundary=work_dir
        ),
        "output_audit_sha256": sha256_artifact(
            output_audit_path, boundary=work_dir
        ),
    }
    tex_path = work_relative_artifact_path(
        output_dir, build.get("latex"), label="compiled LaTeX path"
    )
    if tex_path.parent != output_dir:
        raise ArtifactSafetyError("compiled LaTeX must be a direct output child")
    if not _artifact_exists(tex_path, work_dir) or sha256_artifact(
        tex_path, boundary=work_dir
    ) != build.get("latex_sha256"):
        raise SystemExit("XeLaTeX source is missing or changed after output audit")

    build_dir = output_dir / "build"
    renders_dir = output_dir / "pdf-renders"
    contact_dir = output_dir / "contact"
    for directory in (build_dir, renders_dir, contact_dir):
        validate_artifact_tree(directory, work_dir, allow_missing=True)
    output_pdf = build_dir / f"{tex_path.stem}.pdf"
    if _artifact_exists(output_pdf, work_dir) and not args.force:
        raise SystemExit(f"refusing to overwrite compiled PDF: {output_pdf}; use --force")

    try:
        latexmk = require_command("latexmk")
        require_command("xelatex")
        pdftoppm = require_command("pdftoppm")
        pdfinfo = require_command("pdfinfo")
        pdftotext = require_command("pdftotext")
        pdffonts = require_command("pdffonts")
    except RuntimeError as exc:
        remove_artifact_file(visual_review_path, boundary=work_dir)
        remove_artifact_file(qa_report_path, boundary=work_dir)
        write_failure(
            compile_audit_path,
            work_dir,
            "preflight",
            [str(exc)],
            compile_output_bindings,
        )
        raise SystemExit(1) from exc

    tex_text = read_artifact_text(tex_path, boundary=work_dir)
    chosen_cjk_font = None
    if contains_cjk(tex_text):
        kpsewhich = shutil.which("kpsewhich")
        xe_cjk_available = False
        if kpsewhich:
            completed = subprocess.run(
                [kpsewhich, "xeCJK.sty"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            xe_cjk_available = bool(completed.stdout.strip())
        chosen_cjk_font = find_cjk_font()
        preflight_failures = []
        if not xe_cjk_available:
            preflight_failures.append(
                "Chinese text is present but xeCJK.sty is unavailable; install a TeX Live CJK package"
            )
        if not chosen_cjk_font:
            preflight_failures.append(
                "Chinese text is present but no supported CJK font was found; install Noto Serif CJK SC, Source Han Serif SC, or FandolSong"
            )
        if preflight_failures:
            write_failure(
                compile_audit_path,
                work_dir,
                "preflight",
                preflight_failures,
                {"cjk_font": chosen_cjk_font, **compile_output_bindings},
            )
            raise SystemExit(1)

    stage_root = Path(
        tempfile.mkdtemp(prefix=".compile-stage-", dir=output_dir)
    )
    validate_artifact_directory(stage_root, boundary=work_dir)
    stage_build = prepare_artifact_directory(stage_root / "build", boundary=work_dir)
    stage_renders = prepare_artifact_directory(stage_root / "pdf-renders", boundary=work_dir)
    stage_contact = prepare_artifact_directory(stage_root / "contact", boundary=work_dir)
    staged_pdf = stage_build / f"{tex_path.stem}.pdf"

    command = [
        latexmk,
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        f"-outdir={stage_build.relative_to(output_dir)}",
        tex_path.name,
    ]
    completed = subprocess.run(
        command,
        cwd=output_dir,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    stdout_path = stage_build / "latexmk.stdout.txt"
    atomic_write_text(stdout_path, completed.stdout, boundary=work_dir)
    validate_artifact_tree(stage_build, work_dir, allow_missing=False)
    log_path = stage_build / f"{tex_path.stem}.log"
    log_text = (
        read_artifact_bytes(log_path, boundary=work_dir).decode("utf-8", errors="replace")
        if _artifact_exists(log_path, work_dir)
        else ""
    )
    if completed.returncode != 0 or not _artifact_exists(staged_pdf, work_dir):
        remove_artifact_file(visual_review_path, boundary=work_dir)
        remove_artifact_file(qa_report_path, boundary=work_dir)
        write_failure(
            compile_audit_path,
            work_dir,
            "latexmk",
            [f"latexmk returned {completed.returncode}; inspect {stdout_path.relative_to(output_dir)}"],
            {
                "latexmk_returncode": completed.returncode,
                "log_tail": (log_text or completed.stdout)[-5000:],
                **compile_output_bindings,
            },
        )
        raise SystemExit(1)

    failures: list[str] = []
    warnings: list[str] = []
    missing_characters = re.findall(r"^Missing character:.*$", log_text, re.M)
    undefined_references = re.findall(
        r"^.*(?:Reference .* undefined|There were undefined references).*$", log_text, re.M
    )
    overfull = re.findall(r"^Overfull \\hbox.*$", log_text, re.M)
    if missing_characters:
        failures.append(f"LaTeX reported {len(missing_characters)} missing characters")
    if undefined_references:
        failures.append(f"LaTeX reported {len(undefined_references)} undefined-reference warnings")
    if overfull:
        warnings.append(f"LaTeX reported {len(overfull)} overfull boxes; inspect them visually")

    validate_artifact_tree(stage_build, work_dir, allow_missing=False)
    info_text = command_output([pdfinfo, str(staged_pdf)])
    page_count = parse_pdf_pages(info_text)
    font_text = command_output([pdffonts, str(staged_pdf)])
    nonembedded_font_lines = [
        line
        for line in font_text.splitlines()[2:]
        if line.strip() and re.search(r"\sno\s+(?:yes|no)\s", line)
    ]
    if nonembedded_font_lines:
        warnings.append(f"{len(nonembedded_font_lines)} PDF font rows may be non-embedded")

    prefix = stage_renders / "page"
    subprocess.run(
        [pdftoppm, "-png", "-r", str(args.render_dpi), str(staged_pdf), str(prefix)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    validate_artifact_tree(stage_renders, work_dir, allow_missing=False)
    page_paths = sorted(stage_renders.glob("page-*.png"))
    for path in page_paths:
        validate_artifact_file(path, boundary=work_dir)
    if len(page_paths) != page_count:
        failures.append(
            f"rendered page count {len(page_paths)} does not match PDF page count {page_count}"
        )
    ink_ratios = [round(image_ink_ratio(path), 6) for path in page_paths]
    blank_pages = [index + 1 for index, ratio in enumerate(ink_ratios) if ratio < 0.001]
    if blank_pages:
        failures.append(f"apparently blank output pages: {blank_pages}")

    extracted_text = command_output([pdftotext, str(staged_pdf), "-"])
    expected_problem_ids = build.get("problem_ids", [])
    missing_problem_ids = [
        problem_id
        for problem_id in expected_problem_ids
        if not re.search(
            rf"Problem\s*[（(]\s*{re.escape(problem_id)}\s*[)）]", extracted_text, re.I
        )
    ]
    if missing_problem_ids:
        failures.append(f"compiled PDF text is missing Problem IDs: {missing_problem_ids}")
    source_contains_cjk = contains_cjk(tex_text)
    extracted_text_contains_cjk = contains_cjk(extracted_text)
    if source_contains_cjk and not extracted_text_contains_cjk:
        failures.append("compiled PDF text extraction contains no Chinese characters")

    contact_sheets = make_contact_sheets(page_paths, stage_contact) if page_paths else []
    validate_artifact_tree(stage_contact, work_dir, allow_missing=False)
    checks = {
        "latexmk_succeeded": completed.returncode == 0,
        "pdf_created": artifact_size(staged_pdf, boundary=work_dir) > 0,
        "no_missing_characters": not missing_characters,
        "no_undefined_references": not undefined_references,
        "all_pages_rendered": len(page_paths) == page_count,
        "no_apparently_blank_pages": not blank_pages,
        "all_problem_ids_present": not missing_problem_ids,
        "chinese_text_extractable": (
            not source_contains_cjk or extracted_text_contains_cjk
        ),
    }
    if not checks["pdf_created"]:
        failures.append("compiled PDF is empty")
    automated_status = "passed" if not failures and all(checks.values()) else "failed"
    report = {
        "status": "failed" if failures else "needs_visual_review",
        "automated_status": automated_status,
        "stage": "complete",
        "latexmk_returncode": completed.returncode,
        "cjk_font": chosen_cjk_font,
        "pdf": f"build/{staged_pdf.name}",
        "pdf_sha256": sha256_artifact(staged_pdf, boundary=work_dir),
        "page_count": page_count,
        "rendered_pages": len(page_paths),
        "ink_ratios": ink_ratios,
        "blank_pages": blank_pages,
        "missing_character_count": len(missing_characters),
        "undefined_reference_count": len(undefined_references),
        "overfull_box_count": len(overfull),
        "problem_ids_expected": expected_problem_ids,
        "problem_ids_missing": missing_problem_ids,
        "source_contains_cjk": source_contains_cjk,
        "extracted_text_contains_cjk": extracted_text_contains_cjk,
        "contact_sheets": contact_sheets,
        "checks": checks,
        "warnings": warnings,
        "failures": failures,
        **compile_output_bindings,
    }
    _publish_compile_stage(
        work_dir=work_dir,
        compile_audit_path=compile_audit_path,
        visual_review_path=visual_review_path,
        qa_report_path=qa_report_path,
        build_dir=build_dir,
        renders_dir=renders_dir,
        contact_dir=contact_dir,
        stage_build=stage_build,
        stage_renders=stage_renders,
        stage_contact=stage_contact,
        report=report,
        force=args.force,
    )
    clear_artifact_directory(
        stage_root, boundary=work_dir, remove_directory=True
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
