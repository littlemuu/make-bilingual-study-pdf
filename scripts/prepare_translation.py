#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    read_json,
    read_jsonl,
    repair_pdf_linebreaks,
    sha256_file,
    write_json,
    write_jsonl,
)
from translation_utils import glossary_term_present, protect_source, validate_glossary


def short_context(blocks: list[dict[str, Any]], index: int, direction: int) -> str:
    cursor = index + direction
    while 0 <= cursor < len(blocks):
        candidate = blocks[cursor]
        if (
            candidate.get("page") == blocks[index].get("page")
            and candidate.get("kind") not in {"artifact", "image", "visual_content"}
            and candidate.get("source", "").strip()
        ):
            value = candidate["source"].replace("\n", " ").strip()
            return value[:320]
        cursor += direction
    return ""


def chunk_requests(
    requests: list[dict[str, Any]], max_source_chars: int
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for request in requests:
        request_chars = len(request["source_for_translation"])
        if current and current_chars + request_chars > max_source_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(request)
        current_chars += request_chars
    if current:
        chunks.append(current)
    return chunks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create resumable, placeholder-protected translation request batches."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--max-source-chars", type=int, default=8000)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not 1000 <= args.max_source_chars <= 30000:
        raise SystemExit("--max-source-chars must be between 1000 and 30000")

    work_dir = args.work_dir.expanduser().resolve()
    required = [
        work_dir / "manifest.json",
        work_dir / "blocks.jsonl",
        work_dir / "source-audit.json",
        work_dir / "translation" / "glossary.json",
    ]
    for path in required:
        if not path.is_file():
            raise SystemExit(f"missing required artifact: {path}")
    source_audit = read_json(work_dir / "source-audit.json")
    if source_audit.get("status") != "passed":
        raise SystemExit("source audit has not passed; translation is blocked")

    glossary_path = work_dir / "translation" / "glossary.json"
    glossary = read_json(glossary_path)
    if glossary.get("source_blocks_sha256") != sha256_file(work_dir / "blocks.jsonl"):
        raise SystemExit(
            "glossary was created for different source blocks; reinitialize and review it"
        )
    try:
        glossary_terms = validate_glossary(glossary)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    translation_dir = work_dir / "translation"
    requests_dir = translation_dir / "requests"
    responses_dir = translation_dir / "responses"
    plan_path = translation_dir / "plan.json"
    if plan_path.exists() and not args.force:
        raise SystemExit(
            f"translation plan already exists: {plan_path}; use --force to rebuild requests"
        )
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    if args.force:
        for path in requests_dir.glob("part-*.jsonl"):
            path.unlink()

    manifest = read_json(work_dir / "manifest.json")
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    blocks_by_id = {block["id"]: block for block in blocks}
    requests: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not block.get("translatable"):
            continue
        source_for_translation, protected = protect_source(block)
        source_for_translation = repair_pdf_linebreaks(source_for_translation)
        context_before = short_context(blocks, index, -1)
        if block.get("caption_parent") in blocks_by_id:
            context_before = blocks_by_id[block["caption_parent"]]["source"]
        requests.append(
            {
                "schema_version": 1,
                "id": block["id"],
                "page": block["page"],
                "kind": block["kind"],
                "source_sha256": block["source_sha256"],
                "source": block["source"],
                "source_for_translation": source_for_translation,
                "protected_tokens": protected,
                "glossary_terms": [
                    term
                    for term in glossary_terms
                    if glossary_term_present(block["source"], term)
                ],
                "context_before": context_before,
                "context_after": short_context(blocks, index, 1),
            }
        )

    chunks = chunk_requests(requests, args.max_source_chars)
    batch_summaries = []
    for number, chunk in enumerate(chunks, start=1):
        filename = f"part-{number:04d}.jsonl"
        path = requests_dir / filename
        write_jsonl(path, chunk)
        batch_summaries.append(
            {
                "part": number,
                "request_file": f"requests/{filename}",
                "response_file": f"responses/{filename}",
                "request_sha256": sha256_file(path),
                "segment_count": len(chunk),
                "source_characters": sum(
                    len(item["source_for_translation"]) for item in chunk
                ),
                "first_id": chunk[0]["id"],
                "last_id": chunk[-1]["id"],
            }
        )

    plan = {
        "schema_version": 1,
        "source_pdf_sha256": manifest["source_sha256"],
        "source_manifest_sha256": sha256_file(work_dir / "manifest.json"),
        "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
        "source_audit_sha256": sha256_file(work_dir / "source-audit.json"),
        "glossary_sha256": sha256_file(glossary_path),
        "target_language": "zh-CN",
        "translation_policy": "English source first; faithful Simplified Chinese; preserve every placeholder exactly once.",
        "expected_segment_count": len(requests),
        "expected_ids": [item["id"] for item in requests],
        "batch_count": len(chunks),
        "max_source_characters_per_batch": args.max_source_chars,
        "batches": batch_summaries,
    }
    write_json(plan_path, plan)
    print(
        json.dumps(
            {
                "translation_dir": str(translation_dir),
                "segments": len(requests),
                "batches": len(chunks),
                "responses_preserved": len(list(responses_dir.glob("part-*.jsonl"))),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
