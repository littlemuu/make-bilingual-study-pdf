#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from audit_source import audit_adapter_source
from common import sha256_file, sha256_text, write_json, write_jsonl
from document_ir import (
    _classify_v2_block,
    build_document_ir,
    expected_ir,
    validate_ir_against_sources,
)
from profile import canonical_profile_sha256, load_profile, profile_contract


SCRIPT_DIR = Path(__file__).resolve().parent


def canonical_item_sha256(value: object) -> str:
    return sha256_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def make_block(
    block_id: str,
    source: str,
    *,
    kind: str,
    role: str,
    pointer: str,
    item_hash: str,
    text_level: int = 0,
) -> dict:
    return {
        "id": block_id,
        "page": 1,
        "bbox": [0.0, 0.0, 1000.0, 100.0],
        "source": source,
        "source_sha256": sha256_text(source),
        "kind": kind,
        "translatable": kind not in {"list", "image", "math", "code"},
        "protected_spans": [],
        "links": [],
        "adapter_role": role,
        "evidence": {
            "adapter": "mineru-import",
            "content_pointer": f"{pointer}/text",
            "content_item_pointer": pointer,
            "content_item_sha256": item_hash,
            "middle_pointer": "/pdf_info/0/para_blocks/0",
            "middle_match": "exact",
            "page_idx": 0,
            "page_order": int(pointer[1:]) + 1,
            "raw_type": "text",
            "raw_sub_type": None,
            "text_level": text_level,
            "bbox_coordinate_system": "mineru-normalized-1000",
        },
    }


