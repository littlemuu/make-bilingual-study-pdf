#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from common import sha256_file


def image_ink_ratio(path: Path) -> float:
    with Image.open(path) as image:
        grayscale = image.convert("L")
        histogram = grayscale.histogram()
        nonwhite = sum(histogram[:248])
        return nonwhite / max(1, image.width * image.height)


def make_contact_sheets(
    page_paths: list[Path], contact_dir: Path, columns: int = 4, rows: int = 4
) -> list[dict[str, Any]]:
    contact_dir.mkdir(exist_ok=True)
    for old in contact_dir.glob("contact-*.png"):
        old.unlink()
    thumb_width = 210
    label_height = 24
    gap = 12
    per_sheet = columns * rows
    results: list[dict[str, Any]] = []
    for sheet_index in range(0, len(page_paths), per_sheet):
        group = page_paths[sheet_index : sheet_index + per_sheet]
        thumbnails: list[Image.Image] = []
        for page_number, path in enumerate(group, start=sheet_index + 1):
            with Image.open(path) as original:
                image = original.convert("RGB")
                height = round(image.height * thumb_width / image.width)
                image.thumbnail((thumb_width, height), Image.Resampling.LANCZOS)
                tile = Image.new("RGB", (thumb_width, height + label_height), "white")
                tile.paste(image, ((thumb_width - image.width) // 2, label_height))
                ImageDraw.Draw(tile).text((6, 5), f"Page {page_number}", fill="black")
                thumbnails.append(tile)
        tile_height = max(tile.height for tile in thumbnails)
        actual_columns = min(columns, len(thumbnails))
        actual_rows = math.ceil(len(thumbnails) / actual_columns)
        canvas = Image.new(
            "RGB",
            (
                actual_columns * thumb_width + (actual_columns + 1) * gap,
                actual_rows * tile_height + (actual_rows + 1) * gap,
            ),
            "#d9dde5",
        )
        for tile_index, tile in enumerate(thumbnails):
            x = gap + (tile_index % actual_columns) * (thumb_width + gap)
            y = gap + (tile_index // actual_columns) * (tile_height + gap)
            bordered = ImageOps.expand(tile, border=1, fill="#8b93a1")
            canvas.paste(bordered, (x, y))
        filename = f"contact-{sheet_index // per_sheet + 1:03d}.png"
        path = contact_dir / filename
        canvas.save(path)
        results.append(
            {
                "path": f"{contact_dir.name}/{filename}",
                "sha256": sha256_file(path),
                "first_page": sheet_index + 1,
                "last_page": sheet_index + len(group),
            }
        )
    return results
