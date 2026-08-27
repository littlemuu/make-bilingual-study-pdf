#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pymupdf as fitz
from PIL import Image

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = REPOSITORY
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audit_source import current_source_audit_bindings
from adapters.mineru import LARGE_RASTER_PAGE_AREA_RATIO, RASTER_COVERAGE_METHOD
from build_outputs import should_group_paragraphs, source_only_markdown_body
from common import read_json, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl
from document_ir import expected_ir
from profile import canonical_profile_sha256, load_profile, profile_contract


def run_script(name: str, work_dir: Path, *args: str, succeeds: bool = True) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / name), str(work_dir), *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        env=environment,
    )
    if (result.returncode == 0) != succeeds:
        raise AssertionError(
            f"{name} returned {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def make_block(
    block_id: str,
    kind: str,
    source: str,
    top: float,
    *,
    translatable: bool,
    caption_parent: str | None = None,
) -> dict:
    block = {
        "id": block_id,
        "page": 1,
        "bbox": [72.0, top, 500.0, top + 12.0],
        "kind": kind,
        "source": source,
        "source_sha256": sha256_text(source),
        "translatable": translatable,
        "links": [],
        "protected_spans": [],
        "stats": {},
    }
    if caption_parent is not None:
        block["caption_parent"] = caption_parent
    return block


