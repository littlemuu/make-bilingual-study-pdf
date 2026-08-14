#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from build_outputs import should_group_paragraphs, source_only_markdown_body
from common import read_json, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl
from profile import canonical_profile_sha256, load_profile, profile_contract


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def run_script(name: str, work_dir: Path, *args: str, succeeds: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / name), str(work_dir), *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
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
    write_jsonl(work_dir / "blocks.jsonl", blocks)

    roles = {
        "n-title": "title",
        "n-author": "author-affiliation",
        "n-abstract": "abstract",
        "n-section": "section",
        "n-p1": "paragraph",
        "n-p2": "paragraph",
        "n-figure": "figure",
        "n-caption": "figure-caption",
        "n-references": "references",
        "n-header": "page-header",
    }
    role_policies = {item["role"]: item for item in contract["roles"]}
    nodes = []
    for block in blocks:
        role = roles[block["id"]]
        policy = role_policies.get(role)
        output = (
            policy["output"]
            if policy is not None
            else contract["auxiliary_dispositions"][role]
        )
        relations = {}
        if block["id"] == "n-caption":
            relations["caption_parent"] = "n-figure"
        nodes.append(
            {
                "id": block["id"],
                "type": block["kind"],
                "location": {"page": 1, "bbox": block["bbox"]},
                "source": {
                    "text": block["source"],
                    "sha256": block["source_sha256"],
                },
                "translatable": block["translatable"],
                "semantic": {
                    "role": role,
                    "style": policy["style"] if policy is not None else None,
                    "output": output,
                    "evidence": {
                        "method": "fixture",
                        "source_pointer": f"blocks.jsonl#{block['id']}",
                    },
                },
                "relations": relations,
            }
        )

    counts = Counter(roles[node["id"]] for node in nodes)
    inventory = {}
    for role, declared in contract["role_inventory"].items():
        occurrence_ids = [node["id"] for node in nodes if roles[node["id"]] == role]
        count = counts.get(role, 0)
        inventory[role] = {
            "occurrence_count": count,
            "node_count": count,
            "occurrence_ids": occurrence_ids,
            "membership_counts": {"none": count, "anchor-only": 0, "complete": 0},
            "minimum": declared["minimum"],
            "maximum": declared["maximum"],
            "style": declared["style"],
            "output": declared["output"],
        }
    write_json(
        work_dir / "document-ir.json",
        {
            "schema_version": 2,
            "profile": {
                "id": profile["id"],
                "sha256": canonical_profile_sha256(profile),
            },
            "nodes": nodes,
            "semantic_groups": [],
            "inventories": {"role_inventory": inventory},
        },
    )

    (work_dir / "visuals").mkdir()
    (work_dir / "visuals" / "figure.bin").write_bytes(b"v2.3 visual fixture\n")
    source_hash = "a" * 64
    write_json(
        work_dir / "manifest.json",
        {
            "schema_version": 1,
            "source_pdf": "fixture.pdf",
            "source_sha256": source_hash,
            "page_count": 1,
            "profile": {"id": profile["id"]},
            "visuals": [
                {
                    "id": "vis-1",
                    "anchor_id": "n-figure",
                    "path": "visuals/figure.bin",
                    "contained_block_ids": [],
                    "caption_continuation_ids": [],
                }
            ],
            "links": [],
            "external_uris": [],
            "problem_ids": [],
        },
    )
    write_json(work_dir / "source-audit.json", {"status": "passed"})
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
        write_jsonl(
            work_dir / "translation" / "translations-merged.jsonl",
            [
                {
                    "id": item["id"],
                    "source_sha256": item["source_sha256"],
                    "translation": translations[item["id"]],
                }
                for item in request_rows
            ],
        )
        write_json(work_dir / "translation" / "translation-audit.json", {"status": "passed"})

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