class V23IrSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_profile("academic-paper-en-zh")

    def test_specific_patterns_outrank_generic_adapter_fallbacks(self) -> None:
        contract = profile_contract(load_profile("lecture-notes-en-zh"))
        theorem = _classify_v2_block(
            {
                "source": "Theorem 1. Frozen inputs produce stable identities.",
                "kind": "prose",
                "adapter_role": "paragraph",
                "evidence": {"raw_sub_type": None, "text_level": 0},
            },
            contract,
        )
        self.assertEqual(theorem["role"], "theorem")
        footnote = _classify_v2_block(
            {
                "source": "Correspondence: fixture@example.invalid",
                "kind": "prose",
                "adapter_role": "page_footnote",
                "evidence": {"raw_sub_type": None, "text_level": 0},
            },
            contract,
        )
        self.assertEqual(footnote["role"], "page-footnote")
        byline = _classify_v2_block(
            {
                "source": "Ada Example — Department of Reproducible Learning",
                "kind": "prose",
                "adapter_role": "paragraph",
                "evidence": {"raw_sub_type": None, "text_level": 0},
            },
            profile_contract(self.profile),
        )
        self.assertEqual(byline["role"], "author-affiliation")

    def make_work(self, root: Path, *, manual: bool = False) -> tuple[Path, dict, list[dict]]:
        work_dir = root / "work"
        work_dir.mkdir()
        (work_dir / "adapter-inputs").mkdir()
        (work_dir / "adapter-assets").mkdir()
        source_path = work_dir / "source.pdf"
        source_path.write_bytes(b"frozen-source")

        specifications = [
            ("Title", "title", 1, "heading"),
            ("Abstract", "abstract", 0, "prose"),
            ("Introduction", "section", 1, "heading"),
            ("Body paragraph", "paragraph", 0, "prose"),
            ("References", "reference", 0, "list"),
        ]
        content = [
            {
                "type": "text",
                "text": text,
                "text_level": level,
                "page_idx": 0,
                "bbox": [0.0, index * 100.0, 1000.0, (index + 1) * 100.0],
            }
            for index, (text, _role, level, _kind) in enumerate(specifications)
        ]
        blocks = []
        items = []
        for index, (text, role, level, kind) in enumerate(specifications):
            pointer = f"/{index}"
            item_hash = canonical_item_sha256(content[index])
            block_id = f"mineru-{index:04d}"
            block = make_block(
                block_id,
                text,
                kind=kind,
                role=role,
                pointer=pointer,
                item_hash=item_hash,
                text_level=level,
            )
            block["evidence"]["raw_type"] = "text"
            blocks.append(block)
            items.append(
                {
                    "pointer": pointer,
                    "item_sha256": item_hash,
                    "page_idx": 0,
                    "page_order": index + 1,
                    "raw_type": "text",
                    "raw_sub_type": None,
                    "disposition": "emitted",
                    "reason": None,
                    "node_ids": [block_id],
                    "visual_ids": [],
                    "middle_pointers": ["/pdf_info/0/para_blocks/0"],
                    "middle_match": "exact",
                }
            )

        input_values = {
            "origin": source_path.read_bytes(),
            "content": (json.dumps(content, ensure_ascii=False, indent=2) + "\n").encode(),
            "middle": b"{}\n",
        }
        inputs = []
        for role, payload in input_values.items():
            path = work_dir / "adapter-inputs" / f"fixture-{role}.bin"
            path.write_bytes(payload)
            inputs.append(
                {
                    "role": role,
                    "relative_path": f"fixture-{role}.bin",
                    "work_path": path.relative_to(work_dir).as_posix(),
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        # The content input must remain JSON despite its neutral frozen filename.
        content_record = next(item for item in inputs if item["role"] == "content")
        content_path = work_dir / content_record["work_path"]
        content_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        content_record["sha256"] = sha256_file(content_path)
        content_record["size"] = content_path.stat().st_size
        inputs.sort(key=lambda item: (item["role"], item["relative_path"]))

        asset_path = work_dir / "adapter-assets" / "pixel.png"
        Image.new("RGB", (2, 3), "white").save(asset_path)
        assets = [
            {
                "id": "asset-pixel",
                "relative_path": "images/pixel.png",
                "work_path": asset_path.relative_to(work_dir).as_posix(),
                "sha256": sha256_file(asset_path),
                "size": asset_path.stat().st_size,
                "mime": "image/png",
                "width": 2,
                "height": 3,
            }
        ]
        review_pages: list[str] = []
        review_contacts: list[dict] = []
        if manual:
            review_dir = work_dir / "source-review"
            review_dir.mkdir()
            review_path = review_dir / "page-0001.png"
            Image.new("RGB", (2, 3), "white").save(review_path)
            review_pages = [review_path.relative_to(work_dir).as_posix()]
            review_contact_dir = work_dir / "source-review-contact"
            review_contact_dir.mkdir()
            review_contact_path = review_contact_dir / "contact-001.png"
            Image.new("RGB", (2, 3), "white").save(review_contact_path)
            review_contacts = [
                {
                    "path": review_contact_path.relative_to(work_dir).as_posix(),
                    "sha256": sha256_file(review_contact_path),
                    "first_page": 1,
                    "last_page": 1,
                }
            ]
        evidence = {
            "schema_version": 1,
            "adapter": "mineru-import",
            "source": {
                "logical_name": "source.pdf",
                "sha256": sha256_file(source_path),
                "page_count": 1,
            },
            "mineru": {
                "version": "3.4.4",
                "backend": "pipeline",
                "support_level": "verified",
            },
            "inputs": inputs,
            "assets": assets,
            "pages": [
                {
                    "page_idx": 0,
                    "page_size": [612.0, 792.0],
                    "source_page_size": [612.0, 792.0],
                    "native_text_characters": 0 if manual else 59,
                    "adapter_text_characters": 59,
                    "manual_review_reasons": (
                        ["native_oracle_empty"] if manual else []
                    ),
                    "status": (
                        "manual_source_review_required"
                        if manual
                        else "native_oracle_available"
                    ),
                }
            ],
            "items": items,
            "manual_source_review_required": manual,
            "manual_review_pages": [1] if manual else [],
            "manual_review_page_comparisons": review_pages,
            "manual_review_contact_sheets": review_contacts,
        }
        evidence_path = work_dir / "adapter-evidence.json"
        write_json(evidence_path, evidence)
        manifest = {
            "schema_version": 4,
            "profile": {
                "id": self.profile["id"],
                "sha256": canonical_profile_sha256(self.profile),
            },
            "source_pdf": str(source_path),
            "source_sha256": sha256_file(source_path),
            "page_count": 1,
            "adapter": {
                "id": "mineru-import",
                "evidence": "adapter-evidence.json",
                "evidence_sha256": sha256_file(evidence_path),
                "backend": "pipeline",
                "version": "3.4.4",
            },
            "input_artifacts": inputs,
            "external_uris": [],
            "external_uri_count": 0,
            "links": [],
            "visuals": [],
            "source_contact_sheets": [],
            "source_review_pages": review_pages,
            "source_review_contact_sheets": review_contacts,
        }
        write_json(work_dir / "profile.json", self.profile)
        write_json(work_dir / "manifest.json", manifest)
        write_jsonl(work_dir / "blocks.jsonl", blocks)
        return work_dir, manifest, blocks

    def test_v2_selector_semantics_complete_membership_and_full_inventory(self) -> None:
        content = {"type": "text", "text": "Abstract", "page_idx": 0}
        item_hash = canonical_item_sha256(content)
        abstract = make_block(
            "abstract-1",
            "Abstract",
            kind="prose",
            role="abstract",
            pointer="/0",
            item_hash=item_hash,
        )
        paragraph = make_block(
            "paragraph-1",
            "Body",
            kind="prose",
            role="paragraph",
            pointer="/1",
            item_hash=item_hash,
        )
        abstract["evidence"]["structural_membership"] = {
            "status": "complete",
            "member_node_ids": ["abstract-1", "paragraph-1"],
        }
        manifest = {
            "source_pdf": "source.pdf",
            "source_sha256": "a" * 64,
            "page_count": 1,
            "adapter": {"id": "mineru-import"},
            "visuals": [],
        }
        ir = build_document_ir(
            manifest,
            [abstract, paragraph],
            self.profile,
            manifest_sha256="b" * 64,
            blocks_sha256="c" * 64,
            adapter_freeze={"sha256": "d" * 64, "inputs": [], "assets": []},
        )
        self.assertEqual(ir["schema_version"], 2)
        self.assertEqual(
            ir["nodes"][0]["semantic"],
            {
                "role": "abstract",
                "style": "abstract",
                "output": "bilingual",
                "evidence": {
                    "method": "profile-selector",
                    "source_pointer": "/0/text",
                },
            },
        )
        group = next(item for item in ir["semantic_groups"] if item["role"] == "abstract")
        self.assertEqual(group["membership"], "complete")
        self.assertEqual(group["member_node_ids"], ["abstract-1", "paragraph-1"])
        inventory = ir["inventories"]["role_inventory"]
        self.assertEqual(inventory["abstract"]["node_count"], 2)
        self.assertEqual(list(inventory), list(self.profile["qa"]["role_inventory"]))
        self.assertEqual(
            set(inventory["figure"]),
            {
                "occurrence_count",
                "node_count",
                "occurrence_ids",
                "membership_counts",
                "minimum",
                "maximum",
                "style",
                "output",
            },
        )
        self.assertEqual(inventory["figure"]["occurrence_count"], 0)
        self.assertEqual(
            inventory["figure"]["membership_counts"],
            {"none": 0, "anchor-only": 0, "complete": 0},
        )

    def test_unproved_or_invalid_structural_membership_is_anchor_only(self) -> None:
        item_hash = canonical_item_sha256({"type": "text", "text": "Abstract"})
        block = make_block(
            "abstract-1",
            "Abstract",
            kind="prose",
            role="abstract",
            pointer="/0",
            item_hash=item_hash,
        )
        block["evidence"]["structural_membership"] = {
            "status": "complete",
            "member_node_ids": ["unknown"],
        }
        ir = build_document_ir(
            {
                "source_pdf": "source.pdf",
                "source_sha256": "a" * 64,
                "page_count": 1,
                "adapter": {"id": "mineru-import"},
                "visuals": [],
            },
            [block],
            self.profile,
            manifest_sha256="b" * 64,
            blocks_sha256="c" * 64,
            adapter_freeze={"sha256": "d" * 64, "inputs": [], "assets": []},
        )
        self.assertEqual(ir["semantic_groups"][0]["membership"], "anchor-only")
        self.assertEqual(ir["semantic_groups"][0]["member_node_ids"], ["abstract-1"])

    def test_adapter_freeze_dispositions_assets_and_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v23-ir-source-") as temporary:
            work_dir, manifest, blocks = self.make_work(Path(temporary))
            adapter = audit_adapter_source(work_dir, manifest, blocks)
            self.assertEqual(adapter["failures"], [])
            self.assertEqual(adapter["item_count"], 5)
            ir = expected_ir(work_dir, self.profile)
            self.assertEqual(len(ir["source"]["adapter_evidence"]["assets"]), 1)
            write_json(work_dir / "document-ir.json", ir)
            self.assertEqual(validate_ir_against_sources(work_dir, self.profile), [])

            asset_path = work_dir / "adapter-assets" / "pixel.png"
            asset_path.write_bytes(asset_path.read_bytes() + b"drift")
            failures = validate_ir_against_sources(work_dir, self.profile)
            self.assertTrue(any("invalid document IR inputs" in item for item in failures))

    def test_manual_review_is_a_nonpassed_source_audit_status(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v23-ir-manual-") as temporary:
            work_dir, manifest, blocks = self.make_work(Path(temporary), manual=True)
            adapter = audit_adapter_source(work_dir, manifest, blocks)
            self.assertEqual(adapter["failures"], [])
            self.assertTrue(adapter["manual_source_review_required"])

            write_json(work_dir / "document-ir.json", expected_ir(work_dir, self.profile))
            (work_dir / "oracle.txt").write_text(
                "Title Abstract Introduction Body paragraph References\f", encoding="utf-8"
            )
            renders = work_dir / "renders"
            renders.mkdir()
            Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
            contacts = work_dir / "source-contact"
            contacts.mkdir()
            contact_path = contacts / "contact-001.png"
            Image.new("RGB", (2, 3), "white").save(contact_path)
            manifest["source_contact_sheets"] = [
                {
                    "path": contact_path.relative_to(work_dir).as_posix(),
                    "sha256": sha256_file(contact_path),
                    "first_page": 1,
                    "last_page": 1,
                }
            ]
            write_json(work_dir / "manifest.json", manifest)
            # Manifest changed, so regenerate the bound IR before invoking the audit.
            write_json(work_dir / "document-ir.json", expected_ir(work_dir, self.profile))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "audit_source.py"), str(work_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            report = json.loads((work_dir / "source-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "manual_source_review_required")
            self.assertEqual(report["failures"], [])

            review_page = work_dir / manifest["source_review_pages"][0]
            review_page.write_bytes(b"not-a-decodable-image")
            drifted = audit_adapter_source(work_dir, manifest, blocks)
            self.assertTrue(
                any("comparison cannot be fully decoded" in item for item in drifted["failures"])
            )


def main() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(V23IrSourceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(
        json.dumps(
            {
                "status": "passed",
                "tests": result.testsRun,
                "results": [
                    "schema V2 selector semantics and complete membership are deterministic",
                    "specific semantic patterns outrank generic paragraph/heading fallbacks",
                    "unproved structural membership remains anchor-only",
                    "adapter evidence inputs/assets/dispositions freeze the IR",
                    "manual source review remains an explicit nonpassed audit status",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
