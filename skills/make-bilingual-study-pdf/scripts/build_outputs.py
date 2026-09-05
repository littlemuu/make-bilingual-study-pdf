#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common import (
    json_loads_strict,
    problem_ids,
    repair_pdf_linebreaks,
    sha256_text,
)
from audit_source import current_manifest_visual_bindings, validate_source_audit_binding
from audit_translation import validate_translation_audit_binding
from profile import (
    canonical_profile_sha256,
    load_work_profile,
    profile_contract,
)
from html_table import validate_table_html
from safe_artifacts import (
    atomic_copy_file,
    atomic_write_text,
    clear_artifact_directory,
    lexical_absolute_path,
    portable_artifact_basename,
    prepare_artifact_directory,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


GENERIC_OUTPUT_DISPOSITIONS = frozenset(
    {"bilingual", "source-only", "visual-once", "artifact-omitted"}
)


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


def _atomic_write_json(path: Path, value: object, work_dir: Path) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    return os.path.lexists(path)


def _invalidate_output_audit(work_dir: Path) -> None:
    output_dir = work_dir / "output"
    if validate_artifact_tree(
        output_dir, work_dir, allow_missing=True
    ) is None:
        return
    output_audit_path = output_dir / "output-audit.json"
    validate_artifact_file(
        output_audit_path, boundary=work_dir, allow_missing=True
    )
    remove_artifact_file(output_audit_path, boundary=work_dir)


def _legacy_output_policy(block: dict[str, Any]) -> str:
    kind = block.get("kind")
    if kind == "artifact":
        return "artifact-omitted"
    if kind in {"image", "math", "visual_content"}:
        return "visual-once"
    if kind == "code":
        return "source-only"
    return "bilingual" if block.get("translatable") else "source-only"


def _semantic_policy(
    node: dict[str, Any] | None,
    block: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    roles = {item["role"]: item for item in contract["roles"]}
    semantic = node.get("semantic") if isinstance(node, dict) else None
    semantic = semantic if isinstance(semantic, dict) else {}
    role_value = node.get("semantic_role") if isinstance(node, dict) else None
    if isinstance(role_value, dict):
        role_value = role_value.get("role")
    anchor = node.get("semantic_anchor") if isinstance(node, dict) else None
    role = semantic.get("role") or role_value
    if role is None and isinstance(anchor, dict):
        role = anchor.get("role")
    declared = roles.get(role, {})
    auxiliary_output = contract.get("auxiliary_dispositions", {}).get(role)
    style = semantic.get("style") or declared.get("style")
    output = (
        semantic.get("output")
        or (node.get("output_disposition") if isinstance(node, dict) else None)
        or declared.get("output")
        or auxiliary_output
    )
    if contract["source_schema_version"] == 1:
        output = output or _legacy_output_policy(block)
    else:
        if (
            role not in roles
            and role not in contract.get("auxiliary_dispositions", {})
        ) or output is None or (role in roles and style is None):
            raise ValueError(f"node {block.get('id')} has no registered semantic policy")
        if role in roles and (
            style != declared["style"] or output != declared["output"]
        ):
            raise ValueError(
                f"node {block.get('id')} semantic policy disagrees with its Profile role"
            )
        if auxiliary_output is not None and output != auxiliary_output:
            raise ValueError(
                f"node {block.get('id')} auxiliary policy disagrees with its Profile"
            )
    if output not in GENERIC_OUTPUT_DISPOSITIONS:
        raise ValueError(f"node {block.get('id')} has an unknown output disposition")
    relations = node.get("relations", {}) if isinstance(node, dict) else {}
    if not isinstance(relations, dict):
        raise ValueError(f"node {block.get('id')} relations must be an object")
    return {
        "role": role,
        "style": style,
        "output": output,
        "relations": relations,
    }


def load_semantic_model(
    work_dir: Path,
    blocks: list[dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, set[str]],
    dict[str, dict[str, Any]],
]:
    """Load a strict V2 semantic model, with a non-mutating V1 compatibility view."""
    contract = profile_contract(profile)
    ir_path = work_dir / "document-ir.json"
    if _artifact_exists(ir_path, work_dir):
        ir = _read_json(ir_path, work_dir)
        raw_nodes = ir.get("nodes")
        if not isinstance(raw_nodes, list):
            raise ValueError("document IR nodes must be an array")
    elif contract["source_schema_version"] == 1:
        ir = {"nodes": [], "semantic_groups": [], "inventories": {}}
        raw_nodes = []
    else:
        raise ValueError("schema V2 output requires document-ir.json")

    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in raw_nodes:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or not node_id or node_id in nodes_by_id:
            raise ValueError(f"invalid or duplicate document IR node id: {node_id!r}")
        nodes_by_id[node_id] = node
    block_ids = [block.get("id") for block in blocks]
    if len(block_ids) != len(set(block_ids)):
        raise ValueError("source blocks contain duplicate IDs")
    if contract["source_schema_version"] == 2 and list(nodes_by_id) != block_ids:
        raise ValueError("document IR node order does not exactly match source blocks")

    groups_by_node: dict[str, set[str]] = defaultdict(set)
    group_ids_by_role: dict[str, list[str]] = defaultdict(list)
    for group in ir.get("semantic_groups", []):
        if not isinstance(group, dict):
            raise ValueError("document IR semantic groups must be objects")
        group_id = group.get("id")
        role = group.get("role")
        members = group.get("member_node_ids")
        if (
            not isinstance(group_id, str)
            or not group_id
            or not isinstance(role, str)
            or not isinstance(members, list)
            or not members
            or len(members) != len(set(members))
        ):
            raise ValueError(f"invalid semantic group: {group_id!r}")
        unknown = sorted(set(members) - set(block_ids))
        if unknown:
            raise ValueError(f"semantic group {group_id} has unknown members: {unknown}")
        group_ids_by_role[role].append(group_id)
        for node_id in members:
            groups_by_node[node_id].add(group_id)

    semantics: dict[str, dict[str, Any]] = {}
    for block in blocks:
        semantics[block["id"]] = _semantic_policy(
            nodes_by_id.get(block["id"]), block, contract
        )

    declared_inventory = contract["role_inventory"]
    raw_inventory = ir.get("inventories", {}).get("role_inventory")
    normalized_inventory: dict[str, dict[str, Any]] = {}
    if contract["source_schema_version"] == 2:
        if not isinstance(raw_inventory, dict) or set(raw_inventory) != set(
            declared_inventory
        ):
            raise ValueError(
                "document IR role_inventory must exactly cover Profile roles"
            )
        for role, declared in declared_inventory.items():
            item = raw_inventory.get(role)
            required = {
                "occurrence_count",
                "node_count",
                "occurrence_ids",
                "membership_counts",
                "minimum",
                "maximum",
                "style",
                "output",
            }
            if not isinstance(item, dict) or not required <= set(item):
                raise ValueError(f"document IR role_inventory.{role} is incomplete")
            if (
                item["minimum"] != declared["minimum"]
                or item["maximum"] != declared["maximum"]
                or item["style"] != declared["style"]
                or item["output"] != declared["output"]
            ):
                raise ValueError(
                    f"document IR role_inventory.{role} disagrees with the Profile"
                )
            if (
                not isinstance(item["occurrence_count"], int)
                or isinstance(item["occurrence_count"], bool)
                or item["occurrence_count"] < 0
                or not isinstance(item["node_count"], int)
                or isinstance(item["node_count"], bool)
                or item["node_count"] < 0
                or not isinstance(item["occurrence_ids"], list)
                or len(item["occurrence_ids"]) != len(set(item["occurrence_ids"]))
                or len(item["occurrence_ids"]) != item["occurrence_count"]
                or not isinstance(item["membership_counts"], dict)
            ):
                raise ValueError(f"document IR role_inventory.{role} has invalid counts")
            normalized_inventory[role] = copy.deepcopy(item)
    else:
        role_counts = ir.get("inventories", {}).get("semantic_role_counts", {})
        for role, declared in declared_inventory.items():
            occurrence_ids = list(group_ids_by_role.get(role, []))
            occurrence_count = int(role_counts.get(role, len(occurrence_ids)))
            normalized_inventory[role] = {
                **declared,
                "occurrence_count": occurrence_count,
                "node_count": sum(
                    semantic.get("role") == role for semantic in semantics.values()
                ),
                "occurrence_ids": occurrence_ids,
                "membership_counts": {
                    "anchor-only": occurrence_count,
                    "complete": 0,
                },
            }
    return (
        contract,
        nodes_by_id,
        semantics,
        groups_by_node,
        normalized_inventory,
    )


LATEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "‣": r"\BilingualMath{\blacktriangleright}",
    "▷": r"\BilingualMath{\triangleright}",
}

LATEX_SEQUENCE_REPLACEMENTS = {
    "\U0001D4A9\ufe00": r"\BilingualMath{\mathcal{N}}",
}

LATEX_UNICODE_MATH_REPLACEMENTS = {
    "≤": r"\BilingualMath{\leq}",
    "≥": r"\BilingualMath{\geq}",
    "∈": r"\BilingualMath{\in}",
    "ℝ": r"\BilingualMath{\mathbb{R}}",
    "⊤": r"\BilingualMath{\top}",
    "𝟙": r"\BilingualMath{\mathbb{1}}",
    "′": r"\BilingualMath{^{\prime}}",
    "⋅": r"\BilingualMath{\cdot}",
    "∇": r"\BilingualMath{\nabla}",
    "⊙": r"\BilingualMath{\odot}",
    "∑": r"\BilingualMath{\sum}",
    "𝛼": r"\BilingualMath{𝛼}",
    "𝛽": r"\BilingualMath{𝛽}",
    "𝜀": r"\BilingualMath{𝜀}",
    "𝜇": r"\BilingualMath{𝜇}",
    "𝜎": r"\BilingualMath{𝜎}",
    "𝜃": r"\BilingualMath{𝜃}",
    "𝜆": r"\BilingualMath{𝜆}",
    "𝜏": r"\BilingualMath{𝜏}",
    "ℎ": r"\BilingualMath{ℎ}",
    "ℓ": r"\BilingualMath{\ell}",
}


def mathematical_italic_latin_replacements() -> dict[str, str]:
    characters = (
        [chr(codepoint) for codepoint in range(0x1D434, 0x1D44E)]
        + [chr(codepoint) for codepoint in range(0x1D44E, 0x1D455)]
        + ["ℎ"]
        + [chr(codepoint) for codepoint in range(0x1D456, 0x1D468)]
    )
    letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    if len(characters) != len(letters):
        raise RuntimeError("mathematical italic Latin mapping is incomplete")
    return {
        character: rf"\BilingualMath{{{character}}}"
        for character, _letter in zip(characters, letters)
    }


LATEX_UNICODE_MATH_REPLACEMENTS.update(mathematical_italic_latin_replacements())


def latex_escape(text: str) -> str:
    escaped: list[str] = []
    index = 0
    while index < len(text):
        for source, replacement in LATEX_SEQUENCE_REPLACEMENTS.items():
            if text.startswith(source, index):
                escaped.append(replacement)
                index += len(source)
                break
        else:
            character = text[index]
            escaped.append(
                LATEX_UNICODE_MATH_REPLACEMENTS.get(
                    character, LATEX_REPLACEMENTS.get(character, character)
                )
            )
            index += 1
    return "".join(escaped)


def latex_url(text: str) -> str:
    return text.replace("\\", "%5C").replace("{", "%7B").replace("}", "%7D")


def prose_text(text: str) -> str:
    return repair_pdf_linebreaks(text)


def markdown_escape(text: str) -> str:
    text = prose_text(text)
    text = text.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]<>#])", r"\\\1", text)


