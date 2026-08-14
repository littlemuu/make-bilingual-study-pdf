#!/usr/bin/env python3
"""Public, model-free contract tests for the V2.3 MinerU import adapter."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import math
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import pymupdf as fitz

from adapters.base import AdapterError
from adapters.mineru import (
    LARGE_RASTER_PAGE_AREA_RATIO,
    SUPPORTED_BACKEND,
    VERIFIED_VERSION,
    adapter_text_exceeds_native_oracle,
    canonical_json_sha256,
    discover_inputs,
    import_mineru,
    load_strict_json,
    normalize_content,
    page_raster_coverage_ratios,
    resolve_asset,
    validate_assets,
    validate_content_bbox,
    validate_middle,
)
from common import normalize_text, sha256_file, sha256_text
from build_outputs import source_only_markdown_body
from profile import load_profile


REPOSITORY = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = (
    REPOSITORY / "tests" / "fixtures" / "mineru" / "pipeline-3.4.4"
)
ORIGINAL_WHICH = shutil.which


def find_poppler_executable(name: str) -> str:
    candidates = [ORIGINAL_WHICH(name)]
    for program_files in (
        Path(r"C:\Program Files"),
        Path(r"C:\Program Files (x86)"),
    ):
        candidates.append(str(program_files / "Calibre2" / "app" / "bin" / f"{name}.exe"))
    candidates.append(ORIGINAL_WHICH(f"{name}.exe"))
    seen: set[str] = set()
    for value in candidates:
        if not value or value in seen:
            continue
        seen.add(value)
        path = Path(value)
        if not path.is_file():
            continue
        try:
            probe = subprocess.run(
                [str(path), "-v"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(path)
    raise RuntimeError(f"no working {name} executable is available")


class MinerUContractTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture_manifest = json.loads(
            (FIXTURE_ROOT / "fixture-manifest.json").read_text(encoding="utf-8")
        )
        cls.profile = load_profile("academic-paper-en-zh")
        cls.poppler = {
            name: find_poppler_executable(name)
            for name in ("pdftoppm", "pdftotext")
        }

    def load_case(self, name: str) -> dict:
        expected = self.fixture_manifest["cases"][name]
        output_dir = FIXTURE_ROOT / expected["output_dir"]
        source = FIXTURE_ROOT / expected["source"]
        inputs = discover_inputs(source, output_dir)
        content = load_strict_json(inputs["content"])
        middle = load_strict_json(inputs["middle"])
        with fitz.open(source) as document:
            page_count = document.page_count
        backend, version, pages, middle_assets = validate_middle(middle, page_count)
        blocks, visuals, dispositions, content_assets = normalize_content(
            content,
            source_sha256=sha256_file(source),
            backend=backend,
            version=version,
            page_count=page_count,
            middle_pages=pages,
            profile=self.profile,
        )
        asset_paths = middle_assets | content_assets
        assets = validate_assets(output_dir, asset_paths)
        normalized = {
            "blocks": blocks,
            "visuals": visuals,
            "dispositions": dispositions,
            "asset_paths": sorted(asset_paths),
        }
        return {
            "expected": expected,
            "output_dir": output_dir,
            "source": source,
            "inputs": inputs,
            "content": content,
            "middle": middle,
            "backend": backend,
            "version": version,
            "pages": pages,
            "blocks": blocks,
            "visuals": visuals,
            "dispositions": dispositions,
            "assets": assets,
            "normalized": normalized,
        }

    def normalize_mutation(
        self,
        content: object,
        *,
        middle: object | None = None,
        version: str | None = None,
    ) -> tuple[list[dict], list[dict], list[dict], set[str]]:
        native = self.load_case("native")
        if middle is None:
            pages = native["pages"]
            backend = native["backend"]
            active_version = version or native["version"]
        else:
            backend, active_version, pages, _assets = validate_middle(middle, 1)
            if version is not None:
                active_version = version
        return normalize_content(
            content,
            source_sha256=sha256_file(native["source"]),
            backend=backend,
            version=active_version,
            page_count=1,
            middle_pages=pages,
            profile=self.profile,
        )

    def guarded_import(
        self, source: Path, output_dir: Path, work_dir: Path
    ) -> dict:
        original_run = subprocess.run

        def reject_mineru(command, *args, **kwargs):
            executable = command[0] if isinstance(command, (list, tuple)) else command
            executable_name = Path(str(executable)).name.casefold()
            if executable_name.startswith("mineru"):
                self.fail(f"the importer attempted to execute MinerU: {command!r}")
            return original_run(command, *args, **kwargs)

        def frozen_which(name: str):
            return self.poppler.get(name) or ORIGINAL_WHICH(name)

        with mock.patch("adapters.mineru.shutil.which", side_effect=frozen_which), mock.patch(
            "adapters.mineru.subprocess.run", side_effect=reject_mineru
        ):
            return import_mineru(
                source,
                output_dir,
                work_dir,
                "academic-paper-en-zh",
                render_dpi=72,
            )

    def make_mixed_native_scan_case(
        self,
        root: Path,
        *,
        ocr_mode: str = "long",
        rotated_partial_scan: bool = False,
    ) -> tuple[Path, Path, int, int]:
        """Create 3 native pages plus one OCR-only image page with a page number."""

        output_dir = root / "mixed-output"
        output_dir.mkdir()
        source = root / "mixed-source.pdf"
        document = fitz.open()
        native_items = [
            [
                {
                    "id": "mixed-title",
                    "type": "text",
                    "text": "Mixed Native and Scanned Evidence",
                    "text_level": 1,
                    "bbox": [50, 50, 950, 100],
                    "page_idx": 0,
                },
                {
                    "id": "mixed-abstract-heading",
                    "type": "text",
                    "text": "Abstract",
                    "text_level": 1,
                    "bbox": [50, 120, 950, 160],
                    "page_idx": 0,
                },
                {
                    "id": "mixed-abstract-body",
                    "type": "text",
                    "text": (
                        "This native abstract supplies an independent Poppler oracle "
                        "with enough substantive English text to prove that the first "
                        "page is native and does not need OCR or manual source review."
                    ),
                    "bbox": [50, 180, 950, 320],
                    "page_idx": 0,
                },
            ],
            [
                {
                    "id": "mixed-section",
                    "type": "text",
                    "text": "1 Native Evidence",
                    "text_level": 1,
                    "bbox": [50, 50, 950, 100],
                    "page_idx": 1,
                },
                {
                    "id": "mixed-body",
                    "type": "text",
                    "text": (
                        "The second native page deliberately contains a complete text "
                        "layer. Independent extraction and adapter content should agree "
                        "on this prose while remaining isolated from every other page."
                    ),
                    "bbox": [50, 130, 950, 300],
                    "page_idx": 1,
                },
            ],
            [
                {
                    "id": "mixed-references-heading",
                    "type": "text",
                    "text": "References",
                    "text_level": 1,
                    "bbox": [50, 50, 950, 100],
                    "page_idx": 2,
                },
                {
                    "id": "mixed-references",
                    "type": "list",
                    "sub_type": "ref_text",
                    "list_items": [
                        "[1] A. Example. Independent native source evidence for mixed documents, 2026.",
                        "[2] B. Example. Page-local scan detection and explicit manual review, 2026.",
                    ],
                    "bbox": [50, 130, 950, 300],
                    "page_idx": 2,
                },
            ],
        ]
        for page_items in native_items:
            page = document.new_page(width=1000, height=1000)
            lines: list[str] = []
            for item in page_items:
                if item["type"] == "list":
                    lines.extend(item["list_items"])
                else:
                    lines.append(item["text"])
            inserted = page.insert_textbox(
                fitz.Rect(50, 50, 950, 900),
                "\n\n".join(lines),
                fontname="helv",
                fontsize=12,
            )
            self.assertGreaterEqual(inserted, 0)

        scanned_page_width = 612 if rotated_partial_scan else 1000
        scanned_page_height = 792 if rotated_partial_scan else 1000
        scanned_page = document.new_page(
            width=scanned_page_width, height=scanned_page_height
        )
        scan_rect = (
            fitz.Rect(0, 0, scanned_page_width * 0.6, scanned_page_height)
            if rotated_partial_scan
            else scanned_page.rect
        )
        scanned_page.insert_image(
            scan_rect,
            filename=str(FIXTURE_ROOT / "scan" / "images" / "scanned-page.png"),
        )
        scanned_page.insert_text(
            (scanned_page_width - 30, scanned_page_height - 12),
            "4",
            fontname="helv",
            fontsize=10,
        )
        if rotated_partial_scan:
            scanned_page.set_rotation(90)
        source.write_bytes(document.tobytes(garbage=4, deflate=True, no_new_id=True))
        document.close()
        shutil.copyfile(source, output_dir / "mixed_origin.pdf")

        ocr_seed = (
            "MinerU OCR recovered substantive English from the scanned fourth page, "
            "but those characters do not exist in the independent Poppler text layer. "
        )
        if ocr_mode == "short":
            normalized_ocr_text = (ocr_seed * 2)[:98]
            normalized_ocr_text = normalized_ocr_text.rstrip()
            normalized_ocr_text += "x" * (98 - len(normalized_ocr_text))
            ocr_text = normalized_ocr_text.replace(" ", "  ", 1)
            self.assertEqual(len(ocr_text), 99)
            self.assertEqual(len(normalize_text(ocr_text)), 98)
        elif ocr_mode == "long":
            ocr_text = (ocr_seed * 5)[:495]
            self.assertEqual(len(ocr_text), 495)
        elif ocr_mode == "minimal":
            ocr_text = "x"
        elif ocr_mode == "none":
            ocr_text = ""
        else:
            self.fail(f"unknown mixed-scan OCR mode: {ocr_mode}")
        content = [item for page_items in native_items for item in page_items]
        if ocr_text:
            content.append(
                {
                    "id": "mixed-ocr-body",
                    "type": "text",
                    "text": ocr_text,
                    "bbox": [50, 80, 950, 900],
                    "page_idx": 3,
                }
            )
        middle_pages = []
        for page_index in range(4):
            page_items = [item for item in content if item["page_idx"] == page_index]
            blocks = [
                {
                    "type": (
                        "title"
                        if item["type"] == "text" and item.get("text_level", 0) > 0
                        else "ref_text"
                        if item["type"] == "list"
                        else "text"
                    ),
                    "bbox": item["bbox"],
                }
                for item in page_items
            ]
            middle_pages.append(
                {
                    "page_idx": page_index,
                    "page_size": (
                        [612, 792]
                        if rotated_partial_scan and page_index == 3
                        else [1000, 1000]
                    ),
                    "preproc_blocks": [],
                    "para_blocks": blocks,
                    "discarded_blocks": [],
                }
            )
        (output_dir / "mixed_content_list.json").write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "mixed_middle.json").write_text(
            json.dumps(
                {
                    "pdf_info": middle_pages,
                    "_backend": SUPPORTED_BACKEND,
                    "_version_name": VERIFIED_VERSION,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return source, output_dir, len(ocr_text), len(normalize_text(ocr_text))

    def test_fixture_hashes_and_origin_binding(self) -> None:
        for relative, expected_hash in self.fixture_manifest["files"].items():
            path = FIXTURE_ROOT / relative
            self.assertTrue(path.is_file(), relative)
            self.assertEqual(sha256_file(path), expected_hash, relative)
        generator = self.fixture_manifest["generator"]
        generator_path = REPOSITORY / generator["path"]
        self.assertEqual(sha256_file(generator_path), generator["sha256"])
        for name in ("native", "scan"):
            case = self.load_case(name)
            self.assertEqual(
                sha256_file(case["source"]),
                sha256_file(case["inputs"]["origin"]),
            )
        self.assertEqual(
            self.fixture_manifest["mineru_contract"]["version"], VERIFIED_VERSION
        )
        self.assertEqual(
            self.fixture_manifest["mineru_contract"]["backend"], SUPPORTED_BACKEND
        )

    def test_input_discovery_fails_closed(self) -> None:
        native = self.load_case("native")
        with tempfile.TemporaryDirectory(prefix="v23-mineru-discovery-") as temporary:
            root = Path(temporary)
            output = root / "output"
            shutil.copytree(native["output_dir"], output)
            source = root / "source.pdf"
            shutil.copyfile(native["source"], source)

            middle = output / "fixture_middle.json"
            middle.unlink()
            with self.assertRaises(AdapterError):
                discover_inputs(source, output)
            shutil.copyfile(native["inputs"]["middle"], middle)

            content = output / "fixture_content_list.json"
            content.unlink()
            with self.assertRaises(AdapterError):
                discover_inputs(source, output)
            shutil.copyfile(native["inputs"]["content"], content)

            extra = output / "ambiguous_content_list.json"
            shutil.copyfile(content, extra)
            with self.assertRaises(AdapterError):
                discover_inputs(source, output)
            extra.unlink()

            shutil.copyfile(FIXTURE_ROOT / "scan-source.pdf", output / "fixture_origin.pdf")
            with self.assertRaises(AdapterError):
                discover_inputs(source, output)

    def test_normalization_is_deterministic(self) -> None:
        first = self.load_case("native")
        second = self.load_case("native")
        self.assertEqual(first["normalized"], second["normalized"])
        self.assertEqual(
            canonical_json_sha256(first["normalized"]),
            first["expected"]["normalized_contract_sha256"],
        )
        ids = [block["id"] for block in first["blocks"]]
        self.assertEqual(len(ids), len(set(ids)))
        for block in first["blocks"]:
            self.assertEqual(block["source_sha256"], sha256_text(block["source"]))

    def test_types_pointers_dispositions_and_coordinates(self) -> None:
        case = self.load_case("native")
        expected = case["expected"]
        self.assertEqual(len(case["content"]), expected["content_items"])
        self.assertEqual(len(case["blocks"]), expected["normalized_blocks"])
        self.assertEqual(len(case["visuals"]), expected["visual_requests"])
        self.assertEqual(len(case["assets"]), expected["validated_assets"])
        self.assertEqual(
            dict(Counter(item["disposition"] for item in case["dispositions"])),
            expected["disposition_counts"],
        )
        self.assertEqual(
            dict(Counter(item["middle_match"] for item in case["dispositions"])),
            expected["middle_match_counts"],
        )
        self.assertEqual(
            sorted({item["raw_type"] for item in case["dispositions"]}),
            expected["raw_types"],
        )
        self.assertEqual(
            [item["pointer"] for item in case["dispositions"]],
            [f"/{index}" for index in range(len(case["content"]))],
        )
        for index, disposition in enumerate(case["dispositions"]):
            self.assertEqual(disposition["middle_match"], "exact")
            self.assertEqual(len(disposition["middle_pointers"]), 1)
            self.assertTrue(
                disposition["middle_pointers"][0].startswith(
                    "/pdf_info/0/para_blocks/"
                )
            )
            self.assertTrue(disposition["node_ids"] or disposition["visual_ids"])
            self.assertEqual(
                disposition["item_sha256"],
                canonical_json_sha256(case["content"][index]),
            )
        artifact_types = {
            item["raw_type"]
            for item in case["dispositions"]
            if item["disposition"] == "artifact_omitted"
        }
        self.assertEqual(
            artifact_types, {"header", "footer", "page_number", "aside_text"}
        )
        self.assertEqual(
            {
                item["raw_sub_type"]
                for item in case["dispositions"]
                if item["raw_type"] == "code"
            },
            {"code", "algorithm"},
        )
        for content_item, middle_item in zip(
            case["content"], case["pages"][0]["blocks"]
        ):
            self.assertEqual(middle_item["raw_bbox"], content_item["bbox"])
            self.assertEqual(middle_item["normalized_bbox"], content_item["bbox"])
        for block in case["blocks"]:
            evidence = block["evidence"]
            self.assertEqual(evidence["adapter"], "mineru-import")
            self.assertEqual(evidence["middle_match"], "exact")
            self.assertEqual(
                evidence["bbox_coordinate_system"], "mineru-normalized-1000"
            )

    def test_official_dual_field_table_preserves_structure_and_visual(self) -> None:
        case = self.load_case("native")
        table_index, table_item = next(
            (index, item)
            for index, item in enumerate(case["content"])
            if item["type"] == "table"
        )
        self.assertEqual(table_item["img_path"], "images/table.png")
        self.assertTrue(table_item["table_body"].startswith("<html><body><table>"))

        disposition = case["dispositions"][table_index]
        self.assertEqual(len(disposition["node_ids"]), 4)
        self.assertEqual(len(disposition["visual_ids"]), 1)
        blocks = {
            block["id"]: block
            for block in case["blocks"]
            if block["id"] in disposition["node_ids"]
        }
        structured = next(
            block for block in blocks.values() if block["adapter_role"] == "table"
        )
        visual_node = next(
            block
            for block in blocks.values()
            if block["adapter_role"] == "table_visual"
        )
        self.assertEqual(structured["kind"], "table")
        self.assertTrue(structured["source"].startswith("<table>"))
        self.assertNotIn("<html", structured["source"].lower())
        self.assertEqual(visual_node["kind"], "image")
        captions = [
            block
            for block in blocks.values()
            if block["adapter_role"] in {"table_caption", "table_footnote"}
        ]
        self.assertEqual(len(captions), 2)
        self.assertTrue(
            all(block["caption_parent"] == structured["id"] for block in captions)
        )
        visual = next(
            item for item in case["visuals"] if item["id"] in disposition["visual_ids"]
        )
        self.assertEqual(visual["anchor_id"], visual_node["id"])
        self.assertEqual(visual["caption_id"], captions[0]["id"])
        self.assertEqual(visual["source_asset"], "images/table.png")
        markdown = source_only_markdown_body(structured)
        self.assertTrue(markdown.startswith("```{=html}\n<table>"))
        self.assertTrue(markdown.endswith("</table>\n```"))

    def test_backend_version_page_and_bbox_fail_closed(self) -> None:
        middle = self.load_case("native")["middle"]
        for backend in (None, "vlm", "office"):
            mutated = copy.deepcopy(middle)
            mutated["_backend"] = backend
            with self.subTest(backend=backend), self.assertRaises(AdapterError):
                validate_middle(mutated, 1)
        for version in (None, "3.4", "3.4.4-alpha", "2.5.4", "4.0.0"):
            mutated = copy.deepcopy(middle)
            mutated["_version_name"] = version
            with self.subTest(version=version), self.assertRaises(AdapterError):
                validate_middle(mutated, 1)
        compatible = copy.deepcopy(middle)
        compatible["_version_name"] = "3.9.9"
        self.assertEqual(validate_middle(compatible, 1)[1], "3.9.9")

        for bbox in (
            None,
            [0, 0, 10],
            [0.0, 0, 10, 10],
            [True, 0, 10, 10],
            [-1, 0, 10, 10],
            [0, 0, 1001, 10],
            [10, 0, 10, 10],
            [0, 10, 10, 10],
        ):
            with self.subTest(bbox=bbox), self.assertRaises(AdapterError):
                validate_content_bbox(bbox, "/fixture")

        content = self.load_case("native")["content"]
        for page_idx in (-1, 1, 0.5, True):
            mutated = copy.deepcopy(content)
            mutated[0]["page_idx"] = page_idx
            with self.subTest(page_idx=page_idx), self.assertRaises(AdapterError):
                self.normalize_mutation(mutated)

        for page_size in ([0, 1000], [1000], [math.inf, 1000], [True, 1000]):
            mutated = copy.deepcopy(middle)
            mutated["pdf_info"][0]["page_size"] = page_size
            with self.subTest(page_size=page_size), self.assertRaises(AdapterError):
                validate_middle(mutated, 1)

    def test_strict_json_rejects_corruption(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v23-mineru-json-") as temporary:
            root = Path(temporary)
            cases = {
                "broken.json": b'{"unterminated":',
                "bom.json": b"\xef\xbb\xbf{}",
                "duplicate.json": b'{"same": 1, "same": 2}',
                "nan.json": b'{"value": NaN}',
                "utf8.json": b'\xff',
            }
            for name, payload in cases.items():
                path = root / name
                path.write_bytes(payload)
                with self.subTest(name=name), self.assertRaises(AdapterError):
                    load_strict_json(path)
            with self.assertRaises(AdapterError):
                load_strict_json(root / "missing.json")

    def test_unknown_types_subtypes_and_duplicate_ids_fail_closed(self) -> None:
        content = self.load_case("native")["content"]
        unknown_type = copy.deepcopy(content)
        unknown_type[0]["type"] = "unknown-layout-object"
        with self.assertRaises(AdapterError):
            self.normalize_mutation(unknown_type)

        for raw_type, bad_subtype in (
            ("code", "pseudocode"),
            ("list", "text"),
            ("text", "paragraph"),
            ("equation", "display"),
            ("table", "grid"),
        ):
            mutated = copy.deepcopy(content)
            item = next(entry for entry in mutated if entry["type"] == raw_type)
            item["sub_type"] = bad_subtype
            with self.subTest(raw_type=raw_type), self.assertRaises(AdapterError):
                self.normalize_mutation(mutated)

        opaque_visual = copy.deepcopy(content)
        image_item = next(entry for entry in opaque_visual if entry["type"] == "image")
        image_item["sub_type"] = "synthetic-opaque-visual-kind"
        _blocks, _visuals, dispositions, _assets = self.normalize_mutation(opaque_visual)
        image_disposition = next(
            item for item in dispositions if item["raw_type"] == "image"
        )
        self.assertEqual(
            image_disposition["raw_sub_type"], "synthetic-opaque-visual-kind"
        )

        unsafe_table = copy.deepcopy(content)
        table_item = next(entry for entry in unsafe_table if entry["type"] == "table")
        table_item["table_body"] = (
            "<table><tr><td>safe<script>bad()</script></td></tr></table>"
        )
        table_item.pop("img_path", None)
        with self.assertRaises(AdapterError):
            self.normalize_mutation(unsafe_table)

        unsafe_wrapper = copy.deepcopy(content)
        table_item = next(entry for entry in unsafe_wrapper if entry["type"] == "table")
        table_item["table_body"] = (
            "<html><head><script>bad()</script></head><body>"
            "<table><tr><td>safe</td></tr></table></body></html>"
        )
        with self.assertRaises(AdapterError):
            self.normalize_mutation(unsafe_wrapper)

        duplicate_id = copy.deepcopy(content)
        duplicate_id[1]["id"] = duplicate_id[0]["id"]
        with self.assertRaises(AdapterError):
            self.normalize_mutation(duplicate_id)

        reversed_pages = copy.deepcopy(content[:2])
        reversed_pages[0]["page_idx"] = 1
        reversed_pages[1]["page_idx"] = 0
        native = self.load_case("native")
        with self.assertRaises(AdapterError):
            normalize_content(
                reversed_pages,
                source_sha256=sha256_file(native["source"]),
                backend=native["backend"],
                version=native["version"],
                page_count=2,
                middle_pages=[native["pages"][0], native["pages"][0]],
                profile=self.profile,
            )

    def test_asset_paths_fail_closed(self) -> None:
        output_dir = self.load_case("native")["output_dir"]
        relative, resolved = resolve_asset(output_dir, "images/diagram.png")
        self.assertEqual(relative, "images/diagram.png")
        self.assertTrue(resolved.is_file())
        unsafe = (
            "",
            "/tmp/diagram.png",
            "../diagram.png",
            "images/../diagram.png",
            "C:/Windows/diagram.png",
            r"C:\Windows\diagram.png",
            r"images\diagram.png",
        )
        for value in unsafe:
            with self.subTest(value=value), self.assertRaises(AdapterError):
                resolve_asset(output_dir, value)

        with tempfile.TemporaryDirectory(prefix="v23-mineru-symlink-") as temporary:
            root = Path(temporary)
            outside = root / "outside.png"
            shutil.copyfile(output_dir / "images" / "diagram.png", outside)
            fixture_dir = root / "fixture"
            fixture_dir.mkdir()
            link = fixture_dir / "escape.png"
            try:
                link.symlink_to(outside)
            except OSError:
                return
            with self.assertRaises(AdapterError):
                resolve_asset(fixture_dir, "escape.png")

    def test_asset_decode_hash_and_corruption_fail_closed(self) -> None:
        case = self.load_case("native")
        self.assertEqual(len(case["assets"]), 2)
        assets = {asset["relative_path"]: asset for asset in case["assets"]}
        self.assertEqual(set(assets), {"images/diagram.png", "images/table.png"})
        for relative, asset in assets.items():
            self.assertEqual(asset["mime"], "image/png")
            self.assertGreater(asset["width"], 0)
            self.assertGreater(asset["height"], 0)
            self.assertEqual(
                asset["sha256"],
                self.fixture_manifest["files"][f"native/{relative}"],
            )

        with tempfile.TemporaryDirectory(prefix="v23-mineru-assets-") as temporary:
            output = Path(temporary)
            images = output / "images"
            images.mkdir()
            payload = (case["output_dir"] / "images" / "diagram.png").read_bytes()
            (images / "truncated.png").write_bytes(payload[: len(payload) // 2])
            with self.assertRaises(AdapterError):
                validate_assets(output, {"images/truncated.png"})
            with self.assertRaises(AdapterError):
                validate_assets(output, {"images/missing.png"})

    def test_full_import_is_deterministic_without_running_mineru(self) -> None:
        case = self.load_case("native")
        with tempfile.TemporaryDirectory(prefix="v23-mineru-import-") as temporary:
            root = Path(temporary)
            first = self.guarded_import(
                case["source"], case["output_dir"], root / "first"
            )
            second = self.guarded_import(
                case["source"], case["output_dir"], root / "second"
            )
            self.assertEqual(first["status"], case["expected"]["expected_import_status"])
            self.assertEqual(second["status"], first["status"])
            first_ir = (root / "first" / "document-ir.json").read_bytes()
            second_ir = (root / "second" / "document-ir.json").read_bytes()
            self.assertEqual(first_ir, second_ir)
            self.assertEqual(sha256_text(first_ir.decode("utf-8")), sha256_text(second_ir.decode("utf-8")))
            evidence = json.loads(
                (root / "first" / "adapter-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["mineru"]["version"], VERIFIED_VERSION)
            self.assertEqual(evidence["mineru"]["backend"], SUPPORTED_BACKEND)
            self.assertEqual(len(evidence["items"]), case["expected"]["content_items"])
            self.assertFalse(evidence["manual_source_review_required"])
            expected_inputs = {
                (
                    role,
                    Path(path).relative_to(case["output_dir"]).as_posix(),
                    sha256_file(path),
                )
                for role, path in (
                    ("origin", case["inputs"]["origin"]),
                    ("content", case["inputs"]["content"]),
                    ("middle", case["inputs"]["middle"]),
                )
            }
            actual_inputs = {
                (item["role"], item["relative_path"], item["sha256"])
                for item in evidence["inputs"]
            }
            self.assertEqual(actual_inputs, expected_inputs)
            self.assertEqual(
                {item["sha256"] for item in evidence["assets"]},
                {item["sha256"] for item in case["assets"]},
            )
            previous_argv = sys.argv
            try:
                sys.argv = ["audit_source.py", str(root / "first")]
                with contextlib.redirect_stdout(io.StringIO()):
                    runpy.run_path(
                        str(REPOSITORY / "scripts" / "audit_source.py"),
                        run_name="__main__",
                    )
            finally:
                sys.argv = previous_argv
            source_audit = json.loads(
                (root / "first" / "source-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(source_audit["status"], "passed")
            self.assertGreaterEqual(source_audit["global_fivegram_coverage"], 0.95)

    def test_scanned_source_cannot_be_reported_passed(self) -> None:
        case = self.load_case("scan")
        self.assertEqual(
            canonical_json_sha256(case["normalized"]),
            case["expected"]["normalized_contract_sha256"],
        )
        with tempfile.TemporaryDirectory(prefix="v23-mineru-scan-") as temporary:
            work_dir = Path(temporary) / "work"
            result = self.guarded_import(case["source"], case["output_dir"], work_dir)
            self.assertEqual(
                result["status"], case["expected"]["expected_import_status"]
            )
            self.assertNotEqual(result["status"], "passed")
            self.assertEqual(result["manual_review_pages"], [1])
            evidence = json.loads(
                (work_dir / "adapter-evidence.json").read_text(encoding="utf-8")
            )
            self.assertTrue(evidence["manual_source_review_required"])
            self.assertEqual(evidence["manual_review_pages"], [1])
            self.assertEqual(
                evidence["pages"][0]["status"], "manual_source_review_required"
            )
            self.assertFalse((work_dir / "source-audit.json").exists())

    def test_mixed_scan_page_cannot_hide_behind_document_native_ratio(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v23-mineru-mixed-scan-") as temporary:
            root = Path(temporary)
            source, output_dir, raw_ocr_length, adapter_ocr_length = (
                self.make_mixed_native_scan_case(root)
            )
            work_dir = root / "work"
            result = self.guarded_import(source, output_dir, work_dir)
            self.assertEqual(result["status"], "manual_source_review_required")
            self.assertEqual(result["manual_review_pages"], [4])

            evidence = json.loads(
                (work_dir / "adapter-evidence.json").read_text(encoding="utf-8")
            )
            self.assertEqual(evidence["manual_review_pages"], [4])
            first_three = evidence["pages"][:3]
            self.assertTrue(
                all(page["status"] == "native_oracle_available" for page in first_three)
            )
            scanned = evidence["pages"][3]
            self.assertLess(scanned["native_text_characters"], 100)
            self.assertEqual(raw_ocr_length, 495)
            self.assertEqual(
                scanned["adapter_text_characters"], adapter_ocr_length
            )
            self.assertIn(
                "adapter_text_without_native_oracle",
                scanned["manual_review_reasons"],
            )
            manifest = json.loads(
                (work_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["native_text_page_ratio"], 0.75)

            completed = subprocess.run(
                [sys.executable, str(REPOSITORY / "scripts" / "audit_source.py"), str(work_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            report = json.loads(
                (work_dir / "source-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "manual_source_review_required")
            self.assertEqual(report["failures"], [])

    def test_short_mixed_scan_page_cannot_hide_below_native_threshold(self) -> None:
        self.assertTrue(adapter_text_exceeds_native_oracle(1, 98, 100))
        with tempfile.TemporaryDirectory(
            prefix="v23-mineru-short-mixed-scan-"
        ) as temporary:
            root = Path(temporary)
            source, output_dir, raw_ocr_length, adapter_ocr_length = (
                self.make_mixed_native_scan_case(root, ocr_mode="short")
            )
            self.assertEqual(raw_ocr_length, 99)
            self.assertEqual(adapter_ocr_length, 98)

            work_dir = root / "work"
            result = self.guarded_import(source, output_dir, work_dir)
            self.assertEqual(result["status"], "manual_source_review_required")
            self.assertEqual(result["manual_review_pages"], [4])

            evidence = json.loads(
                (work_dir / "adapter-evidence.json").read_text(encoding="utf-8")
            )
            scanned = evidence["pages"][3]
            self.assertEqual(scanned["native_text_characters"], 1)
            self.assertEqual(scanned["adapter_text_characters"], 98)
            self.assertEqual(scanned["raster_image_area_ratio"], 1.0)
            self.assertIn(
                "large_raster_without_native_oracle",
                scanned["manual_review_reasons"],
            )
            self.assertIn(
                "adapter_text_without_native_oracle",
                scanned["manual_review_reasons"],
            )
            manifest = json.loads(
                (work_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["native_text_page_ratio"], 0.75)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY / "scripts" / "audit_source.py"),
                    str(work_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            report = json.loads(
                (work_dir / "source-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "manual_source_review_required")
            self.assertEqual(report["failures"], [])

    def test_large_raster_scan_requires_review_with_minimal_or_no_ocr(self) -> None:
        for ocr_mode, expected_adapter_length in (("minimal", 1), ("none", 0)):
            with self.subTest(ocr_mode=ocr_mode), tempfile.TemporaryDirectory(
                prefix=f"v23-mineru-{ocr_mode}-ocr-scan-"
            ) as temporary:
                root = Path(temporary)
                source, output_dir, raw_ocr_length, adapter_ocr_length = (
                    self.make_mixed_native_scan_case(root, ocr_mode=ocr_mode)
                )
                self.assertEqual(raw_ocr_length, expected_adapter_length)
                self.assertEqual(adapter_ocr_length, expected_adapter_length)
                with fitz.open(source) as source_document:
                    self.assertEqual(
                        page_raster_coverage_ratios(source_document),
                        [0.0, 0.0, 0.0, 1.0],
                    )

                work_dir = root / "work"
                result = self.guarded_import(source, output_dir, work_dir)
                self.assertEqual(result["status"], "manual_source_review_required")
                self.assertEqual(result["manual_review_pages"], [4])

                evidence = json.loads(
                    (work_dir / "adapter-evidence.json").read_text(encoding="utf-8")
                )
                scanned = evidence["pages"][3]
                self.assertEqual(scanned["native_text_characters"], 1)
                self.assertEqual(
                    scanned["adapter_text_characters"], expected_adapter_length
                )
                self.assertEqual(scanned["raster_image_area_ratio"], 1.0)
                self.assertGreaterEqual(
                    scanned["raster_image_area_ratio"],
                    LARGE_RASTER_PAGE_AREA_RATIO,
                )
                self.assertEqual(
                    scanned["manual_review_reasons"],
                    ["large_raster_without_native_oracle"],
                )
                manifest = json.loads(
                    (work_dir / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["native_text_page_ratio"], 0.75)

                completed = subprocess.run(
                    [
                        sys.executable,
                        str(REPOSITORY / "scripts" / "audit_source.py"),
                        str(work_dir),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                report = json.loads(
                    (work_dir / "source-audit.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    report["status"], "manual_source_review_required"
                )
                self.assertEqual(report["failures"], [])

    def test_rotated_large_raster_uses_rotated_page_coordinates(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="v23-mineru-rotated-partial-scan-"
        ) as temporary:
            root = Path(temporary)
            source, output_dir, raw_ocr_length, adapter_ocr_length = (
                self.make_mixed_native_scan_case(
                    root, ocr_mode="none", rotated_partial_scan=True
                )
            )
            self.assertEqual(raw_ocr_length, 0)
            self.assertEqual(adapter_ocr_length, 0)
            with fitz.open(source) as source_document:
                scanned_page = source_document[3]
                self.assertEqual(scanned_page.rotation, 90)
                self.assertEqual(
                    [scanned_page.rect.width, scanned_page.rect.height],
                    [792.0, 612.0],
                )
                self.assertEqual(
                    page_raster_coverage_ratios(source_document),
                    [0.0, 0.0, 0.0, 0.6],
                )

            work_dir = root / "work"
            result = self.guarded_import(source, output_dir, work_dir)
            self.assertEqual(result["status"], "manual_source_review_required")
            self.assertEqual(result["manual_review_pages"], [4])

            evidence = json.loads(
                (work_dir / "adapter-evidence.json").read_text(encoding="utf-8")
            )
            scanned = evidence["pages"][3]
            self.assertEqual(scanned["native_text_characters"], 1)
            self.assertEqual(scanned["adapter_text_characters"], 0)
            self.assertEqual(scanned["raster_image_area_ratio"], 0.6)
            self.assertEqual(
                scanned["manual_review_reasons"],
                ["large_raster_without_native_oracle"],
            )
            manifest = json.loads(
                (work_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["native_text_page_ratio"], 0.75)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY / "scripts" / "audit_source.py"),
                    str(work_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            report = json.loads(
                (work_dir / "source-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "manual_source_review_required")
            self.assertEqual(report["failures"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
