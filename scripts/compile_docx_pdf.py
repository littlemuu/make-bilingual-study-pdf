#!/usr/bin/env python3
"""Render a V2 DOCX to PDF, render every page, and run automated PDF checks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import fitz

from extract_pdf import invalid_pngs, repair_truncated_renders


CJK_RE = re.compile(r"[\u3400-\u9fff]")
PROBLEM_RE = re.compile(r"Problem \(([^)]+)\)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_font_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def font_stem_family(path: str) -> str:
    stem = normalized_font_name(Path(path).stem)
    return re.sub(
        r"(?:thin|extralight|light|regular|medium|semibold|demibold|bold|extrabold|black|italic|oblique)+$",
        "",
        stem,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--expected-problems", type=int)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument("--cjk-font", default="Noto Sans S Chinese")
    args = parser.parse_args()

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
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(1)

    source = args.docx.resolve()
    target = args.pdf.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bilingual-docx-pdf-") as temporary:
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
        if not generated.is_file():
            raise SystemExit(
                "LibreOffice did not produce the expected PDF\n"
                f"exit={conversion.returncode}\nstdout={conversion.stdout}\nstderr={conversion.stderr}"
            )
        shutil.copy2(generated, target)

    render_dir = args.render_dir.resolve()
    render_dir.mkdir(parents=True, exist_ok=True)
    for old in render_dir.glob("page-*.png"):
        old.unlink()
    subprocess.run(
        [
            commands["pdftoppm"],
            "-png",
            "-r",
            str(args.dpi),
            str(target),
            str(render_dir / "page"),
        ],
        check=True,
    )
    renders = sorted(render_dir.glob("page-*.png"))
    repair_truncated_renders(commands["pdftoppm"], target, renders, args.dpi)
    invalid_renders = invalid_pngs(renders)

    document = fitz.open(target)
    page_sizes = []
    text_lengths = []
    nonwhite_fractions = []
    full_text = []
    for page in document:
        text = page.get_text()
        full_text.append(text)
        page_sizes.append([round(page.rect.width, 2), round(page.rect.height, 2)])
        text_lengths.append(len(re.sub(r"\s+", "", text)))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5), colorspace=fitz.csGRAY, alpha=False)
        nonwhite_fractions.append(
            round(sum(value < 248 for value in pixmap.samples) / len(pixmap.samples), 6)
        )
    document.close()
    joined = "\n".join(full_text)
    problem_ids = PROBLEM_RE.findall(joined)
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
        "pdf_created": target.is_file() and target.stat().st_size > 0,
        "all_pages_rendered": len(renders) == len(page_sizes),
        "all_renders_decodable": not invalid_renders,
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
    if args.expected_problems is not None:
        checks["problem_count_matches"] = len(problem_ids) == args.expected_problems
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "docx_sha256": sha256(source),
        "pdf_sha256": sha256(target),
        "page_count": len(page_sizes),
        "rendered_page_count": len(renders),
        "invalid_renders": [path.name for path in invalid_renders],
        "problem_count": len(problem_ids),
        "font_count": len(font_rows),
        "requested_cjk_font": args.cjk_font,
        "resolved_cjk_family": resolved_cjk_family,
        "resolved_cjk_file": resolved_cjk_file,
        "blank_pages": blank_pages,
        "minimum_page_text_characters": min(text_lengths) if text_lengths else 0,
        "minimum_nonwhite_fraction": min(nonwhite_fractions) if nonwhite_fractions else 0,
        "checks": checks,
        "failures": [name for name, value in checks.items() if not value],
    }
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