def markdown_quote(text: str) -> str:
    lines = text.strip().splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def heading_level(source: str) -> int:
    match = re.match(r"^(\d+(?:\.\d+)*)\s+", prose_text(source))
    if not match:
        return 2
    return min(4, match.group(1).count(".") + 2)


def should_group_paragraphs(
    previous: dict[str, Any],
    current: dict[str, Any],
    semantics: dict[str, dict[str, Any]] | None = None,
    groups_by_node: dict[str, set[str]] | None = None,
) -> bool:
    if previous["page"] != current["page"]:
        return False
    if previous["kind"] not in {"prose", "list"}:
        return False
    if current["kind"] not in {"prose", "list"}:
        return False
    if not previous.get("translatable") or not current.get("translatable"):
        return False
    if previous.get("caption_parent") or current.get("caption_parent"):
        return False
    if semantics is not None:
        previous_semantic = semantics.get(previous["id"])
        current_semantic = semantics.get(current["id"])
        if previous_semantic is None or current_semantic is None:
            return False
        if (
            previous_semantic.get("role") != current_semantic.get("role")
            or previous_semantic.get("output") != "bilingual"
            or current_semantic.get("output") != "bilingual"
            or previous_semantic.get("relations")
            != current_semantic.get("relations")
        ):
            return False
        if groups_by_node is not None and groups_by_node.get(
            previous["id"], set()
        ) != groups_by_node.get(current["id"], set()):
            return False
    previous_box = previous["bbox"]
    current_box = current["bbox"]
    vertical_gap = current_box[1] - previous_box[3]
    if vertical_gap < -2 or vertical_gap > 10:
        return False
    if abs(previous_box[0] - current_box[0]) > 44:
        return False
    previous_text = prose_text(previous["source"])
    current_text = prose_text(current["source"])
    if len(previous_text) + len(current_text) > 1800:
        return False
    incomplete_tail = not re.search(r"[.!?;:。！？；：][\"')\]]?$", previous_text)
    tail_word = re.search(r"([A-Za-z]+)\W*$", previous_text)
    connective_tail = bool(
        tail_word
        and tail_word.group(1).lower()
        in {
            "a",
            "an",
            "and",
            "as",
            "at",
            "by",
            "for",
            "from",
            "in",
            "of",
            "or",
            "the",
            "to",
            "with",
        }
    )
    return incomplete_tail or connective_tail


