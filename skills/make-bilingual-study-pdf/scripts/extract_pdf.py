#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

from common import (
    ascii_tokens,
    ngrams,
    normalize_text,
    problem_ids,
    sha256_file,
    sha256_text,
    write_json,
    write_jsonl,
)
from document_ir import write_document_ir
from profile import (
    bind_profile,
    canonical_profile_sha256,
    load_profile,
    prepare_profile_work_directory,
    validate_profile_binding_target,
)
from visual_utils import make_contact_sheets


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"required command not found: {name}")
    return path


def run_text(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout


def command_version(command: str, flag: str = "-v") -> str:
    completed = subprocess.run(
        [command, flag],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return (completed.stdout or "").strip().splitlines()[0]


def invalid_pngs(paths: list[Path]) -> list[Path]:
    invalid: list[Path] = []
    for path in paths:
        try:
            with Image.open(path) as image:
                image.load()
        except (OSError, SyntaxError, ValueError):
            invalid.append(path)
    return invalid


def repair_truncated_renders(
    pdftoppm: str,
    pdf_path: Path,
    rendered_pages: list[Path],
    render_dpi: int,
) -> None:
    invalid = invalid_pngs(rendered_pages)
    unrepaired: list[Path] = []
    for path in invalid:
        match = re.fullmatch(r"page-(\d+)\.png", path.name)
        if match is None:
            raise SystemExit(f"cannot recover page number from render: {path.name}")
        page_number = int(match.group(1))
        repaired = False
        for _attempt in range(3):
            with tempfile.TemporaryDirectory(
                prefix="bilingual-render-retry-"
            ) as temp_dir:
                retry_prefix = Path(temp_dir) / path.stem
                subprocess.run(
                    [
                        pdftoppm,
                        "-png",
                        "-r",
                        str(render_dpi),
                        "-f",
                        str(page_number),
                        "-l",
                        str(page_number),
                        "-singlefile",
                        str(pdf_path),
                        str(retry_prefix),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                retry_path = retry_prefix.with_suffix(".png")
                if invalid_pngs([retry_path]):
                    continue
                copy_command = shutil.which("cp")
                if copy_command:
                    subprocess.run(
                        [copy_command, "--", str(retry_path), str(path)], check=True
                    )
                else:
                    with retry_path.open("rb") as source, path.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                        target.flush()
                        os.fsync(target.fileno())
                if not invalid_pngs([path]):
                    repaired = True
                    break
        if not repaired:
            unrepaired.append(path)
    still_invalid = sorted(set(unrepaired + invalid_pngs(rendered_pages)))
    if still_invalid:
        names = ", ".join(path.name for path in still_invalid)
        raise SystemExit(f"rendering produced invalid PNG files after retry: {names}")


def intersects(a: fitz.Rect, b: fitz.Rect) -> bool:
    intersection = a & b
    return not intersection.is_empty and intersection.get_area() > 0


def split_poppler_pages(text: str) -> list[str]:
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def sequence_coverage(oracle: str, candidate: str) -> float:
    oracle_grams = ngrams(ascii_tokens(oracle), 5)
    candidate_grams = set(ngrams(ascii_tokens(candidate), 5))
    if not oracle_grams:
        return 1.0
    return sum(gram in candidate_grams for gram in oracle_grams) / len(oracle_grams)


def extract_page_blocks(
    page: fitz.Page,
    page_number: int,
    page_links: list[dict[str, Any]],
    sort: bool,
) -> list[dict[str, Any]]:
    page_blocks: list[dict[str, Any]] = []
    page_dict = page.get_text("dict", sort=sort)
    block_index = 0
    for block in page_dict.get("blocks", []):
        block_index += 1
        bbox = fitz.Rect(block.get("bbox", fitz.Rect()))
        if block.get("type") == 1:
            page_blocks.append(
                {
                    "id": f"p{page_number:03d}-b{block_index:03d}",
                    "page": page_number,
                    "bbox": [round(number, 3) for number in bbox],
                    "page_height": page.rect.height,
                    "source": "",
                    "source_sha256": sha256_text(""),
                    "kind": "image",
                    "translatable": False,
                    "stats": {},
                    "protected_spans": [],
                    "links": [],
                }
            )
            continue

        text, stats, protected_spans = extract_text_block(block)
        if not text:
            continue
        block_links = [
            item["id"]
            for item in page_links
            if intersects(bbox, fitz.Rect(item["bbox"]))
        ]
        page_blocks.append(
            {
                "id": f"p{page_number:03d}-b{block_index:03d}",
                "page": page_number,
                "bbox": [round(number, 3) for number in bbox],
                "page_height": page.rect.height,
                "source": text,
                "source_sha256": sha256_text(text),
                "kind": "unclassified",
                "translatable": True,
                "stats": stats,
                "protected_spans": protected_spans,
                "links": block_links,
            }
        )
    return page_blocks


def font_role(font: str) -> str | None:
    compact = re.sub(r"[^A-Za-z0-9]", "", font).lower()
    if any(
        token in compact
        for token in ("math", "cmmi", "cmsy", "cmex", "msam", "msbm")
    ):
        return "math"
    if "mono" in compact or "courier" in compact:
        return "code"
    return None


def extract_text_block(
    block: dict[str, Any],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    font_chars: Counter[str] = Counter()
    total_chars = 0
    mono_chars = 0
    math_chars = 0
    bold_chars = 0
    sizes: list[float] = []

    physical_lines = sorted(
        block.get("lines", []),
        key=lambda line: (line.get("bbox", [0, 0, 0, 0])[1], line.get("bbox", [0, 0, 0, 0])[0]),
    )
    rows: list[dict[str, Any]] = []
    for line in physical_lines:
        bbox = fitz.Rect(line.get("bbox", fitz.Rect()))
        matching = next(
            (
                row
                for row in rows
                if abs(row["bbox"].y0 - bbox.y0) <= 1.2
                and abs(row["bbox"].y1 - bbox.y1) <= 1.2
            ),
            None,
        )
        if matching is None:
            matching = {"bbox": fitz.Rect(bbox), "lines": []}
            rows.append(matching)
        else:
            matching["bbox"] |= bbox
        matching["lines"].append(line)
    rows.sort(key=lambda row: (row["bbox"].y0, row["bbox"].x0))

    block_bbox = fitz.Rect(block.get("bbox", fitz.Rect()))
    rendered_rows: list[dict[str, Any]] = []
    for row in rows:
        spans = sorted(
            (
                span
                for line in row["lines"]
                for span in line.get("spans", [])
                if span.get("text", "")
            ),
            key=lambda span: (span.get("bbox", [0, 0, 0, 0])[0], span.get("bbox", [0, 0, 0, 0])[1]),
        )
        if not spans:
            continue
        row_chars = sum(len(span.get("text", "")) for span in spans)
        row_code_chars = sum(
            len(span.get("text", ""))
            for span in spans
            if font_role(span.get("font", "")) == "code"
        )
        median_row_size = sorted(float(span.get("size", 0.0)) for span in spans)[
            len(spans) // 2
        ]
        prefix = ""
        if row_chars and row_code_chars / row_chars >= 0.70:
            indent_width = max(1.0, median_row_size * 0.52)
            prefix = " " * max(
                0, round((row["bbox"].x0 - block_bbox.x0) / indent_width)
            )

        parts = [prefix]
        line_runs: list[dict[str, Any]] = []
        line_offset = len(prefix)
        previous_x1: float | None = None
        previous_value = ""
        for span in spans:
            value = span.get("text", "")
            font = span.get("font", "")
            span_bbox = fitz.Rect(span.get("bbox", fitz.Rect()))
            if previous_x1 is not None and not previous_value.endswith((" ", "\t")):
                gap = span_bbox.x0 - previous_x1
                if gap > max(1.0, float(span.get("size", 0.0)) * 0.14):
                    parts.append(" ")
                    line_offset += 1
            role = font_role(font)
            parts.append(value)
            if role:
                line_runs.append(
                    {
                        "start": line_offset,
                        "end": line_offset + len(value),
                        "role": role,
                    }
                )
            line_offset += len(value)
            previous_x1 = span_bbox.x1
            previous_value = value

            length = len(value)
            total_chars += length
            font_chars[font] += length
            sizes.append(float(span.get("size", 0.0)))
            if role == "code":
                mono_chars += length
            if role == "math":
                math_chars += length
            if "bold" in font.lower():
                bold_chars += length
        line_text = "".join(parts).rstrip()
        rendered_rows.append(
            {
                "text": line_text,
                "runs": [run for run in line_runs if run["start"] < len(line_text)],
                "bbox": row["bbox"],
            }
        )

    positive_gaps = [
        current["bbox"].y0 - previous["bbox"].y1
        for previous, current in zip(rendered_rows, rendered_rows[1:])
        if current["bbox"].y0 >= previous["bbox"].y1
    ]
    median_gap = (
        sorted(positive_gaps)[len(positive_gaps) // 2] if positive_gaps else 0.0
    )
    separators: list[str] = []
    paragraph_count = 1 if rendered_rows else 0
    for previous, current in zip(rendered_rows, rendered_rows[1:]):
        gap = current["bbox"].y0 - previous["bbox"].y1
        previous_text = previous["text"].rstrip()
        current_text = current["text"].lstrip()
        likely_break = (
            gap > median_gap + 0.2
            and bool(re.search(r"[.!?。！？][\"')\]]?$", previous_text))
        ) or (
            bool(re.match(r"^(?:Problem|Deliverable|For reference|Note)\b", current_text))
            and bool(re.search(r"[.!?。！？][\"')\]]?$", previous_text))
        )
        separators.append("\n\n" if likely_break else "\n")
        if likely_break:
            paragraph_count += 1

    pieces: list[str] = []
    for index, row in enumerate(rendered_rows):
        pieces.append(row["text"])
        if index < len(separators):
            pieces.append(separators[index])
    untrimmed = "".join(pieces)
    left_trim = len(untrimmed) - len(untrimmed.lstrip())
    text = untrimmed.strip()
    right_limit = left_trim + len(text)

    protected_spans: list[dict[str, Any]] = []
    offset = 0
    for index, row in enumerate(rendered_rows):
        line_text = row["text"]
        for run in row["runs"]:
            start = max(offset + run["start"], left_trim)
            end = min(offset + run["end"], offset + len(line_text), right_limit)
            if start >= end:
                continue
            adjusted_start = start - left_trim
            adjusted_end = end - left_trim
            protected_spans.append(
                {
                    "start": adjusted_start,
                    "end": adjusted_end,
                    "text": text[adjusted_start:adjusted_end],
                    "role": run["role"],
                }
            )
        offset += len(line_text)
        if index < len(separators):
            offset += len(separators[index])

    denominator = max(1, total_chars)
    stats = {
        "fonts": [name for name, _ in font_chars.most_common()],
        "font_character_counts": dict(font_chars),
        "max_font_size": max(sizes, default=0.0),
        "median_font_size": sorted(sizes)[len(sizes) // 2] if sizes else 0.0,
        "mono_ratio": round(mono_chars / denominator, 4),
        "math_ratio": round(math_chars / denominator, 4),
        "bold_ratio": round(bold_chars / denominator, 4),
        "line_count": len(rendered_rows),
        "paragraph_count": paragraph_count,
    }
    return text, stats, protected_spans


def horizontal_overlap_ratio(rect: fitz.Rect, left: float, right: float) -> float:
    if rect.width < 1.0:
        return 1.0 if left <= rect.x0 <= right else 0.0
    overlap = max(0.0, min(rect.x1, right) - max(rect.x0, left))
    return overlap / max(1.0, rect.width)


def make_visuals(
    doc: fitz.Document,
    blocks: list[dict[str, Any]],
    work_dir: Path,
    render_dpi: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Crop native figures/tables and PDF image blocks without reconstructing them."""
    visuals_dir = work_dir / "visuals"
    visuals_dir.mkdir(exist_ok=True)
    by_page: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        by_page.setdefault(int(block["page"]), []).append(block)

    visuals: list[dict[str, Any]] = []
    unresolved: list[str] = []
    scale = max(1.5, render_dpi / 72.0)

    for page_number, page_blocks in by_page.items():
        page = doc[page_number - 1]
        for block in page_blocks:
            if block["kind"] not in {"image", "math", "math_with_text"}:
                continue
            clip = fitz.Rect(block["bbox"]) & page.rect
            minimum_size = 2 if block["kind"] in {"math", "math_with_text"} else 8
            if clip.is_empty or clip.width < minimum_size or clip.height < minimum_size:
                unresolved.append(block["id"])
                continue
            if block["kind"] in {"math", "math_with_text"}:
                margin = 1 if block["kind"] == "math_with_text" else 3
                clip = fitz.Rect(
                    max(page.rect.x0, clip.x0 - margin),
                    max(page.rect.y0, clip.y0 - margin),
                    min(page.rect.x1, clip.x1 + margin),
                    min(page.rect.y1, clip.y1 + margin),
                )
            visual_id = f"visual-{block['id']}"
            filename = f"{visual_id}.png"
            page.get_pixmap(
                matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False
            ).save(visuals_dir / filename)
            visuals.append(
                {
                    "id": visual_id,
                    "page": page_number,
                    "kind": (
                        "embedded_image"
                        if block["kind"] == "image"
                        else block["kind"]
                    ),
                    "anchor_id": block["id"],
                    "caption_id": None,
                    "bbox": [round(number, 3) for number in clip],
                    "path": f"visuals/{filename}",
                    "contained_block_ids": [block["id"]],
                }
            )

        captions = [
            block
            for block in page_blocks
            if block["kind"] == "caption"
            and re.match(r"^(Figure|Table)\s+\d+\s*:", block["source"], re.I)
        ]
        if not captions:
            continue

        caption_rows: list[list[dict[str, Any]]] = []
        for caption in sorted(captions, key=lambda item: (item["bbox"][1], item["bbox"][0])):
            if (
                caption_rows
                and abs(caption["bbox"][1] - caption_rows[-1][0]["bbox"][1]) <= 18
            ):
                caption_rows[-1].append(caption)
            else:
                caption_rows.append([caption])

        drawings = [
            fitz.Rect(item["rect"])
            for item in page.get_drawings()
            if fitz.Rect(item["rect"]).get_area() >= 20
            or max(fitz.Rect(item["rect"]).width, fitz.Rect(item["rect"]).height) >= 8
        ]
        for row in caption_rows:
            ordered = sorted(row, key=lambda item: item["bbox"][0])
            centers = [
                (item["bbox"][0] + item["bbox"][2]) / 2 for item in ordered
            ]
            boundaries = [36.0]
            boundaries.extend(
                (centers[index] + centers[index + 1]) / 2
                for index in range(len(centers) - 1)
            )
            boundaries.append(page.rect.width - 36.0)

            for index, caption in enumerate(ordered):
                caption_rect = fitz.Rect(caption["bbox"])
                left, right = boundaries[index], boundaries[index + 1]
                continuations: list[str] = []
                if not re.search(r"[.!?。！？][\"')\]]?$", caption["source"].strip()):
                    for candidate_block in page_blocks:
                        if candidate_block["id"] == caption["id"]:
                            continue
                        rect = fitz.Rect(candidate_block["bbox"])
                        center = (rect.x0 + rect.x1) / 2
                        if (
                            candidate_block["kind"] == "prose"
                            and caption_rect.y1 <= rect.y0 <= caption_rect.y1 + 24
                            and left <= center <= right
                            and len(candidate_block["source"]) <= 180
                        ):
                            candidate_block["kind"] = "caption_continuation"
                            candidate_block["caption_parent"] = caption["id"]
                            continuations.append(candidate_block["id"])
                candidates = [
                    rect
                    for rect in drawings
                    if rect.y1 <= caption_rect.y0 + 3
                    and caption_rect.y0 - rect.y1 <= 380
                    and rect.y0 >= max(0.0, caption_rect.y0 - 400)
                    and horizontal_overlap_ratio(rect, left, right) >= 0.5
                ]
                if not candidates:
                    unresolved.append(caption["id"])
                    continue
                seed = max(
                    candidates,
                    key=lambda rect: (rect.y1, rect.get_area(), rect.width + rect.height),
                )
                cluster = [seed]
                union = fitz.Rect(seed)
                changed = True
                while changed:
                    changed = False
                    expanded = fitz.Rect(
                        union.x0 - 8, union.y0 - 8, union.x1 + 8, union.y1 + 8
                    )
                    for rect in candidates:
                        if rect in cluster:
                            continue
                        if (
                            not (rect & expanded).is_empty
                            or expanded.contains(rect.tl)
                            or expanded.contains(rect.br)
                        ):
                            cluster.append(rect)
                            union = fitz.Rect(
                                min(union.x0, rect.x0),
                                min(union.y0, rect.y0),
                                max(union.x1, rect.x1),
                                max(union.y1, rect.y1),
                            )
                            changed = True
                union.x0 = max(left, union.x0 - 4)
                union.x1 = min(right, union.x1 + 4)
                union.y0 = max(0.0, union.y0 - 4)
                union.y1 = min(caption_rect.y0 - 2, union.y1 + 4)
                if union.width < 40 or union.height < 30:
                    unresolved.append(caption["id"])
                    continue

                contained: list[str] = []
                for candidate_block in page_blocks:
                    if candidate_block["id"] == caption["id"]:
                        continue
                    rect = fitz.Rect(candidate_block["bbox"])
                    intersection = rect & union
                    if (
                        not intersection.is_empty
                        and intersection.get_area() / max(1.0, rect.get_area()) >= 0.65
                    ):
                        contained.append(candidate_block["id"])
                        if candidate_block["kind"] not in {"artifact", "image"}:
                            candidate_block["kind"] = "visual_content"
                            candidate_block["translatable"] = False

                visual_id = f"visual-{caption['id']}"
                filename = f"{visual_id}.png"
                page.get_pixmap(
                    matrix=fitz.Matrix(scale, scale), clip=union, alpha=False
                ).save(visuals_dir / filename)
                visuals.append(
                    {
                        "id": visual_id,
                        "page": page_number,
                        "kind": "figure_or_table",
                        "anchor_id": caption["id"],
                        "caption_id": caption["id"],
                        "caption_continuation_ids": continuations,
                        "bbox": [round(number, 3) for number in union],
                        "path": f"visuals/{filename}",
                        "contained_block_ids": contained,
                    }
                )

    return visuals, unresolved


def classify_block(
    text: str,
    stats: dict[str, Any],
    bbox: fitz.Rect,
    page_height: float,
    repeated_margin_text: set[str],
) -> str:
    normalized = normalize_text(text)
    margin_key = re.sub(r"\d+", "#", normalized.lower())
    near_bottom = bbox.y0 >= page_height * 0.92
    near_top = bbox.y1 <= page_height * 0.08

    if not normalized:
        return "empty"
    if near_bottom and re.fullmatch(r"\d+", normalized):
        return "artifact"
    if (near_top or near_bottom) and margin_key in repeated_margin_text:
        return "artifact"
    if stats["mono_ratio"] >= 0.80:
        return "code"
    math_symbol_count = len(re.findall(r"[=∑√∫∏≤≥≠≈→←⊤⊥⋅×⊙]", normalized))
    natural_math_words = re.findall(r"\b[A-Za-z]{3,}\b", normalized)
    formula_like = (
        math_symbol_count >= 1
        and (stats.get("line_count", 1) >= 2 or bool(re.search(r"\(\d+\)\s*$", normalized)))
        and len(natural_math_words) <= 4
    )
    if (
        (stats["math_ratio"] >= 0.55 or formula_like)
        and len(normalized) <= 500
        and re.match(r"^where\b", normalized, re.I)
    ):
        return "math_with_text"
    if (stats["math_ratio"] >= 0.55 or formula_like) and len(normalized) <= 500:
        return "math"
    if re.match(r"^(Figure|Table|Algorithm)\s+\d+\s*:", normalized, re.I):
        return "caption"
    if re.match(r"^(Problem|Example|Low-Resource Tip)\s*[(:]", normalized, re.I):
        return "callout"
    if (
        len(normalized) <= 220
        and (
            re.match(r"^\d+(?:\.\d+)*\s+\S", normalized)
            or stats["max_font_size"] >= 14
            or stats["bold_ratio"] >= 0.72
        )
    ):
        return "heading"
    if re.match(r"^(?:[•*-]|\d+[.)]|\([a-z]\))\s+", normalized, re.I):
        return "list"
    return "prose"


def margin_repetitions(raw_pages: list[list[dict[str, Any]]]) -> set[str]:
    counts: Counter[str] = Counter()
    for page_blocks in raw_pages:
        seen: set[str] = set()
        for item in page_blocks:
            bbox = fitz.Rect(item["bbox"])
            page_height = item["page_height"]
            if bbox.y1 > page_height * 0.10 and bbox.y0 < page_height * 0.90:
                continue
            normalized = normalize_text(item.get("source", "")).lower()
            if not normalized:
                continue
            seen.add(re.sub(r"\d+", "#", normalized))
        counts.update(seen)
    return {value for value, count in counts.items() if count >= 3}


def prepare_output(work_dir: Path, force: bool) -> None:
    try:
        work_dir = prepare_profile_work_directory(work_dir)
        validate_profile_binding_target(work_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    collisions = [
        work_dir / "profile.json",
        work_dir / "document-ir.json",
        work_dir / "manifest.json",
        work_dir / "blocks.jsonl",
        work_dir / "oracle.txt",
        work_dir / "oracle-layout.txt",
        work_dir / "source-audit.json",
    ]
    existing = [path for path in collisions if path.exists()]
    if existing and not force:
        names = ", ".join(path.name for path in existing)
        raise SystemExit(f"refusing to overwrite existing artifacts: {names}; use --force")
    if force:
        for generated in (work_dir / "document-ir.json",):
            if generated.is_file():
                generated.unlink()
        for directory, pattern in (
            (work_dir / "renders", "page-*.png"),
            (work_dir / "visuals", "visual-*.png"),
        ):
            if directory.is_dir():
                for generated in directory.glob(pattern):
                    generated.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a native-text PDF into auditable page/block artifacts."
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument(
        "--profile",
        default="assignment-en-zh",
        help="built-in profile id or path to a profile JSON file",
    )
    parser.add_argument("--render-dpi", type=int, default=120)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    work_dir = Path(os.path.abspath(args.work_dir.expanduser()))
    if not pdf_path.is_file():
        raise SystemExit(f"PDF does not exist: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise SystemExit(f"input is not a PDF: {pdf_path}")
    if args.render_dpi < 72 or args.render_dpi > 300:
        raise SystemExit("--render-dpi must be between 72 and 300")
    try:
        profile = load_profile(args.profile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if profile["input"]["adapter"] != "native-text-pdf":
        raise SystemExit("extract_pdf.py requires the native-text-pdf input adapter")

    pdftotext = require_command("pdftotext")
    pdftoppm = require_command("pdftoppm")
    prepare_output(work_dir, args.force)
    try:
        profile = bind_profile(work_dir, args.profile, force=args.force)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise SystemExit(f"cannot open PDF: {exc}") from exc
    if doc.needs_pass:
        raise SystemExit("encrypted PDFs are not supported in v1")
    if doc.page_count == 0:
        raise SystemExit("PDF has no pages")

    oracle_text = run_text([pdftotext, "-raw", str(pdf_path), "-"])
    oracle_layout_text = run_text([pdftotext, "-layout", str(pdf_path), "-"])
    oracle_pages = split_poppler_pages(oracle_text)
    oracle_layout_pages = split_poppler_pages(oracle_layout_text)
    if len(oracle_pages) != doc.page_count:
        raise SystemExit(
            f"page-count mismatch: PDF={doc.page_count}, pdftotext={len(oracle_pages)}"
        )
    if len(oracle_layout_pages) != doc.page_count:
        raise SystemExit(
            "page-count mismatch for Poppler layout oracle: "
            f"PDF={doc.page_count}, pdftotext={len(oracle_layout_pages)}"
        )

    minimum_page_characters = profile["input"]["minimum_text_characters_per_page"]
    minimum_native_ratio = profile["input"]["minimum_native_text_page_ratio"]
    page_text_lengths = [len(normalize_text(page)) for page in oracle_pages]
    native_pages = sum(length >= minimum_page_characters for length in page_text_lengths)
    native_ratio = native_pages / doc.page_count
    if native_ratio < minimum_native_ratio:
        raise SystemExit(
            "the active profile accepts native-text PDFs only: usable-text page ratio "
            f"{native_ratio:.3f} is below {minimum_native_ratio:.3f}; report this "
            "document as scanned/mixed instead of continuing"
        )

    all_links: list[dict[str, Any]] = []
    raw_pages: list[list[dict[str, Any]]] = []
    page_summaries: list[dict[str, Any]] = []
    drawing_counts: list[int] = []

    for page_number, page in enumerate(doc, start=1):
        page_links: list[dict[str, Any]] = []
        for link_number, link in enumerate(page.get_links(), start=1):
            rect = fitz.Rect(link.get("from", fitz.Rect()))
            value = {
                "id": f"p{page_number:03d}-l{link_number:03d}",
                "page": page_number,
                "bbox": [round(number, 3) for number in rect],
                "uri": link.get("uri"),
                "target_page": (link.get("page") + 1) if link.get("page", -1) >= 0 else None,
            }
            page_links.append(value)
            all_links.append(value)

        unsorted_blocks = extract_page_blocks(page, page_number, page_links, sort=False)
        sorted_blocks = extract_page_blocks(page, page_number, page_links, sort=True)
        order_scores = {}
        for strategy, candidate in (
            ("content_stream", unsorted_blocks),
            ("geometric", sorted_blocks),
        ):
            candidate_text = "\n".join(
                item["source"] for item in candidate if item["kind"] != "image"
            )
            order_scores[strategy] = sequence_coverage(
                oracle_pages[page_number - 1], candidate_text
            )
        if order_scores["content_stream"] >= order_scores["geometric"]:
            page_blocks = unsorted_blocks
            block_order_strategy = "content_stream"
        else:
            page_blocks = sorted_blocks
            block_order_strategy = "geometric"
        raw_pages.append(page_blocks)
        drawing_count = len(page.get_drawings())
        drawing_counts.append(drawing_count)
        page_summaries.append(
            {
                "page": page_number,
                "width": round(page.rect.width, 3),
                "height": round(page.rect.height, 3),
                "rotation": page.rotation,
                "oracle_characters": page_text_lengths[page_number - 1],
                "drawing_count": drawing_count,
                "link_count": len(page_links),
                "problem_ids": sorted(set(problem_ids(oracle_pages[page_number - 1]))),
                "block_order_strategy": block_order_strategy,
                "block_order_candidate_coverages": {
                    name: round(score, 4) for name, score in order_scores.items()
                },
            }
        )

    repeated_margin_text = margin_repetitions(raw_pages)
    blocks: list[dict[str, Any]] = []
    for page_blocks in raw_pages:
        for block in page_blocks:
            if block["kind"] != "image":
                bbox = fitz.Rect(block["bbox"])
                block["kind"] = classify_block(
                    block["source"],
                    block["stats"],
                    bbox,
                    block["page_height"],
                    repeated_margin_text,
                )
                block["translatable"] = block["kind"] not in {
                    "artifact",
                    "code",
                    "math",
                }
            block.pop("page_height", None)
            blocks.append(block)

    visuals, unresolved_visuals = make_visuals(
        doc, blocks, work_dir, args.render_dpi
    )

    renders_dir = work_dir / "renders"
    renders_dir.mkdir(exist_ok=True)
    prefix = renders_dir / "page"
    subprocess.run(
        [
            pdftoppm,
            "-png",
            "-r",
            str(args.render_dpi),
            str(pdf_path),
            str(prefix),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    rendered_pages = sorted(renders_dir.glob("page-*.png"))
    if len(rendered_pages) != doc.page_count:
        raise SystemExit(
            f"rendering incomplete: expected {doc.page_count}, found {len(rendered_pages)}"
        )
    repair_truncated_renders(pdftoppm, pdf_path, rendered_pages, args.render_dpi)
    source_contact_sheets = make_contact_sheets(
        rendered_pages, work_dir / "source-contact"
    )

    (work_dir / "oracle.txt").write_text(oracle_text, encoding="utf-8")
    (work_dir / "oracle-layout.txt").write_text(
        oracle_layout_text, encoding="utf-8"
    )
    write_jsonl(work_dir / "blocks.jsonl", blocks)
    external_uris = sorted({item["uri"] for item in all_links if item.get("uri")})
    manifest = {
        "schema_version": 3,
        "profile": {
            "id": profile["id"],
            "sha256": canonical_profile_sha256(profile),
        },
        "source_pdf": str(pdf_path),
        "source_sha256": sha256_file(pdf_path),
        "page_count": doc.page_count,
        "native_text_page_ratio": round(native_ratio, 4),
        "render_dpi": args.render_dpi,
        "artifacts": {
            "profile": "profile.json",
            "document_ir": "document-ir.json",
            "blocks": "blocks.jsonl",
            "oracle": "oracle.txt",
            "oracle_layout": "oracle-layout.txt",
            "renders": "renders/page-*.png",
            "visuals": "visuals/visual-*.png",
            "source_contact": "source-contact/contact-*.png",
        },
        "tools": {
            "pymupdf": fitz.VersionBind,
            "pdftotext": command_version(pdftotext, "-v"),
            "pdftoppm": command_version(pdftoppm, "-v"),
        },
        "pages": page_summaries,
        "block_count": len(blocks),
        "block_kind_counts": dict(Counter(block["kind"] for block in blocks)),
        "problem_ids": sorted(set(problem_ids(oracle_text))),
        "external_uris": external_uris,
        "external_uri_count": len(external_uris),
        "internal_link_count": sum(item.get("target_page") is not None for item in all_links),
        "links": all_links,
        "visuals": visuals,
        "unresolved_visual_anchors": unresolved_visuals,
        "source_contact_sheets": source_contact_sheets,
        "drawing_pages": [
            index + 1 for index, count in enumerate(drawing_counts) if count > 0
        ],
    }
    write_json(work_dir / "manifest.json", manifest)
    document_ir_path = write_document_ir(work_dir, profile)
    document_ir = json.loads(document_ir_path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "work_dir": str(work_dir),
                "pages": doc.page_count,
                "blocks": len(blocks),
                "problem_ids": len(manifest["problem_ids"]),
                "external_uris": len(external_uris),
                "native_text_page_ratio": manifest["native_text_page_ratio"],
                "profile": profile["id"],
                "semantic_roles": document_ir["inventories"]["semantic_role_counts"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"external command failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode or 1) from exc
