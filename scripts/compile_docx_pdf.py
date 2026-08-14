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
from common import read_json, sha256_file
from profile import load_work_profile, profile_contract, target_text_pattern
from visual_utils import make_contact_sheets


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


def load_v2_context(work_dir: Path) -> dict[str, Any]:
    work_dir = work_dir.expanduser().resolve()
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    build_path = work_dir / "output" / "build-manifest.json"
    missing = [str(path) for path in (profile_path, ir_path, build_path) if not path.is_file()]
    if missing:
        raise ValueError(f"schema V2 PDF compile is missing frozen artifacts: {missing}")
    profile = load_work_profile(work_dir)
    if profile.get("schema_version") != 2:
        return {"schema_version": 1, "profile": profile}
    ir = read_json(ir_path)
    build = read_json(build_path)
    if ir.get("schema_version") != 2:
        raise ValueError("schema V2 PDF compile requires document IR schema_version 2")
    if build.get("profile_id") != profile["id"] or ir.get("profile", {}).get("id") != profile["id"]:
        raise ValueError("frozen Profile ids disagree")
    if build.get("profile_file_sha256") != sha256_file(profile_path):
        raise ValueError("build manifest does not bind the frozen Profile file")
    if build.get("document_ir_sha256") != sha256_file(ir_path):
        raise ValueError("build manifest does not bind the frozen document IR")
    if build.get("role_inventory") != ir.get("inventories", {}).get("role_inventory"):
        raise ValueError("build manifest role inventory does not match document IR")
    return {
        "schema_version": 2,
        "work_dir": work_dir,
        "profile": profile,
        "ir": ir,
        "build": build,
        "ir_path": ir_path,
        "build_path": build_path,
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
    if args.work_dir is not None:
        try:
            context = load_v2_context(args.work_dir)
            expected_role_flags = parse_expected_roles(args.expected_role)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if args.expected_role:
            raise SystemExit("--expected-role requires --work-dir")
        expected_role_flags = {}
    if context and context["schema_version"] == 1 and expected_role_flags:
        raise SystemExit("--expected-role is available only for schema V2 Profiles")

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
    contact_dir = args.audit_output.resolve().parent / "contact"
    contact_sheets = make_contact_sheets(renders, contact_dir) if renders else []

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
        "pdf_created": target.is_file() and target.stat().st_size > 0,
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
        "docx_sha256": sha256(source),
        "pdf_sha256": sha256(target),
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
    if context and context["schema_version"] == 2:
        report.update(
            {
                "profile": context["profile"]["id"],
                "role_counts": role_counts,
                "role_presence_counts": role_presence_counts,
                "occurrence_presence": occurrence_presence,
                "document_ir_sha256": sha256_file(context["ir_path"]),
                "build_manifest_sha256": sha256_file(context["build_path"]),
            }
        )
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