def latex_heading_command(source: str) -> str:
    level = heading_level(source)
    return {2: "section", 3: "subsection", 4: "subsubsection"}[level]


def response_marker(block: dict[str, Any], mode: str = "segment") -> str:
    return (
        f"<!-- bilingual:{mode} id={block['id']} "
        f"source_sha256={block['source_sha256']} -->"
    )


def visual_markdown(path: str, alt: str) -> str:
    return f"![{markdown_escape(alt)}]({path})"


def visual_latex(path: str) -> str:
    return (
        "\\begin{center}\n"
        f"\\includegraphics[width=0.92\\linewidth,height=0.62\\textheight,keepaspectratio]"
        f"{{{latex_escape(path)}}}\n"
        "\\end{center}"
    )


def make_bilingual_markdown(
    block: dict[str, Any], translation: str, source_override: str | None = None
) -> str:
    source = source_override if source_override is not None else block["source"]
    kind = block["kind"]
    marker = response_marker(block)
    if kind == "heading":
        level = heading_level(source)
        return (
            f"{marker}\n{'#' * level} {markdown_escape(source)}\n\n"
            f"{markdown_quote('**' + markdown_escape(translation) + '**')}"
        )
    if kind == "callout":
        return (
            f"{marker}\n**{markdown_escape(source)}**\n\n"
            f"{markdown_quote('**' + markdown_escape(translation) + '**')}"
        )
    if kind in {"caption", "caption_continuation"}:
        return (
            f"{marker}\n*{markdown_escape(source)}*\n\n"
            f"{markdown_quote('*' + markdown_escape(translation) + '*')}"
        )
    return (
        f"{marker}\n{markdown_escape(source)}\n\n"
        f"{markdown_quote(markdown_escape(translation))}"
    )


