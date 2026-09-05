#!/usr/bin/env python3
"""Rebuild the frozen native assignment PDF used by migration tests.

The committed PDF is the test input.  This helper documents how it was authored;
tests never substitute hand-written blocks, oracles, renders, or audit gates for
the production ``pipeline source`` stage.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw


OUTPUT = Path(__file__).with_name("assignment-native-source-expanded.pdf")


def add_text(page: fitz.Page, box: tuple[float, float, float, float], text: str, *, font: str = "helv", size: float = 11) -> None:
    inserted = page.insert_textbox(fitz.Rect(box), text, fontname=font, fontsize=size, color=(0, 0, 0), lineheight=1.2)
    if inserted < 0:
        raise RuntimeError(f"text did not fit: {text!r}")


def main() -> None:
    document = fitz.open()
    page = document.new_page(width=612, height=792)
    add_text(page, (54, 40, 558, 70), "1 Native Assignment Migration Fixture", font="hebo", size=16)
    add_text(page, (54, 88, 558, 118), "Problem (p1): Explain the update rule for the training objective.")
    add_text(page, (54, 130, 558, 160), "The objective combines a data term with a regularization term.")
    add_text(page, (54, 172, 558, 202), "Example (e1): Evaluate the rule when the learning rate is 0.1.")
    add_text(page, (54, 214, 558, 244), "Low-Resource Tip: Cache repeated feature vectors.")
    add_text(page, (54, 256, 558, 286), "- Verify every intermediate value before submission.")
    add_text(page, (54, 302, 558, 346), "for value in values:\n    total += value", font="cour", size=10)
    add_text(page, (54, 362, 558, 406), "f(x) = x^2 + 3x\n(1)", size=12)
    add_text(page, (54, 422, 558, 466), "where x = 2\nand y = 3", size=12)

    image = Image.new("RGB", (180, 80), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((3, 3, 176, 76), outline="black", width=3)
    drawing.line((16, 62, 62, 20, 104, 50, 160, 14), fill="navy", width=4)
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    page.insert_image(fitz.Rect(54, 485, 234, 565), stream=payload.getvalue())

    page.draw_rect(fitz.Rect(300, 485, 500, 565), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(320, 545), fitz.Point(360, 505), color=(0, 0, 0), width=2)
    page.draw_line(fitz.Point(360, 505), fitz.Point(410, 535), color=(0, 0, 0), width=2)
    page.draw_line(fitz.Point(410, 535), fitz.Point(480, 500), color=(0, 0, 0), width=2)
    add_text(page, (320, 510, 480, 535), "Curve label inside graphic", size=10)
    add_text(page, (300, 575, 558, 605), "Figure 1: Deterministic reference curve")
    add_text(page, (300, 598, 558, 628), "The figure is retained once with its caption.")
    add_text(page, (295, 748, 317, 770), "1", size=10)

    metadata = {
        "title": "Native Assignment Migration Fixture",
        "author": "make-bilingual-study-pdf test suite",
        "subject": "Public deterministic native-text PDF fixture",
        "creator": "PyMuPDF",
        "producer": "PyMuPDF",
        "creationDate": "D:20260904000000+00'00'",
        "modDate": "D:20260904000000+00'00'",
    }
    document.set_metadata(metadata)
    document.save(OUTPUT, garbage=4, deflate=True, clean=True, no_new_id=True)
    document.close()
    print(OUTPUT)


if __name__ == "__main__":
    main()
