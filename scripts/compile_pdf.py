#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from common import contains_cjk, read_json, sha256_file, write_json
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
    audit_path: Path, stage: str, failures: list[str], details: dict[str, Any] | None = None
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
    write_json(audit_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


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

    work_dir = args.work_dir.expanduser().resolve()
    output_dir = work_dir / "output"
    output_audit_path = output_dir / "output-audit.json"
    build_manifest_path = output_dir / "build-manifest.json"
    compile_audit_path = output_dir / "compile-audit.json"
    if not output_audit_path.is_file() or not build_manifest_path.is_file():
        raise SystemExit("output build/audit artifacts are missing")
    if read_json(output_audit_path).get("status") != "passed":
        raise SystemExit("output audit is not passed")
    build = read_json(build_manifest_path)
    tex_path = output_dir / build["latex"]
    if not tex_path.is_file() or sha256_file(tex_path) != build["latex_sha256"]:
        raise SystemExit("XeLaTeX source is missing or changed after output audit")

    try:
        latexmk = require_command("latexmk")
        require_command("xelatex")
        pdftoppm = require_command("pdftoppm")
        pdfinfo = require_command("pdfinfo")
        pdftotext = require_command("pdftotext")
        pdffonts = require_command("pdffonts")
    except RuntimeError as exc:
        write_failure(compile_audit_path, "preflight", [str(exc)])
        raise SystemExit(1) from exc

    tex_text = tex_path.read_text(encoding="utf-8")
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
                "preflight",
                preflight_failures,
                {"cjk_font": chosen_cjk_font},
            )
            raise SystemExit(1)

    build_dir = output_dir / "build"
    renders_dir = output_dir / "pdf-renders"
    contact_dir = output_dir / "contact"
    build_dir.mkdir(exist_ok=True)
    renders_dir.mkdir(exist_ok=True)
    contact_dir.mkdir(exist_ok=True)
    output_pdf = build_dir / f"{tex_path.stem}.pdf"
    if output_pdf.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite compiled PDF: {output_pdf}; use --force")
    if args.force:
        for path in renders_dir.glob("page-*.png"):
            path.unlink()
        for suffix in (".aux", ".fdb_latexmk", ".fls", ".log", ".out", ".pdf"):
            generated = build_dir / f"{tex_path.stem}{suffix}"
            if generated.exists():
                generated.unlink()

    command = [
        latexmk,
        "-xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        f"-outdir={build_dir.name}",
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
    stdout_path = build_dir / "latexmk.stdout.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8", newline="\n")
    log_path = build_dir / f"{tex_path.stem}.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    if completed.returncode != 0 or not output_pdf.is_file():
        write_failure(
            compile_audit_path,
            "latexmk",
            [f"latexmk returned {completed.returncode}; inspect {stdout_path.relative_to(output_dir)}"],
            {
                "latexmk_returncode": completed.returncode,
                "log_tail": (log_text or completed.stdout)[-5000:],
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

    info_text = command_output([pdfinfo, str(output_pdf)])
    page_count = parse_pdf_pages(info_text)
    font_text = command_output([pdffonts, str(output_pdf)])
    nonembedded_font_lines = [
        line
        for line in font_text.splitlines()[2:]
        if line.strip() and re.search(r"\sno\s+(?:yes|no)\s", line)
    ]
    if nonembedded_font_lines:
        warnings.append(f"{len(nonembedded_font_lines)} PDF font rows may be non-embedded")

    prefix = renders_dir / "page"
    subprocess.run(
        [pdftoppm, "-png", "-r", str(args.render_dpi), str(output_pdf), str(prefix)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    page_paths = sorted(renders_dir.glob("page-*.png"))
    if len(page_paths) != page_count:
        failures.append(
            f"rendered page count {len(page_paths)} does not match PDF page count {page_count}"
        )
    ink_ratios = [round(image_ink_ratio(path), 6) for path in page_paths]
    blank_pages = [index + 1 for index, ratio in enumerate(ink_ratios) if ratio < 0.001]
    if blank_pages:
        failures.append(f"apparently blank output pages: {blank_pages}")

    extracted_text = command_output([pdftotext, str(output_pdf), "-"])
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
    if contains_cjk(tex_text) and not contains_cjk(extracted_text):
        failures.append("compiled PDF text extraction contains no Chinese characters")

    contact_sheets = make_contact_sheets(page_paths, contact_dir) if page_paths else []
    automated_status = "failed" if failures else "passed"
    report = {
        "status": "failed" if failures else "needs_visual_review",
        "automated_status": automated_status,
        "stage": "complete",
        "latexmk_returncode": completed.returncode,
        "cjk_font": chosen_cjk_font,
        "pdf": f"build/{output_pdf.name}",
        "pdf_sha256": sha256_file(output_pdf),
        "page_count": page_count,
        "rendered_pages": len(page_paths),
        "ink_ratios": ink_ratios,
        "blank_pages": blank_pages,
        "missing_character_count": len(missing_characters),
        "undefined_reference_count": len(undefined_references),
        "overfull_box_count": len(overfull),
        "problem_ids_expected": expected_problem_ids,
        "problem_ids_missing": missing_problem_ids,
        "contact_sheets": contact_sheets,
        "warnings": warnings,
        "failures": failures,
    }
    write_json(compile_audit_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