def make_bilingual_latex(
    block: dict[str, Any], translation: str, source_override: str | None = None
) -> str:
    source = prose_text(source_override if source_override is not None else block["source"])
    translated = prose_text(translation)
    anchor = f"\\SegmentAnchor{{{latex_escape(block['id'])}}}"
    kind = block["kind"]
    if kind == "heading":
        command = latex_heading_command(source)
        return (
            f"{anchor}\n\\{command}{{{latex_escape(source)}}}\n"
            "\\begin{BilingualTranslation}\n"
            f"\\textbf{{{latex_escape(translated)}}}\n"
            "\\end{BilingualTranslation}"
        )
    if kind == "callout":
        return (
            f"{anchor}\n\\begin{{BilingualCallout}}\n{latex_escape(source)}\n"
            "\\end{BilingualCallout}\n"
            "\\begin{BilingualTranslation}\n"
            f"\\textbf{{{latex_escape(translated)}}}\n"
            "\\end{BilingualTranslation}"
        )
    if kind in {"caption", "caption_continuation"}:
        return (
            f"{anchor}\n\\begin{{center}}\\small\\itshape\n"
            f"{latex_escape(source)}\\par\n"
            f"\\color{{TranslationColor}}{latex_escape(translated)}\n"
            "\\end{center}"
        )
    return (
        f"{anchor}\n{latex_escape(source)}\n\n"
        "\\begin{BilingualTranslation}\n"
        f"{latex_escape(translated)}\n"
        "\\end{BilingualTranslation}"
    )


def make_translation_only_markdown(block: dict[str, Any], translation: str) -> str:
    return f"{response_marker(block)}\n{markdown_quote(markdown_escape(translation))}"


def make_translation_only_latex(block: dict[str, Any], translation: str) -> str:
    return (
        f"\\SegmentAnchor{{{latex_escape(block['id'])}}}\n"
        "\\begin{BilingualTranslation}\n"
        f"{latex_escape(prose_text(translation))}\n"
        "\\end{BilingualTranslation}"
    )


def source_only_markdown_body(block: dict[str, Any]) -> str:
    source = block["source"]
    kind = block["kind"]
    if kind == "code":
        return f"```text\n{source}\n```"
    if kind == "heading":
        return f"{'#' * heading_level(source)} {markdown_escape(source)}"
    if kind in {"caption", "caption_continuation"}:
        return f"*{markdown_escape(source)}*"
    if kind == "table" and source.lstrip().lower().startswith("<table"):
        validate_table_html(source)
        return f"```{{=html}}\n{source.strip()}\n```"
    if kind == "list":
        lines = [line for line in source.splitlines() if line.strip()]
        bullet_match = re.compile(r"^\s*[-*+]\s+(.*)$")
        bullets = [bullet_match.match(line) for line in lines]
        if lines and all(match is not None for match in bullets):
            return "\n".join(
                f"- {markdown_escape(match.group(1))}"
                for match in bullets
                if match is not None
            )
    return markdown_escape(source)


def make_source_only_markdown(block: dict[str, Any]) -> str:
    marker = response_marker(block, "source-only")
    return f"{marker}\n{source_only_markdown_body(block)}"


def make_source_only_latex(block: dict[str, Any]) -> str:
    anchor = f"\\SegmentAnchor{{{latex_escape(block['id'])}}}"
    source = prose_text(block["source"])
    kind = block["kind"]
    if kind == "code":
        return (
            f"{anchor}\n"
            "\\begin{Verbatim}[fontsize=\\small,breaklines=true,breakanywhere=true]\n"
            + block["source"]
            + "\n\\end{Verbatim}"
        )
    if kind == "heading":
        return f"{anchor}\n\\{latex_heading_command(source)}{{{latex_escape(source)}}}"
    if kind in {"caption", "caption_continuation"}:
        return (
            f"{anchor}\n\\begin{{center}}\\small\\itshape\n"
            f"{latex_escape(source)}\n\\end{{center}}"
        )
    return f"{anchor}\n{latex_escape(source)}"


