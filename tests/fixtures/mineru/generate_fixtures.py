#!/usr/bin/env python3
"""Regenerate the original synthetic PDFs/assets under pipeline-3.4.4.

The fixture JSON is hand-authored against the documented MinerU 3.4.4 legacy
pipeline schema.  This script creates the corresponding original PDF bytes and
the synthetic structured-table screenshot; it never invokes MinerU or downloads
a model.
"""
from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent / "pipeline-3.4.4"


def write_table_image() -> None:
    """Create the deterministic screenshot paired with the structured table."""

    path = ROOT / "native" / "images" / "table.png"
    image = Image.new("RGB", (720, 180), "white")
    draw = ImageDraw.Draw(image)
    columns = (0, 480, 719)
    rows = (0, 70, 179)
    for x in columns:
        draw.line((x, 0, x, 179), fill="#202938", width=3)
    for y in rows:
        draw.line((0, y, 719, y), fill="#202938", width=3)
    draw.rectangle((2, 2, 717, 68), fill="#dbeafe")
    draw.text((24, 24), "Gate", fill="#111827")
    draw.text((504, 24), "Count", fill="#111827")
    draw.text((24, 112), "explicit", fill="#111827")
    draw.text((504, 112), "1", fill="#111827")
    image.save(path, format="PNG", optimize=False, compress_level=9)


def source_lines(content: list[dict]) -> list[str]:
    lines: list[str] = []
    for item in content:
        raw_type = item["type"]
        if raw_type in {"text", "equation", "page_footnote"}:
            lines.extend(str(item["text"]).splitlines())
        elif raw_type == "image":
            lines.extend(item["image_caption"])
            lines.extend(item["image_footnote"])
        elif raw_type == "chart":
            lines.extend(item["chart_caption"])
            lines.extend(item["chart_footnote"])
        elif raw_type == "table":
            # The structured HTML body is represented by the table itself rather
            # than duplicated as literal markup in the source PDF text layer.
            lines.extend(item["table_caption"])
            lines.extend(item["table_footnote"])
        elif raw_type == "code":
            lines.extend(item["code_body"].splitlines())
            lines.extend(item.get("code_caption", []))
            lines.extend(item.get("code_footnote", []))
        elif raw_type == "list":
            lines.extend(item["list_items"])
    return lines


def write_native_pdf() -> None:
    native = ROOT / "native"
    content = json.loads(
        (native / "fixture_content_list.json").read_text(encoding="utf-8")
    )
    document = pymupdf.open()
    # Match the fixture middle.json raw 1000 x 1000 page coordinate space.
    page = document.new_page(width=1000.0, height=1000.0)
    y = 38.0
    for logical_line in source_lines(content):
        wrapped = textwrap.wrap(
            logical_line,
            width=105,
            replace_whitespace=False,
            drop_whitespace=False,
        ) or [""]
        for line in wrapped:
            page.insert_text(
                (36.0, y),
                line.rstrip(),
                fontname="helv",
                fontsize=8.5,
                color=(0.08, 0.10, 0.15),
            )
            y += 11.5
        y += 2.5
    document.set_metadata(
        {
            "title": "Original synthetic MinerU adapter fixture",
            "author": "make-bilingual-study-pdf contributors",
            "subject": "Reproducible model-free contract fixture",
            "keywords": "synthetic, MinerU, adapter, test",
            "creator": "tests/fixtures/mineru/generate_fixtures.py",
            "producer": "PyMuPDF",
            "creationDate": "D:20260814000000+08'00'",
            "modDate": "D:20260814000000+08'00'",
        }
    )
    payload = document.tobytes(garbage=4, deflate=True, no_new_id=True)
    document.close()
    source = ROOT / "native-source.pdf"
    source.write_bytes(payload)
    shutil.copyfile(source, native / "fixture_origin.pdf")


def main() -> None:
    write_table_image()
    write_native_pdf()
    print(f"regenerated {ROOT / 'native-source.pdf'}")


if __name__ == "__main__":
    main()
