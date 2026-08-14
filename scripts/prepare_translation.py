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
from profile import canonical_profile_sha256, load_work_profile, profile_contract


def semantic_policy(
    node: dict[str, Any] | None,
    block: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a node's semantic policy while retaining the V1 block fallback."""
    role_policies = {
        item["role"]: item for item in contract.get("roles", [])
    }
    semantic = node.get("semantic") if isinstance(node, dict) else None
    semantic = semantic if isinstance(semantic, dict) else {}
    role_value = node.get("semantic_role") if isinstance(node, dict) else None
    if isinstance(role_value, dict):
        role_value = role_value.get("role")
    anchor = node.get("semantic_anchor") if isinstance(node, dict) else None
    role = semantic.get("role") or role_value
    if role is None and isinstance(anchor, dict):
        role = anchor.get("role")
    role_policy = role_policies.get(role, {})
    auxiliary_output = contract.get("auxiliary_dispositions", {}).get(role)
    output = (
        semantic.get("output")
        or (node.get("output_disposition") if isinstance(node, dict) else None)
        or role_policy.get("output")
        or auxiliary_output
    )
    style = semantic.get("style") or role_policy.get("style")
    if contract["source_schema_version"] == 1:
        output = output or ("bilingual" if block.get("translatable") else "source-only")
    elif (
        role not in role_policies
        and role not in contract.get("auxiliary_dispositions", {})
    ) or output is None or (role in role_policies and style is None):
        raise ValueError(f"node {block.get('id')} has no registered semantic policy")
    if auxiliary_output is not None and output != auxiliary_output:
        raise ValueError(
            f"node {block.get('id')} auxiliary policy disagrees with its Profile"
        )
    return {"role": role, "style": style, "output": output}


def ir_nodes_by_id(
    work_dir: Path, blocks: list[dict[str, Any]], contract: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    # Schema V1 freezes document-ir.json as an opaque build input.  Legacy
    # fixtures intentionally use a minimal object here, so parsing semantic
    # nodes would tighten the old contract and break reproducibility.
    if contract["source_schema_version"] == 1:
        return {}
    path = work_dir / "document-ir.json"
    if not path.is_file():
        raise ValueError("schema V2 translation requires document-ir.json")
    ir = read_json(path)
    nodes = ir.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("document IR nodes must be an array")
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or not node_id or node_id in result:
            raise ValueError(f"invalid or duplicate document IR node id: {node_id!r}")
        result[node_id] = node
    if contract["source_schema_version"] == 2:
        block_ids = [block.get("id") for block in blocks]
        if list(result) != block_ids:
            raise ValueError("document IR node order does not exactly match source blocks")
    return result


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
    try:
        profile = load_work_profile(work_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    profile_sha256 = canonical_profile_sha256(profile)
    if glossary.get("profile_id") not in (None, profile["id"]):
        raise SystemExit("glossary was created for a different profile")
    if glossary.get("profile_sha256") not in (None, profile_sha256):
        raise SystemExit("glossary profile changed; reinitialize and review it")
    if glossary.get("target_language") != profile["translation"]["target_language"]:
        raise SystemExit("glossary target language does not match the active profile")
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
    contract = profile_contract(profile)
    try:
        nodes_by_id = ir_nodes_by_id(work_dir, blocks, contract)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    blocks_by_id = {block["id"]: block for block in blocks}
    requests: list[dict[str, Any]] = []
    translation_role_counts: dict[str, int] = {}
    for index, block in enumerate(blocks):
        try:
            policy = semantic_policy(nodes_by_id.get(block["id"]), block, contract)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        node = nodes_by_id.get(block["id"], {})
        translatable = bool(node.get("translatable", block.get("translatable")))
        if policy["output"] != "bilingual" or not translatable:
            continue
        source_for_translation, protected = protect_source(block)
        source_for_translation = repair_pdf_linebreaks(source_for_translation)
        context_before = short_context(blocks, index, -1)
        if block.get("caption_parent") in blocks_by_id:
            context_before = blocks_by_id[block["caption_parent"]]["source"]
        request = {
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
        if contract["source_schema_version"] == 2:
            request["semantic_role"] = policy["role"]
            request["output_disposition"] = policy["output"]
            translation_role_counts[policy["role"]] = (
                translation_role_counts.get(policy["role"], 0) + 1
            )
        requests.append(request)

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
        "schema_version": 2,
        "profile_id": profile["id"],
        "profile_sha256": profile_sha256,
        "profile_file_sha256": (
            sha256_file(work_dir / "profile.json")
            if (work_dir / "profile.json").is_file()
            else None
        ),
        "document_ir_sha256": sha256_file(work_dir / "document-ir.json") if (work_dir / "document-ir.json").is_file() else None,
        "source_pdf_sha256": manifest["source_sha256"],
        "source_manifest_sha256": sha256_file(work_dir / "manifest.json"),
        "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
        "source_audit_sha256": sha256_file(work_dir / "source-audit.json"),
        "glossary_sha256": sha256_file(glossary_path),
        "target_language": profile["translation"]["target_language"],
        "translation_policy": profile["translation"]["policy"],
        "expected_segment_count": len(requests),
        "expected_ids": [item["id"] for item in requests],
        "batch_count": len(chunks),
        "max_source_characters_per_batch": args.max_source_chars,
        "batches": batch_summaries,
    }
    if contract["source_schema_version"] == 2:
        plan["semantic_contract_version"] = contract["contract_version"]
        plan["translation_role_counts"] = translation_role_counts
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