def plan_visuals(
    work_dir: Path, output_dir: Path, visuals: list[dict[str, Any]]
) -> tuple[
    dict[str, str],
    list[dict[str, str]],
    list[tuple[Path, Path]],
]:
    assets_dir = output_dir / "assets"
    paths: dict[str, str] = {}
    copied: list[dict[str, str]] = []
    copies: list[tuple[Path, Path]] = []
    bindings = current_manifest_visual_bindings(work_dir, {"visuals": visuals})
    target_names: set[str] = set()
    for visual in bindings:
        source = work_relative_artifact_path(
            work_dir, visual.get("path"), label="visual asset path"
        )
        validate_artifact_file(source, boundary=work_dir)
        target = assets_dir / source.name
        target_key = target.name.casefold()
        if target_key in target_names:
            raise ValueError(f"visual output basename is duplicated: {target.name}")
        target_names.add(target_key)
        relative = f"assets/{target.name}"
        paths[visual["id"]] = relative
        copied.append(
            {
                "id": visual["id"],
                "path": relative,
                "sha256": visual["sha256"],
            }
        )
        copies.append((source, target))
    return paths, copied, copies


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministically merge audited translations into Markdown and XeLaTeX."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--basename")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    source_audit_path = work_dir / "source-audit.json"
    translation_audit_path = work_dir / "translation" / "translation-audit.json"
    merged_path = work_dir / "translation" / "translations-merged.jsonl"
    for path in (source_audit_path, translation_audit_path, merged_path):
        validate_artifact_file(path, boundary=work_dir, allow_missing=True)
        if not os.path.lexists(path):
            raise SystemExit(f"missing required artifact: {path}")
    _, source_binding_errors = validate_source_audit_binding(
        work_dir, source_audit_path
    )
    if source_binding_errors:
        _invalidate_output_audit(work_dir)
        raise SystemExit(
            "source audit bindings are stale: "
            + "; ".join(source_binding_errors)
        )
    translation_audit = _read_json(translation_audit_path, work_dir)
    if translation_audit.get("status") != "passed":
        raise SystemExit("translation audit is not passed")
    _, translation_binding_errors = validate_translation_audit_binding(
        work_dir, translation_audit_path
    )
    if translation_binding_errors:
        _invalidate_output_audit(work_dir)
        raise SystemExit(
            "translation audit bindings are stale: "
            + "; ".join(translation_binding_errors)
        )

    manifest_path = work_dir / "manifest.json"
    blocks_path = work_dir / "blocks.jsonl"
    for path in (manifest_path, blocks_path):
        validate_artifact_file(path, boundary=work_dir)
    manifest = _read_json(manifest_path, work_dir)
    blocks = _read_jsonl(blocks_path, work_dir)
    try:
        profile = load_work_profile(work_dir)
        (
            semantic_contract,
            ir_nodes_by_id,
            node_semantics,
            groups_by_node,
            semantic_role_inventory,
        ) = load_semantic_model(work_dir, blocks, profile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    generic_semantics = semantic_contract["source_schema_version"] == 2
    translations_list = _read_jsonl(merged_path, work_dir)
    translations = {entry["id"]: entry["translation"] for entry in translations_list}
    if len(translations) != len(translations_list):
        raise SystemExit("merged translations contain duplicate IDs")
    if generic_semantics:
        expected_translation_ids = {
            block["id"]
            for block in blocks
            if node_semantics[block["id"]]["output"] == "bilingual"
            and bool(
                ir_nodes_by_id[block["id"]].get(
                    "translatable", block.get("translatable")
                )
            )
        }
        if set(translations) != expected_translation_ids:
            missing = sorted(expected_translation_ids - set(translations))
            extra = sorted(set(translations) - expected_translation_ids)
            raise SystemExit(
                "audited translations do not match bilingual translatable nodes: "
                f"missing={missing}, extra={extra}"
            )

    default_stem = Path(manifest["source_pdf"]).stem + "_bilingual"
    try:
        basename = portable_artifact_basename(
            args.basename or default_stem, label="--basename"
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    output_dir = work_dir / "output"
    assets_dir = output_dir / "assets"
    output_exists = validate_artifact_tree(
        output_dir, work_dir, allow_missing=True
    ) is not None
    assets_exist = False
    if output_exists:
        assets_exist = validate_artifact_tree(
            assets_dir, work_dir, allow_missing=True
        ) is not None
    markdown_path = output_dir / f"{basename}.md"
    latex_path = output_dir / f"{basename}.tex"
    build_manifest_path = output_dir / "build-manifest.json"
    output_audit_path = output_dir / "output-audit.json"
    collisions: list[Path] = []
    if output_exists:
        for path in (
            markdown_path,
            latex_path,
            build_manifest_path,
            output_audit_path,
        ):
            validate_artifact_file(path, boundary=work_dir, allow_missing=True)
            if os.path.lexists(path):
                collisions.append(path)
        if assets_exist:
            collisions.append(assets_dir)
    if collisions and not args.force:
        raise SystemExit(
            "refusing to overwrite output artifacts; use --force: "
            + ", ".join(path.name for path in collisions)
        )

    visual_paths, copied_visuals, visual_copies = plan_visuals(
        work_dir, output_dir, manifest.get("visuals", [])
    )
    visuals_by_anchor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for visual in manifest.get("visuals", []):
        visuals_by_anchor[visual["anchor_id"]].append(visual)
    continuation_ids = (
        set()
        if generic_semantics
        else {
            continuation
            for visual in manifest.get("visuals", [])
            for continuation in visual.get("caption_continuation_ids", [])
        }
    )
    paragraph_followers: dict[str, list[str]] = {}
    follower_ids: set[str] = set()
    index = 0
    while index < len(blocks):
        leader = blocks[index]
        group = [leader]
        cursor = index + 1
        while cursor < len(blocks) and should_group_paragraphs(
            group[-1],
            blocks[cursor],
            node_semantics if generic_semantics else None,
            groups_by_node if generic_semantics else None,
        ):
            candidate = blocks[cursor]
            if (
                group[-1]["id"] in visuals_by_anchor
                or candidate["id"] in visuals_by_anchor
                or candidate["id"] in continuation_ids
            ):
                break
            group.append(candidate)
            cursor += 1
        if len(group) > 1:
            paragraph_followers[leader["id"]] = [item["id"] for item in group[1:]]
            follower_ids.update(item["id"] for item in group[1:])
            index = cursor
        else:
            index += 1
    blocks_by_id = {block["id"]: block for block in blocks}
    links_by_id = {item["id"]: item for item in manifest.get("links", [])}

    markdown_parts = [
        "<!-- Generated by make-bilingual-study-pdf; edit translations upstream, then rebuild. -->",
        f"<!-- source-sha256: {manifest['source_sha256']} -->",
    ]
    latex_parts: list[str] = []
    dispositions: dict[str, str] = {}
    current_page = None
    emitted_external_uris: set[str] = set()

    for block in blocks:
        block_id = block["id"]
        if block["page"] != current_page:
            current_page = block["page"]
            markdown_parts.append(
                f'\n<a id="source-page-{current_page}"></a>\n<!-- source-page: {current_page} -->'
            )
            latex_parts.append(f"\\SourcePage{{{current_page}}}")

        if generic_semantics:
            policy = node_semantics[block_id]["output"]
            anchored_visuals = visuals_by_anchor.get(block_id, [])
            if policy == "artifact-omitted":
                dispositions[block_id] = policy
                continue
            if block["kind"] == "visual_content":
                owners = [v for v in manifest.get("visuals", [])
                          if block_id in v.get("contained_block_ids", [])
                          and v.get("anchor_id") != block_id]
                if policy != "visual-once" or len(owners) != 1:
                    raise SystemExit(f"visual content {block_id} requires one containing visual")
                dispositions[block_id] = policy
                continue
            if block["kind"] == "caption_continuation":
                owners = [v for v in manifest.get("visuals", [])
                          if block_id in v.get("caption_continuation_ids", [])
                          and v.get("anchor_id") == block.get("caption_parent")]
                if policy != "bilingual" or len(owners) != 1:
                    raise SystemExit(f"caption continuation {block_id} requires one bound parent")
                dispositions[block_id] = policy
                continue
            if policy == "visual-once":
                if len(anchored_visuals) != 1:
                    raise SystemExit(
                        f"visual-once node {block_id} must own exactly one visual; "
                        f"found {len(anchored_visuals)}"
                    )
                visual = anchored_visuals[0]
                relative = visual_paths[visual["id"]]
                markdown_parts.append(
                    response_marker(block, "visual")
                    + "\n"
                    + visual_markdown(relative, f"Source visual from page {block['page']}")
                )
                latex_parts.append(
                    f"\\SegmentAnchor{{{latex_escape(block_id)}}}\n"
                    f"{visual_latex(relative)}"
                )
                dispositions[block_id] = policy
                continue
            if policy == "source-only":
                if anchored_visuals:
                    raise SystemExit(
                        f"source-only node {block_id} unexpectedly owns a visual; "
                        "model the visual as a visual-once node"
                    )
                markdown_parts.append(make_source_only_markdown(block))
                latex_parts.append(make_source_only_latex(block))
                dispositions[block_id] = policy
                continue
            if policy != "bilingual":
                raise SystemExit(f"unknown output disposition for {block_id}: {policy}")
            if not bool(
                ir_nodes_by_id[block_id].get(
                    "translatable", block.get("translatable")
                )
            ):
                raise SystemExit(
                    f"bilingual node {block_id} is not eligible for translation"
                )
            if anchored_visuals and block["kind"] not in {"caption", "math_with_text"}:
                raise SystemExit(
                    f"bilingual node {block_id} unexpectedly owns a visual; "
                    "split its caption from the visual node"
                )
            for visual in anchored_visuals:
                relative = visual_paths[visual["id"]]
                markdown_parts.append(
                    visual_markdown(
                        relative, f"Source visual from page {block['page']}"
                    )
                )
                latex_parts.append(visual_latex(relative))
            if block["kind"] == "math_with_text":
                markdown_parts.append(
                    make_translation_only_markdown(block, translations[block_id])
                )
                latex_parts.append(
                    make_translation_only_latex(block, translations[block_id])
                )
                dispositions[block_id] = policy
                continue
            if block_id in follower_ids:
                dispositions[block_id] = policy
                continue
            if block_id not in translations:
                raise SystemExit(f"missing audited translation for {block_id}")
            source_override = None
            translation = translations[block_id]
            paragraph_group = [
                blocks_by_id[item] for item in paragraph_followers.get(block_id, [])
            ]
            if paragraph_group:
                source_override = " ".join(
                    [block["source"]] + [item["source"] for item in paragraph_group]
                )
                translation = " ".join(
                    [translation]
                    + [translations[item["id"]] for item in paragraph_group]
                )
                for follower in paragraph_group:
                    markdown_parts.append(response_marker(follower, "grouped"))
                    latex_parts.append(
                        f"\\SegmentAnchor{{{latex_escape(follower['id'])}}}"
                    )
            continuation_blocks: list[dict[str, Any]] = []
            for visual in anchored_visuals:
                for continuation_id in visual.get("caption_continuation_ids", []):
                    continuation = blocks_by_id[continuation_id]
                    continuation_blocks.append(continuation)
            if continuation_blocks:
                source_override = " ".join(
                    [block["source"]] + [item["source"] for item in continuation_blocks]
                )
                translation = " ".join(
                    [translation] + [translations[item["id"]] for item in continuation_blocks]
                )
                for continuation in continuation_blocks:
                    markdown_parts.append(response_marker(continuation, "grouped"))

            markdown_parts.append(
                make_bilingual_markdown(
                    block, translation, source_override=source_override
                )
            )
            latex_parts.append(
                make_bilingual_latex(
                    block, translation, source_override=source_override
                )
            )
            dispositions[block_id] = policy
            continue

        if block["kind"] == "artifact":
            dispositions[block_id] = "artifact_omitted"
            continue
        if block["kind"] == "visual_content":
            dispositions[block_id] = "preserved_inside_visual"
            continue
        if block_id in follower_ids:
            dispositions[block_id] = "bilingual_grouped"
            continue
        if block_id in continuation_ids:
            dispositions[block_id] = "grouped_with_caption"
            continue

        anchored_visuals = visuals_by_anchor.get(block_id, [])
        for visual in anchored_visuals:
            relative = visual_paths[visual["id"]]
            visual_piece = visual_markdown(
                relative, f"Source visual from page {block['page']}"
            )
            if block["kind"] in {"image", "math"}:
                visual_piece = response_marker(block, "visual") + "\n" + visual_piece
            markdown_parts.append(visual_piece)
            latex_parts.append(
                f"\\SegmentAnchor{{{latex_escape(block_id)}}}\n{visual_latex(relative)}"
            )

        if block["kind"] in {"image", "math"}:
            if not anchored_visuals:
                raise SystemExit(f"{block['kind']} block lacks a visual crop: {block_id}")
            dispositions[block_id] = f"{block['kind']}_visual"
            continue
        if block["kind"] == "math_with_text":
            if not anchored_visuals:
                raise SystemExit(f"math-with-text block lacks a visual crop: {block_id}")
            if block_id not in translations:
                raise SystemExit(f"missing audited translation for {block_id}")
            markdown_parts.append(
                make_translation_only_markdown(block, translations[block_id])
            )
            latex_parts.append(
                make_translation_only_latex(block, translations[block_id])
            )
            dispositions[block_id] = "bilingual_math_visual"
            continue
        if block["kind"] == "code":
            markdown_parts.append(
                response_marker(block, "source-only")
                + "\n```text\n"
                + block["source"]
                + "\n```"
            )
            latex_parts.append(
                f"\\SegmentAnchor{{{latex_escape(block_id)}}}\n"
                "\\begin{Verbatim}[fontsize=\\small,breaklines=true,breakanywhere=true]\n"
                + block["source"]
                + "\n\\end{Verbatim}"
            )
            dispositions[block_id] = "source_code_once"
            continue

        if block_id not in translations:
            raise SystemExit(f"missing audited translation for {block_id}")
        source_override = None
        translation = translations[block_id]
        paragraph_group = [
            blocks_by_id[item] for item in paragraph_followers.get(block_id, [])
        ]
        if paragraph_group:
            source_override = " ".join(
                [block["source"]] + [item["source"] for item in paragraph_group]
            )
            translation = " ".join(
                [translation] + [translations[item["id"]] for item in paragraph_group]
            )
            for follower in paragraph_group:
                markdown_parts.append(response_marker(follower, "grouped"))
                latex_parts.append(
                    f"\\SegmentAnchor{{{latex_escape(follower['id'])}}}"
                )
        continuation_blocks: list[dict[str, Any]] = []
        for visual in anchored_visuals:
            for continuation_id in visual.get("caption_continuation_ids", []):
                continuation = blocks_by_id[continuation_id]
                continuation_blocks.append(continuation)
        if continuation_blocks:
            source_override = " ".join(
                [block["source"]] + [item["source"] for item in continuation_blocks]
            )
            translation = " ".join(
                [translation] + [translations[item["id"]] for item in continuation_blocks]
            )
            for continuation in continuation_blocks:
                markdown_parts.append(response_marker(continuation, "grouped"))

        markdown_parts.append(
            make_bilingual_markdown(block, translation, source_override=source_override)
        )
        latex_parts.append(
            make_bilingual_latex(block, translation, source_override=source_override)
        )
        dispositions[block_id] = "bilingual"

        block_uris = []
        for link_id in block.get("links", []):
            link = links_by_id.get(link_id, {})
            uri = link.get("uri")
            if uri:
                block_uris.append(uri)
                emitted_external_uris.add(uri)
        if block_uris:
            markdown_parts.append(
                "\n".join(
                    f"[Source link](<{uri}>)" for uri in sorted(set(block_uris))
                )
            )
            latex_parts.append(
                "\n".join(
                    f"\\url{{{latex_url(uri)}}}" for uri in sorted(set(block_uris))
                )
            )

    unaccounted = sorted(set(blocks_by_id) - set(dispositions))
    if unaccounted:
        raise SystemExit(f"unaccounted source blocks: {unaccounted}")

    external_uris = set(manifest.get("external_uris", []))
    if external_uris:
        markdown_parts.append("\n## Source links / 原文链接")
        latex_parts.append("\\section*{Source links / 原文链接}")
        for uri in sorted(external_uris):
            markdown_parts.append(f"- <{uri}>")
            latex_parts.append(f"\\url{{{latex_url(uri)}}}\\par")
        emitted_external_uris.update(external_uris)

    markdown_text = "\n\n".join(markdown_parts).rstrip() + "\n"
    template_path = Path(__file__).resolve().parent.parent / "assets" / "bilingual-template.tex"
    template = template_path.read_text(encoding="utf-8")
    first_heading = next(
        (prose_text(block["source"]) for block in blocks if block["kind"] == "heading"),
        Path(manifest["source_pdf"]).stem,
    )
    latex_text = (
        template.replace("%%__TITLE__%%", latex_escape(first_heading))
        .replace("%%__SOURCE_HASH__%%", manifest["source_sha256"])
        .replace("%%__BODY__%%", "\n\n".join(latex_parts))
    )
    if "%%__" in latex_text:
        raise SystemExit("unresolved template placeholder")

    output_problem_ids = sorted(
        set(problem_ids("\n".join(block["source"] for block in blocks)))
    )
    expected_problem_ids = sorted(manifest.get("problem_ids", []))
    if not generic_semantics and output_problem_ids != expected_problem_ids:
        raise SystemExit("Problem ID inventory changed before output generation")
    if emitted_external_uris != external_uris:
        raise SystemExit("not every external URI was emitted")

    semantic_dispositions = {
        block["id"]: node_semantics[block["id"]]["output"] for block in blocks
    }
    manifest_node_semantics = {
        block["id"]: {
            "role": node_semantics[block["id"]]["role"],
            "style": node_semantics[block["id"]]["style"],
            "output": node_semantics[block["id"]]["output"],
            "group_ids": sorted(groups_by_node.get(block["id"], set())),
            "relations": node_semantics[block["id"]]["relations"],
        }
        for block in blocks
    }
    build_manifest = {
        "schema_version": 1,
        "profile_id": manifest.get("profile", {}).get("id"),
        "profile_sha256": canonical_profile_sha256(profile),
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
        "source_pdf_sha256": manifest["source_sha256"],
        "source_manifest_sha256": sha256_artifact(manifest_path, boundary=work_dir),
        "source_blocks_sha256": sha256_artifact(blocks_path, boundary=work_dir),
        "source_audit_sha256": sha256_artifact(
            source_audit_path, boundary=work_dir
        ),
        "translation_audit_sha256": sha256_artifact(
            translation_audit_path, boundary=work_dir
        ),
        "translations_merged_sha256": sha256_artifact(
            merged_path, boundary=work_dir
        ),
        "markdown": markdown_path.name,
        "markdown_sha256": sha256_text(markdown_text),
        "latex": latex_path.name,
        "latex_sha256": sha256_text(latex_text),
        "assets": copied_visuals,
        "block_count": len(blocks),
        "disposition_counts": dict(Counter(dispositions.values())),
        "dispositions": dispositions,
        "semantic_contract_version": semantic_contract["contract_version"],
        "semantic_disposition_counts": dict(Counter(semantic_dispositions.values())),
        "semantic_dispositions": semantic_dispositions,
        "node_semantics": manifest_node_semantics,
        "role_inventory": semantic_role_inventory,
        "problem_ids": expected_problem_ids,
        "external_uris": sorted(external_uris),
    }

    # All inputs, metadata paths, and prior output entries are validated before
    # force invalidates a previous audit or clears the generated asset directory.
    _, source_binding_errors = validate_source_audit_binding(
        work_dir, source_audit_path
    )
    _, translation_binding_errors = validate_translation_audit_binding(
        work_dir, translation_audit_path
    )
    if source_binding_errors or translation_binding_errors:
        problems = [
            *(f"source: {message}" for message in source_binding_errors),
            *(f"translation: {message}" for message in translation_binding_errors),
        ]
        raise SystemExit(
            "output inputs changed before publication: " + "; ".join(problems)
        )
    refreshed_visuals = plan_visuals(
        work_dir, output_dir, manifest.get("visuals", [])
    )
    if refreshed_visuals != (visual_paths, copied_visuals, visual_copies):
        raise SystemExit("source visual plan changed before output publication")
    if args.force and output_exists:
        remove_artifact_file(output_audit_path, boundary=work_dir)
        if assets_exist:
            clear_artifact_directory(assets_dir, boundary=work_dir)
    prepare_artifact_directory(output_dir, boundary=work_dir)
    prepare_artifact_directory(assets_dir, boundary=work_dir)
    for source, target in visual_copies:
        atomic_copy_file(
            source,
            target,
            boundary=work_dir,
            source_boundary=work_dir,
        )
    atomic_write_text(markdown_path, markdown_text, boundary=work_dir)
    atomic_write_text(latex_path, latex_text, boundary=work_dir)
    _atomic_write_json(build_manifest_path, build_manifest, work_dir)
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "markdown": markdown_path.name,
                "latex": latex_path.name,
                "blocks": len(blocks),
                "assets": len(copied_visuals),
                "disposition_counts": build_manifest["disposition_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
