#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from common import read_json, read_jsonl, sha256_file, sha256_text, write_json
from profile import (
    bind_profile,
    canonical_profile_sha256,
    load_work_profile,
    semantic_match,
)


IR_FILENAME = "document-ir.json"


def block_to_node(block: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    text = block.get("source", "")
    match = semantic_match(profile, text, include_target=False)
    node = {
        "id": block["id"],
        "type": block["kind"],
        "location": {
            "page": block["page"],
            "bbox": block.get("bbox"),
        },
        "source": {
            "text": text,
            "sha256": block["source_sha256"],
        },
        "translatable": bool(block.get("translatable")),
        "protected_spans": block.get("protected_spans", []),
        "link_ids": block.get("links", []),
        "evidence": {
            "adapter": profile["input"]["adapter"],
            "source_block_id": block["id"],
        },
    }
    if match:
        node["semantic_anchor"] = {
            "role": match["role"],
            "identifier": match.get("identifier"),
        }
    if block.get("caption_parent"):
        node["relations"] = {"caption_parent": block["caption_parent"]}
    return node


def build_document_ir(
    manifest: dict[str, Any],
    blocks: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    manifest_sha256: str,
    blocks_sha256: str,
) -> dict[str, Any]:
    seen: set[str] = set()
    nodes: list[dict[str, Any]] = []
    semantic_groups: list[dict[str, Any]] = []
    for block in blocks:
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id:
            raise ValueError("every source block must have a stable id")
        if block_id in seen:
            raise ValueError(f"duplicate source block id: {block_id}")
        seen.add(block_id)
        source = block.get("source", "")
        if sha256_text(source) != block.get("source_sha256"):
            raise ValueError(f"source hash mismatch for block: {block_id}")
        node = block_to_node(block, profile)
        nodes.append(node)
        anchor = node.get("semantic_anchor")
        if anchor:
            identifier = anchor.get("identifier") or block_id
            semantic_groups.append(
                {
                    "id": f"{anchor['role']}:{identifier}",
                    "role": anchor["role"],
                    "identifier": anchor.get("identifier"),
                    "anchor_node_id": block_id,
                    "member_node_ids": [block_id],
                    "membership": "anchor-only",
                    "source_pages": [block["page"]],
                    "evidence": (
                        "The native-text PDF adapter proves the semantic label and anchor. "
                        "It does not infer a complete container range without structural evidence."
                    ),
                }
            )

    role_counts = Counter(item["role"] for item in semantic_groups)
    return {
        "schema_version": 1,
        "profile": {
            "id": profile["id"],
            "sha256": canonical_profile_sha256(profile),
        },
        "source": {
            "adapter": profile["input"]["adapter"],
            "path": manifest.get("source_pdf"),
            "sha256": manifest["source_sha256"],
            "page_count": manifest["page_count"],
            "manifest_sha256": manifest_sha256,
            "blocks_sha256": blocks_sha256,
        },
        "nodes": nodes,
        "semantic_groups": semantic_groups,
        "inventories": {
            "node_count": len(nodes),
            "node_type_counts": dict(Counter(node["type"] for node in nodes)),
            "semantic_role_counts": dict(role_counts),
            "external_uris": manifest.get("external_uris", []),
            "visual_ids": [item["id"] for item in manifest.get("visuals", [])],
        },
    }


def expected_ir(work_dir: Path, profile: dict[str, Any]) -> dict[str, Any]:
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    return build_document_ir(
        read_json(manifest_path),
        read_jsonl(blocks_path),
        profile,
        manifest_sha256=sha256_file(manifest_path),
        blocks_sha256=sha256_file(blocks_path),
    )


def validate_ir_against_sources(
    work_dir: Path, profile: dict[str, Any] | None = None
) -> list[str]:
    failures: list[str] = []
    path = work_dir / IR_FILENAME
    if not path.is_file():
        return [f"missing {IR_FILENAME}"]
    try:
        active_profile = profile or load_work_profile(work_dir)
        actual = read_json(path)
        expected = expected_ir(work_dir, active_profile)
    except (ValueError, KeyError, FileNotFoundError, json.JSONDecodeError) as exc:
        return [f"invalid document IR inputs: {exc}"]
    if actual != expected:
        failures.append("document IR is stale or does not match its bound profile/source artifacts")
    return failures


def write_document_ir(work_dir: Path, profile: dict[str, Any]) -> Path:
    output = work_dir / IR_FILENAME
    write_json(output, expected_ir(work_dir, profile))
    return output


def migrate_work_dir(
    work_dir: Path, profile_reference: str | Path | None, *, force: bool = False
) -> Path:
    work_dir = work_dir.resolve()
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    for path in (manifest_path, blocks_path):
        if not path.is_file():
            raise ValueError(f"missing required source artifact: {path}")
    profile = bind_profile(work_dir, profile_reference, force=force)
    manifest = read_json(manifest_path)
    profile_binding = {
        "id": profile["id"],
        "sha256": canonical_profile_sha256(profile),
    }
    if manifest.get("profile") not in (None, profile_binding) and not force:
        raise ValueError("manifest is bound to a different profile")
    manifest["profile"] = profile_binding
    manifest.setdefault("artifacts", {})["profile"] = "profile.json"
    manifest["artifacts"]["document_ir"] = IR_FILENAME
    write_json(manifest_path, manifest)
    return write_document_ir(work_dir, profile)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or validate the profile-bound unified document IR."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    if args.check:
        profile = load_work_profile(work_dir, args.profile)
        failures = validate_ir_against_sources(work_dir, profile)
        report = {
            "status": "failed" if failures else "passed",
            "profile": profile["id"],
            "ir": str(work_dir / IR_FILENAME),
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if failures:
            raise SystemExit(1)
        return
    try:
        output = migrate_work_dir(work_dir, args.profile, force=args.force)
        ir = read_json(output)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": "passed",
                "profile": ir["profile"]["id"],
                "ir": str(output),
                "nodes": ir["inventories"]["node_count"],
                "semantic_roles": ir["inventories"]["semantic_role_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
