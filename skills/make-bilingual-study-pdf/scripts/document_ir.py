#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from common import read_json, read_jsonl, sha256_text, write_json
from profile import (
    _bind_validated_profile,
    canonical_profile_sha256,
    load_profile,
    load_work_profile,
    profile_contract,
    semantic_match,
    validate_profile_binding_target,
)
from safe_artifacts import (
    ArtifactSafetyError,
    artifact_size,
    clear_artifact_directory,
    lexical_absolute_path,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


IR_FILENAME = "document-ir.json"
MEMBERSHIP_LEVELS = ("none", "anchor-only", "complete")


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


def _token(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.strip().lower().replace("_", "-")


def _selector_match(
    block: dict[str, Any], selector: dict[str, Any]
) -> dict[str, Any] | None:
    """Match one V2 selector using only source-side, adapter-proven fields."""
    evidence = block.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    source = block.get("source", "")
    if not isinstance(source, str):
        return None
    adapter_role = block.get("adapter_role", evidence.get("adapter_role"))
    node_type = block.get("type", block.get("kind"))
    sub_type = block.get(
        "subtype",
        block.get("sub_type", evidence.get("raw_sub_type")),
    )
    text_level = block.get("text_level", evidence.get("text_level"))
    matched_fields: list[str] = []
    identifier = None

    if "adapter_role" in selector:
        if _token(adapter_role) != _token(selector["adapter_role"]):
            return None
        matched_fields.append("adapter_role")
    if "node_types" in selector:
        accepted = {_token(item) for item in selector["node_types"]}
        if _token(node_type) not in accepted:
            return None
        matched_fields.append("node_types")
    if "sub_types" in selector:
        accepted = {_token(item) for item in selector["sub_types"]}
        if _token(sub_type) not in accepted:
            return None
        matched_fields.append("sub_types")
    if "text_levels" in selector:
        if text_level not in selector["text_levels"]:
            return None
        matched_fields.append("text_levels")
    if "source_pattern" in selector:
        match = re.search(selector["source_pattern"], source.strip(), re.I)
        if not match:
            return None
        identifier = match.groupdict().get("identifier")
        matched_fields.append("source_pattern")

    # target_pattern is paired translation-side evidence. It cannot establish a
    # source classification by itself and is intentionally not evaluated here.
    if not matched_fields:
        return None
    return {"matched_fields": matched_fields, "identifier": identifier}


def _classify_v2_block(
    block: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any] | None:
    candidates: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
    generic_adapter_role = _token(block.get("adapter_role")) in {
        "heading",
        "paragraph",
    }
    for role_index, role_spec in enumerate(contract["roles"]):
        for selector_index, selector in enumerate(role_spec["selectors"]):
            matched = _selector_match(block, selector)
            if matched:
                fields = set(matched["matched_fields"])
                # Adapter-proven roles outrank semantic text patterns.  Patterns
                # outrank generic kind/text-level fallbacks so "Abstract" and
                # "Theorem" are not swallowed by heading/prose catch-alls.
                if "adapter_role" in fields and not generic_adapter_role:
                    tier = 4
                elif "sub_types" in fields:
                    tier = 3
                elif "source_pattern" in fields:
                    tier = 2
                else:
                    tier = 1
                candidate = {
                    "role": role_spec["role"],
                    "style": role_spec["style"],
                    "grouping": role_spec["grouping"],
                    "output": role_spec["output"],
                    "selector_index": selector_index,
                    **matched,
                }
                candidates.append(
                    (
                        (tier, len(fields), -role_index, -selector_index),
                        candidate,
                    )
                )
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _auxiliary_role(block: dict[str, Any], contract: dict[str, Any]) -> str | None:
    value = _token(block.get("adapter_role"))
    aliases = {
        "header": "page-header",
        "footer": "page-footer",
        "page-number": "page-number",
        "aside-text": "marginalia",
    }
    value = aliases.get(value, value)
    return value if value in contract["auxiliary_dispositions"] else None


def _v2_node(
    block: dict[str, Any], contract: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    matched = _classify_v2_block(block, contract)
    auxiliary_role = None if matched else _auxiliary_role(block, contract)
    source_evidence = block.get("evidence")
    source_evidence = (
        copy.deepcopy(source_evidence) if isinstance(source_evidence, dict) else {}
    )
    source_evidence.setdefault("adapter", contract["adapter"])
    source_evidence.setdefault("source_block_id", block["id"])
    if matched:
        source_evidence["semantic_selector"] = {
            "role": matched["role"],
            "selector_index": matched["selector_index"],
            "matched_fields": matched["matched_fields"],
        }

    if matched:
        role = matched["role"]
        style = matched["style"]
        output = matched["output"]
    elif auxiliary_role:
        role = auxiliary_role
        style = None
        output = contract["auxiliary_dispositions"][auxiliary_role]
    else:
        role = None
        style = None
        output = (
            "artifact-omitted"
            if block.get("kind") == "artifact"
            else "bilingual"
            if block.get("translatable")
            else "source-only"
        )

    text = block.get("source", "")
    method = (
        "profile-selector"
        if matched
        else "profile-auxiliary-disposition"
        if auxiliary_role
        else "unclassified"
    )
    source_pointer = source_evidence.get("content_pointer", block["id"])
    node = {
        "id": block["id"],
        "type": block.get("type", block.get("kind")),
        "semantic": {
            "role": role,
            "style": style,
            "output": output,
            "evidence": {
                "method": method,
                "source_pointer": source_pointer,
            },
        },
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
        "evidence": source_evidence,
    }
    if matched:
        node["semantic_anchor"] = {
            "role": matched["role"],
            "identifier": matched.get("identifier"),
        }
    if block.get("caption_parent"):
        node["relations"] = {"caption_parent": block["caption_parent"]}
    return node, matched


def _complete_members(
    block: dict[str, Any], *, block_by_id: dict[str, dict[str, Any]], adapter: str
) -> list[str] | None:
    evidence = block.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("adapter") != adapter:
        return None
    structural = evidence.get("structural_membership")
    if not isinstance(structural, dict) or structural.get("status") != "complete":
        return None
    member_ids = structural.get("member_node_ids")
    if (
        not isinstance(member_ids, list)
        or not member_ids
        or any(not isinstance(item, str) or not item for item in member_ids)
        or len(member_ids) != len(set(member_ids))
        or block["id"] not in member_ids
        or any(item not in block_by_id for item in member_ids)
        or any(
            not isinstance(block_by_id[item].get("evidence"), dict)
            or block_by_id[item]["evidence"].get("adapter") != adapter
            for item in member_ids
        )
    ):
        return None
    return list(member_ids)


def _safe_work_path(work_dir: Path, value: Any, *, label: str) -> Path:
    return work_relative_artifact_path(work_dir, value, label=label)


def load_adapter_source_evidence(
    work_dir: Path, manifest: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load and verify adapter evidence plus every frozen input and asset."""
    adapter = manifest.get("adapter")
    if adapter is None:
        return None, None
    if not isinstance(adapter, dict):
        raise ValueError("manifest adapter must be an object")
    adapter_id = adapter.get("id")
    if not isinstance(adapter_id, str) or not adapter_id:
        raise ValueError("manifest adapter id is missing")
    evidence_path = _safe_work_path(
        work_dir, adapter.get("evidence"), label="adapter evidence path"
    )
    validate_artifact_file(evidence_path, boundary=work_dir)
    evidence_sha256 = sha256_artifact(evidence_path, boundary=work_dir)
    if evidence_sha256 != adapter.get("evidence_sha256"):
        raise ValueError("adapter evidence hash does not match manifest")
    evidence = read_json(evidence_path)
    if not isinstance(evidence, dict) or evidence.get("adapter") != adapter_id:
        raise ValueError("adapter evidence identity does not match manifest")

    frozen: dict[str, Any] = {
        "path": adapter["evidence"],
        "sha256": evidence_sha256,
        "inputs": [],
        "assets": [],
    }
    for category, identity_fields in (
        ("inputs", ("role", "relative_path")),
        ("assets", ("id", "relative_path")),
    ):
        records = evidence.get(category)
        if not isinstance(records, list):
            raise ValueError(f"adapter evidence {category} must be an array")
        seen: set[tuple[Any, ...]] = set()
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"adapter evidence {category}[{index}] must be an object")
            identity = tuple(record.get(field) for field in identity_fields)
            if any(not isinstance(value, str) or not value for value in identity):
                raise ValueError(f"adapter evidence {category}[{index}] identity is invalid")
            if identity in seen:
                raise ValueError(f"duplicate adapter evidence {category} identity: {identity}")
            seen.add(identity)
            path = _safe_work_path(
                work_dir,
                record.get("work_path"),
                label=f"adapter evidence {category}[{index}].work_path",
            )
            validate_artifact_file(path, boundary=work_dir)
            if artifact_size(path, boundary=work_dir) != record.get("size"):
                raise ValueError(f"frozen adapter {category[:-1]} size changed: {record.get('work_path')}")
            if sha256_artifact(path, boundary=work_dir) != record.get("sha256"):
                raise ValueError(f"frozen adapter {category[:-1]} hash changed: {record.get('work_path')}")
            frozen[category].append(
                {
                    **{field: record[field] for field in identity_fields},
                    "work_path": record["work_path"],
                    "sha256": record["sha256"],
                    "size": record["size"],
                }
            )
    return evidence, frozen


def _build_document_ir_v1(
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


def _build_document_ir_v2(
    manifest: dict[str, Any],
    blocks: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    manifest_sha256: str,
    blocks_sha256: str,
    adapter_freeze: dict[str, Any] | None,
) -> dict[str, Any]:
    contract = profile_contract(profile)
    manifest_adapter = manifest.get("adapter")
    if contract["adapter"] != "native-text-pdf":
        if not isinstance(manifest_adapter, dict):
            raise ValueError("non-native V2 Profile requires manifest adapter evidence")
        if manifest_adapter.get("id") != contract["adapter"]:
            raise ValueError("manifest adapter does not match the bound Profile")
        if adapter_freeze is None:
            raise ValueError("non-native V2 Profile requires frozen adapter evidence")

    seen: set[str] = set()
    block_by_id: dict[str, dict[str, Any]] = {}
    for block in blocks:
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id:
            raise ValueError("every source block must have a stable id")
        if block_id in seen:
            raise ValueError(f"duplicate source block id: {block_id}")
        seen.add(block_id)
        source = block.get("source", "")
        if not isinstance(source, str) or sha256_text(source) != block.get(
            "source_sha256"
        ):
            raise ValueError(f"source hash mismatch for block: {block_id}")
        block_by_id[block_id] = block

    nodes: list[dict[str, Any]] = []
    semantic_groups: list[dict[str, Any]] = []
    for block in blocks:
        node, matched = _v2_node(block, contract)
        nodes.append(node)
        if not matched:
            continue
        role = matched["role"]
        member_ids = None
        if matched["grouping"] == "structural-container":
            member_ids = _complete_members(
                block, block_by_id=block_by_id, adapter=contract["adapter"]
            )
        membership = "complete" if member_ids is not None else "anchor-only"
        if member_ids is None:
            member_ids = [block["id"]]
        semantic_groups.append(
            {
                "id": f"{role}:{block['id']}",
                "role": role,
                "identifier": matched.get("identifier"),
                "anchor_node_id": block["id"],
                "member_node_ids": member_ids,
                "membership": membership,
                "source_pages": sorted(
                    {int(block_by_id[item]["page"]) for item in member_ids}
                ),
                "evidence": {
                    "adapter": contract["adapter"],
                    "source_pointer": node["semantic"]["evidence"]["source_pointer"],
                    "selector_index": matched["selector_index"],
                    "matched_fields": matched["matched_fields"],
                    "structural_membership": (
                        "adapter-proved" if membership == "complete" else "not-proved"
                    ),
                },
            }
        )

    groups_by_role: dict[str, list[dict[str, Any]]] = {
        role["role"]: [] for role in contract["roles"]
    }
    for group in semantic_groups:
        groups_by_role[group["role"]].append(group)
    inventory: dict[str, dict[str, Any]] = {}
    for role, policy in contract["role_inventory"].items():
        groups = groups_by_role[role]
        membership_counts = Counter(group["membership"] for group in groups)
        member_node_ids = {
            node_id for group in groups for node_id in group["member_node_ids"]
        }
        inventory[role] = {
            "occurrence_count": len(groups),
            "node_count": len(member_node_ids),
            "occurrence_ids": [group["id"] for group in groups],
            "membership_counts": {
                level: membership_counts.get(level, 0)
                for level in MEMBERSHIP_LEVELS
            },
            "minimum": policy["minimum"],
            "maximum": policy["maximum"],
            "style": policy["style"],
            "output": policy["output"],
        }

    source = {
        "adapter": contract["adapter"],
        "path": manifest.get("source_pdf"),
        "sha256": manifest["source_sha256"],
        "page_count": manifest["page_count"],
        "manifest_sha256": manifest_sha256,
        "blocks_sha256": blocks_sha256,
    }
    if adapter_freeze is not None:
        source["adapter_evidence"] = adapter_freeze
    return {
        "schema_version": 2,
        "profile": {
            "id": profile["id"],
            "sha256": canonical_profile_sha256(profile),
        },
        "source": source,
        "nodes": nodes,
        "semantic_groups": semantic_groups,
        "inventories": {
            "node_count": len(nodes),
            "node_type_counts": dict(Counter(node["type"] for node in nodes)),
            "semantic_role_counts": {
                role: item["occurrence_count"] for role, item in inventory.items()
            },
            "role_inventory": inventory,
            "external_uris": manifest.get("external_uris", []),
            "visual_ids": [item["id"] for item in manifest.get("visuals", [])],
        },
    }


def build_document_ir(
    manifest: dict[str, Any],
    blocks: list[dict[str, Any]],
    profile: dict[str, Any],
    *,
    manifest_sha256: str,
    blocks_sha256: str,
    adapter_freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profile.get("schema_version") == 1:
        return _build_document_ir_v1(
            manifest,
            blocks,
            profile,
            manifest_sha256=manifest_sha256,
            blocks_sha256=blocks_sha256,
        )
    return _build_document_ir_v2(
        manifest,
        blocks,
        profile,
        manifest_sha256=manifest_sha256,
        blocks_sha256=blocks_sha256,
        adapter_freeze=adapter_freeze,
    )


def expected_ir(work_dir: Path, profile: dict[str, Any]) -> dict[str, Any]:
    work_dir = validate_artifact_directory(work_dir)
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    validate_artifact_file(manifest_path, boundary=work_dir)
    validate_artifact_file(blocks_path, boundary=work_dir)
    manifest = read_json(manifest_path)
    _evidence, adapter_freeze = load_adapter_source_evidence(work_dir, manifest)
    return build_document_ir(
        manifest,
        read_jsonl(blocks_path),
        profile,
        manifest_sha256=sha256_artifact(manifest_path, boundary=work_dir),
        blocks_sha256=sha256_artifact(blocks_path, boundary=work_dir),
        adapter_freeze=adapter_freeze,
    )


def validate_ir_against_sources(
    work_dir: Path, profile: dict[str, Any] | None = None
) -> list[str]:
    failures: list[str] = []
    try:
        work_dir = validate_artifact_directory(work_dir)
    except ArtifactSafetyError as exc:
        return [f"unsafe document IR work directory: {exc}"]
    path = work_dir / IR_FILENAME
    try:
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        if not os.path.lexists(path):
            return [f"missing {IR_FILENAME}"]
        active_profile = profile or load_work_profile(work_dir)
        actual = read_json(path)
        expected = expected_ir(work_dir, active_profile)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return [f"invalid document IR inputs: {exc}"]
    if actual != expected:
        failures.append("document IR is stale or does not match its bound profile/source artifacts")
    return failures


def write_document_ir(work_dir: Path, profile: dict[str, Any]) -> Path:
    work_dir = validate_artifact_directory(work_dir)
    output = work_dir / IR_FILENAME
    validate_artifact_file(output, boundary=work_dir, allow_missing=True)
    write_json(output, expected_ir(work_dir, profile))
    return output


def migrate_work_dir(
    work_dir: Path, profile_reference: str | Path | None, *, force: bool = False
) -> Path:
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    for path in (manifest_path, blocks_path):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        if not os.path.lexists(path):
            raise ValueError(f"missing required source artifact: {path}")
    ir_path = work_dir / IR_FILENAME
    source_audit_path = work_dir / "source-audit.json"
    for path in (ir_path, source_audit_path):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    downstream_directories = [work_dir / "translation", work_dir / "output"]
    for path in downstream_directories:
        validate_artifact_tree(path, work_dir)

    manifest = read_json(manifest_path)
    validate_profile_binding_target(work_dir)
    requested_profile = load_profile(profile_reference)
    requested_binding = {
        "id": requested_profile["id"],
        "sha256": canonical_profile_sha256(requested_profile),
    }
    if manifest.get("profile") not in (None, requested_binding) and not force:
        raise ValueError("manifest is bound to a different profile")

    profile_binding = requested_binding
    manifest["profile"] = profile_binding
    manifest.setdefault("artifacts", {})["profile"] = "profile.json"
    manifest["artifacts"]["document_ir"] = IR_FILENAME
    blocks = read_jsonl(blocks_path)
    _evidence, adapter_freeze = load_adapter_source_evidence(work_dir, manifest)
    manifest_payload = (
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    document_ir = build_document_ir(
        manifest,
        blocks,
        requested_profile,
        manifest_sha256=sha256_text(manifest_payload),
        blocks_sha256=sha256_artifact(blocks_path, boundary=work_dir),
        adapter_freeze=adapter_freeze,
    )

    remove_artifact_file(source_audit_path, boundary=work_dir)
    _bind_validated_profile(work_dir, requested_profile, force=force)
    for path in downstream_directories:
        clear_artifact_directory(
            path, boundary=work_dir, remove_directory=True
        )
    write_json(ir_path, document_ir)
    write_json(manifest_path, manifest)
    return ir_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build or validate the profile-bound unified document IR."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = Path(os.path.abspath(args.work_dir.expanduser()))
    if args.check:
        try:
            profile = load_work_profile(work_dir, args.profile)
            profile_id = profile["id"]
            failures = validate_ir_against_sources(work_dir, profile)
        except (KeyError, ValueError) as exc:
            profile_id = None
            failures = [f"invalid profile binding: {exc}"]
        report = {
            "status": "failed" if failures else "passed",
            "profile": profile_id,
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
