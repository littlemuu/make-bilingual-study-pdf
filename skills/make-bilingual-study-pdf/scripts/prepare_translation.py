#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from audit_source import validate_source_audit_binding
from common import (
    json_loads_strict,
    repair_pdf_linebreaks,
    sha256_text,
)
from profile import canonical_profile_sha256, load_work_profile, profile_contract
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    prepare_artifact_directory,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
)
from translation_utils import glossary_term_present, protect_source, validate_glossary


def _read_json(path: Path, work_dir: Path) -> Any:
    return json_loads_strict(read_artifact_text(path, boundary=work_dir))


def _read_jsonl(path: Path, work_dir: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, raw in enumerate(
        read_artifact_text(path, boundary=work_dir).splitlines(), 1
    ):
        if not raw.strip():
            continue
        try:
            value = json_loads_strict(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"invalid JSONL object at {path}:{line_number}")
        values.append(value)
    return values


def _json_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _jsonl_payload(values: list[dict[str, Any]]) -> str:
    return "".join(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for value in values
    )


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    return os.path.lexists(path)


def _preflight_flat_directory(directory: Path, work_dir: Path) -> list[Path]:
    """Validate a generated flat directory and every entry without following links."""
    if validate_artifact_tree(directory, boundary=work_dir, allow_missing=True) is None:
        return []
    try:
        with os.scandir(directory) as iterator:
            paths = sorted((Path(entry.path) for entry in iterator), key=lambda item: item.name)
    except OSError as exc:
        raise ArtifactSafetyError(
            f"cannot scan translation artifact directory: {exc}"
        ) from exc
    for path in paths:
        validate_artifact_file(path, boundary=work_dir)
    validate_artifact_directory(directory, boundary=work_dir)
    return paths


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
    if not _artifact_exists(path, work_dir):
        raise ValueError("schema V2 translation requires document-ir.json")
    ir = _read_json(path, work_dir)
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

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    required = [
        work_dir / "manifest.json",
        work_dir / "blocks.jsonl",
        work_dir / "source-audit.json",
        work_dir / "translation" / "glossary.json",
    ]
    for path in required:
        validate_artifact_file(path, boundary=work_dir)
    _, source_binding_errors = validate_source_audit_binding(
        work_dir, work_dir / "source-audit.json"
    )
    if source_binding_errors:
        raise SystemExit(
            "source audit bindings are stale; translation is blocked: "
            + "; ".join(source_binding_errors)
        )

    glossary_path = work_dir / "translation" / "glossary.json"
    glossary = _read_json(glossary_path, work_dir)
    if not isinstance(glossary, dict):
        raise SystemExit("glossary must be a JSON object")
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
    if glossary.get("source_blocks_sha256") != sha256_artifact(
        work_dir / "blocks.jsonl", boundary=work_dir
    ):
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
    plan_exists = _artifact_exists(plan_path, work_dir)
    if plan_exists and not args.force:
        raise SystemExit(
            f"translation plan already exists: {plan_path}; use --force to rebuild requests"
        )

    manifest = _read_json(work_dir / "manifest.json", work_dir)
    blocks = _read_jsonl(work_dir / "blocks.jsonl", work_dir)
    if not isinstance(manifest, dict):
        raise SystemExit("source manifest must be a JSON object")
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

    source_pdf_sha256 = manifest.get("source_sha256")
    if (
        not isinstance(source_pdf_sha256, str)
        or len(source_pdf_sha256) != 64
        or any(character not in "0123456789abcdef" for character in source_pdf_sha256)
    ):
        raise SystemExit("source manifest has an invalid source_sha256")

    # Serialize and hash the complete replacement set before --force is allowed
    # to invalidate any prior plan or request. This keeps validation failures
    # non-destructive and makes each individual publication atomic.
    request_publications: list[tuple[Path, str]] = []
    batch_summaries: list[dict[str, Any]] = []
    for number, chunk in enumerate(chunks, start=1):
        filename = f"part-{number:04d}.jsonl"
        path = requests_dir / filename
        payload = _jsonl_payload(chunk)
        request_publications.append((path, payload))
        batch_summaries.append(
            {
                "part": number,
                "request_file": f"requests/{filename}",
                "response_file": f"responses/{filename}",
                "request_sha256": sha256_text(payload),
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
            sha256_artifact(work_dir / "profile.json", boundary=work_dir)
            if _artifact_exists(work_dir / "profile.json", work_dir)
            else None
        ),
        "document_ir_sha256": (
            sha256_artifact(work_dir / "document-ir.json", boundary=work_dir)
            if _artifact_exists(work_dir / "document-ir.json", work_dir)
            else None
        ),
        "source_pdf_sha256": source_pdf_sha256,
        "source_manifest_sha256": sha256_artifact(
            work_dir / "manifest.json", boundary=work_dir
        ),
        "source_blocks_sha256": sha256_artifact(
            work_dir / "blocks.jsonl", boundary=work_dir
        ),
        "source_audit_sha256": sha256_artifact(
            work_dir / "source-audit.json", boundary=work_dir
        ),
        "glossary_sha256": sha256_artifact(glossary_path, boundary=work_dir),
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
    plan_payload = _json_payload(plan)

    # Complete every directory and target preflight after all fallible input
    # validation/serialization, but before force removes any prior artifact.
    # Responses are user/model inputs and are never cleared.
    prior_request_files = _preflight_flat_directory(requests_dir, work_dir)
    response_files = _preflight_flat_directory(responses_dir, work_dir)
    validate_artifact_file(plan_path, boundary=work_dir, allow_missing=True)

    if args.force:
        # Invalidate the old plan before changing any request it may reference.
        remove_artifact_file(plan_path, boundary=work_dir, missing_ok=True)
        for path in prior_request_files:
            if path.match("part-*.jsonl"):
                remove_artifact_file(path, boundary=work_dir, missing_ok=False)
    prepare_artifact_directory(requests_dir, boundary=work_dir)
    prepare_artifact_directory(responses_dir, boundary=work_dir)
    for path, payload in request_publications:
        atomic_write_text(path, payload, boundary=work_dir)
    atomic_write_text(plan_path, plan_payload, boundary=work_dir)
    print(
        json.dumps(
            {
                "translation_dir": str(translation_dir),
                "segments": len(requests),
                "batches": len(chunks),
                "responses_preserved": sum(
                    path.match("part-*.jsonl") for path in response_files
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except ArtifactSafetyError as exc:
        raise SystemExit(str(exc)) from exc