def build_fixture(work_dir: Path) -> tuple[list[dict], list[str]]:
    profile = load_profile("academic-paper-en-zh")
    contract = profile_contract(profile)
    write_json(work_dir / "profile.json", profile)

    blocks = [
        make_block("n-title", "heading", "A Small Paper", 50, translatable=True),
        make_block("n-author", "prose", "Alice Example", 75, translatable=False),
        make_block("n-abstract", "prose", "Abstract: a concise result.", 100, translatable=True),
        make_block("n-section", "heading", "1 Method", 130, translatable=True),
        make_block("n-p1", "prose", "This paragraph continues", 160, translatable=True),
        make_block("n-p2", "prose", "onto its second extracted block.", 176, translatable=True),
        make_block("n-figure", "image", "Figure source visual", 210, translatable=False),
        make_block(
            "n-caption",
            "caption",
            "Figure 1. A compact result.",
            235,
            translatable=True,
            caption_parent="n-figure",
        ),
        make_block("n-references", "list", "References: Example (2026).", 270, translatable=False),
        make_block("n-header", "artifact", "Journal header", 10, translatable=False),
    ]
    adapter_roles = {
        "n-title": "title",
        "n-author": "author",
        "n-abstract": "abstract",
        "n-section": "section",
        "n-p1": "paragraph",
        "n-p2": "paragraph",
        "n-figure": "figure",
        "n-caption": "figure_caption",
        "n-references": "reference",
        "n-header": "header",
    }
    content: list[dict] = []
    evidence_items: list[dict] = []
    for index, block in enumerate(blocks):
        pointer = f"/{index}"
        content_item = {
            "type": block["kind"],
            "sub_type": None,
            "text": block["source"],
            "page_idx": 0,
            "bbox": block["bbox"],
        }
        item_hash = sha256_text(
            json.dumps(
                content_item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        block["adapter_role"] = adapter_roles[block["id"]]
        block["evidence"] = {
            "adapter": contract["adapter"],
            "adapter_role": block["adapter_role"],
            "content_pointer": pointer,
            "content_item_pointer": pointer,
            "content_item_sha256": item_hash,
            "raw_type": block["kind"],
            "raw_sub_type": None,
        }
        content.append(content_item)
        evidence_items.append(
            {
                "pointer": pointer,
                "item_sha256": item_hash,
                "page_idx": 0,
                "page_order": index + 1,
                "raw_type": block["kind"],
                "raw_sub_type": None,
                "disposition": "emitted",
                "reason": None,
                "node_ids": [block["id"]],
                "visual_ids": ["vis-1"] if block["id"] == "n-figure" else [],
                "middle_pointers": [],
                "middle_match": "not-applicable",
            }
        )
    write_jsonl(work_dir / "blocks.jsonl", blocks)

    (work_dir / "visuals").mkdir()
    visual_path = work_dir / "visuals" / "figure.bin"
    Image.new("RGB", (2, 3), "white").save(visual_path, format="PNG")
    (work_dir / "oracle.txt").write_text("\f", encoding="utf-8")
    (work_dir / "oracle-layout.txt").write_text("\f", encoding="utf-8")
    renders = work_dir / "renders"
    renders.mkdir()
    Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
    source_contact = work_dir / "source-contact"
    source_contact.mkdir()
    source_contact_path = source_contact / "contact-1.png"
    Image.new("RGB", (2, 3), "white").save(source_contact_path)
    source_pdf = work_dir / "fixture.pdf"
    source_document = fitz.open()
    source_page = source_document.new_page(width=612, height=792)
    source_page.insert_text(
        (72, 72),
        "A Small Paper by Alice Example. Abstract and method text provide "
        "a deterministic native-text PDF fixture for the complete source freeze chain.",
    )
    source_pdf.write_bytes(
        source_document.tobytes(garbage=4, deflate=True, no_new_id=True)
    )
    source_document.close()
    source_hash = sha256_file(source_pdf)
    adapter_inputs = work_dir / "adapter-inputs"
    adapter_inputs.mkdir()
    origin_path = adapter_inputs / "fixture-origin.pdf"
    origin_path.write_bytes(source_pdf.read_bytes())
    content_path = adapter_inputs / "fixture-content.json"
    write_json(content_path, content)
    middle_path = adapter_inputs / "fixture-middle.json"
    write_json(middle_path, {})
    input_records = []
    for role, path in (
        ("origin", origin_path),
        ("content", content_path),
        ("middle", middle_path),
    ):
        input_records.append(
            {
                "role": role,
                "relative_path": path.name,
                "work_path": path.relative_to(work_dir).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    input_records.sort(key=lambda item: (item["role"], item["relative_path"]))
    asset_records = [
        {
            "id": "asset-figure",
            "relative_path": "images/figure.bin",
            "work_path": visual_path.relative_to(work_dir).as_posix(),
            "sha256": sha256_file(visual_path),
            "size": visual_path.stat().st_size,
            "mime": "image/png",
            "width": 2,
            "height": 3,
        }
    ]
    adapter_evidence = {
        "schema_version": 1,
        "adapter": contract["adapter"],
        "source": {
            "logical_name": source_pdf.name,
            "sha256": source_hash,
            "page_count": 1,
        },
        "mineru": {
            "version": "3.4.4",
            "backend": "pipeline",
            "support_level": "verified",
        },
        "raster_detection": {
            "method": RASTER_COVERAGE_METHOD,
            "large_page_area_ratio": LARGE_RASTER_PAGE_AREA_RATIO,
        },
        "inputs": input_records,
        "assets": asset_records,
        "pages": [
            {
                "page_idx": 0,
                "page_size": [612.0, 792.0],
                "source_page_size": [612.0, 792.0],
                "native_text_characters": 140,
                "adapter_text_characters": sum(len(block["source"]) for block in blocks),
                "raster_image_area_ratio": 0.0,
                "manual_review_reasons": [],
                "status": "native_oracle_available",
            }
        ],
        "items": evidence_items,
        "manual_source_review_required": False,
        "manual_review_pages": [],
        "manual_review_page_comparisons": [],
        "manual_review_contact_sheets": [],
    }
    evidence_path = work_dir / "adapter-evidence.json"
    write_json(evidence_path, adapter_evidence)
    write_json(
        work_dir / "manifest.json",
        {
            "schema_version": 4,
            "source_pdf": str(source_pdf),
            "source_sha256": source_hash,
            "page_count": 1,
            "artifacts": {
                "profile": "profile.json",
                "document_ir": "document-ir.json",
                "adapter_evidence": "adapter-evidence.json",
                "blocks": "blocks.jsonl",
                "oracle": "oracle.txt",
                "oracle_layout": "oracle-layout.txt",
                "renders": "renders/page-*.png",
                "source_contact": "source-contact/contact-*.png",
            },
            "profile": {
                "id": profile["id"],
                "sha256": canonical_profile_sha256(profile),
            },
            "adapter": {
                "id": contract["adapter"],
                "evidence": "adapter-evidence.json",
                "evidence_sha256": sha256_file(evidence_path),
                "backend": "pipeline",
                "version": "3.4.4",
            },
            "input_artifacts": input_records,
            "visuals": [
                {
                    "id": "vis-1",
                    "anchor_id": "n-figure",
                    "path": "visuals/figure.bin",
                    "asset_id": "asset-figure",
                    "sha256": sha256_file(visual_path),
                    "contained_block_ids": [],
                    "caption_continuation_ids": [],
                }
            ],
            "links": [],
            "external_uris": [],
            "external_uri_count": 0,
            "problem_ids": [],
            "source_contact_sheets": [
                {
                    "path": "source-contact/contact-1.png",
                    "sha256": sha256_file(source_contact_path),
                    "first_page": 1,
                    "last_page": 1,
                }
            ],
            "source_review_pages": [],
            "source_review_contact_sheets": [],
        },
    )
    write_json(work_dir / "document-ir.json", expected_ir(work_dir, profile))
    source_bindings = current_source_audit_bindings(work_dir)
    write_json(
        work_dir / "source-audit.json",
        {
            "status": "passed",
            **source_bindings,
            "minimum_global_coverage": profile["qa"][
                "minimum_global_fivegram_coverage"
            ],
            "failures": [],
        },
    )
    translation_dir = work_dir / "translation"
    translation_dir.mkdir()
    write_json(
        translation_dir / "glossary.json",
        {
            "schema_version": 1,
            "profile_id": profile["id"],
            "profile_sha256": canonical_profile_sha256(profile),
            "target_language": "zh-CN",
            "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
            "terms": [],
        },
    )
    expected_translation_ids = [
        "n-title", "n-abstract", "n-section", "n-p1", "n-p2", "n-caption"
    ]
    return blocks, expected_translation_ids


def test_grouping_boundaries(blocks: list[dict]) -> None:
    previous = next(block for block in blocks if block["id"] == "n-p1")
    current = next(block for block in blocks if block["id"] == "n-p2")
    base = {
        "n-p1": {"role": "paragraph", "output": "bilingual", "relations": {}},
        "n-p2": {"role": "paragraph", "output": "bilingual", "relations": {}},
    }
    assert should_group_paragraphs(previous, current, base, {})
    changed_role = {key: dict(value) for key, value in base.items()}
    changed_role["n-p2"]["role"] = "abstract"
    assert not should_group_paragraphs(previous, current, changed_role, {})
    changed_relation = {key: dict(value) for key, value in base.items()}
    changed_relation["n-p2"]["relations"] = {"caption_parent": "n-figure"}
    assert not should_group_paragraphs(previous, current, changed_relation, {})
    assert not should_group_paragraphs(
        previous,
        current,
        base,
        {"n-p1": {"group-a"}, "n-p2": {"group-b"}},
    )


def test_structured_source_only_markdown() -> None:
    table = make_block(
        "n-table",
        "table",
        "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
        "<tbody><tr><td>Coverage</td><td>100%</td></tr></tbody></table>",
        300,
        translatable=False,
    )
    table_body = source_only_markdown_body(table)
    assert table_body.startswith("```{=html}\n<table>")
    assert table_body.endswith("</table>\n```")
    assert "\\<table" not in table_body

    references = make_block(
        "n-reference-list",
        "list",
        "- [1] Alpha (2025).\n- [2] Beta (2026).",
        330,
        translatable=False,
    )
    list_body = source_only_markdown_body(references)
    assert list_body.splitlines() == [
        "- \\[1\\] Alpha (2025).",
        "- \\[2\\] Beta (2026).",
    ]
    assert "Alpha (2025). -" not in list_body


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="v23-output-") as temp:
        work_dir = Path(temp)
        blocks, expected_translation_ids = build_fixture(work_dir)
        test_grouping_boundaries(blocks)
        test_structured_source_only_markdown()

        run_script("prepare_translation.py", work_dir)
        request_rows = []
        for path in sorted((work_dir / "translation" / "requests").glob("part-*.jsonl")):
            request_rows.extend(read_jsonl(path))
        assert [item["id"] for item in request_rows] == expected_translation_ids
        assert all(item["output_disposition"] == "bilingual" for item in request_rows)

        translations = {
            "n-title": "一篇小论文",
            "n-abstract": "摘要：一个简洁的结果。",
            "n-section": "1 方法",
            "n-p1": "本段继续",
            "n-p2": "到第二个提取块。",
            "n-caption": "图 1：一个简洁的结果。",
        }
        plan = read_json(work_dir / "translation" / "plan.json")
        for batch in plan["batches"]:
            requests = read_jsonl(
                work_dir / "translation" / batch["request_file"]
            )
            write_jsonl(
                work_dir / "translation" / batch["response_file"],
                [
                    {
                        "id": item["id"],
                        "source_sha256": item["source_sha256"],
                        "translation": translations[item["id"]]
                        + (
                            " 保留项："
                            + " ".join(
                                token["placeholder"]
                                for token in item.get("protected_tokens", [])
                            )
                            if item.get("protected_tokens")
                            else ""
                        ),
                    }
                    for item in requests
                ],
            )
        run_script("audit_translation.py", work_dir)

        run_script("build_outputs.py", work_dir, "--basename", "fixture")
        run_script("audit_outputs.py", work_dir)
        report = read_json(work_dir / "output" / "output-audit.json")
        assert report["status"] == "passed"
        assert report["role_inventory"]["chart"]["occurrence_count"] == 0
        build = read_json(work_dir / "output" / "build-manifest.json")
        assert build["dispositions"] == {
            "n-title": "bilingual",
            "n-author": "source-only",
            "n-abstract": "bilingual",
            "n-section": "bilingual",
            "n-p1": "bilingual",
            "n-p2": "bilingual",
            "n-figure": "visual-once",
            "n-caption": "bilingual",
            "n-references": "source-only",
            "n-header": "artifact-omitted",
        }
        markdown = (work_dir / "output" / "fixture.md").read_text(encoding="utf-8")
        assert markdown.count("Alice Example") == 1
        assert markdown.count("](assets/figure.bin)") == 1
        assert markdown.index("This paragraph continues") < markdown.index("本段继续")

        build["dispositions"]["n-author"] = "mystery-output"
        write_json(work_dir / "output" / "build-manifest.json", build)
        run_script("audit_outputs.py", work_dir, succeeds=False)
        failed = read_json(work_dir / "output" / "output-audit.json")
        assert any("unknown output dispositions" in item for item in failed["failures"])

    print(
        json.dumps(
            {
                "status": "passed",
                "tests": [
                    "only bilingual translatable semantic nodes enter translation requests",
                    "paragraph grouping cannot cross role, relation, or semantic group",
                    "all four generic dispositions render exactly once or omit explicitly",
                    "structured tables and reference lists retain native Markdown structure",
                    "allowed-zero role inventory passes minimum/maximum audit",
                    "unknown output dispositions fail closed",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
