#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
from pathlib import Path

from common import json_loads_strict
from profile import load_profile, load_work_profile, profile_contract, semantic_match
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    prepare_artifact_directory,
    read_artifact_text,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)

CJK_RE = re.compile(r"[\u3400-\u9fff]")
LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+")
PROBLEM_BEGIN = "V2-PROBLEM-CALLOUT-BEGIN"
PROBLEM_END = "V2-PROBLEM-CALLOUT-END"
GENERIC_BEGIN = "V23-CALLOUT-BEGIN"
GENERIC_END = "V23-CALLOUT-END"
SEGMENT_MARKER_RE = re.compile(r"<!--\s*bilingual:[^\s]+\s+id=([^\s]+)\s+")


def _bounded_cli_path(boundary: Path, path: Path, *, label: str) -> Path:
    boundary = lexical_absolute_path(boundary)
    candidate = lexical_absolute_path(path)
    try:
        relative = os.path.relpath(candidate, boundary)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} is outside WORK") from exc
    bounded = work_relative_artifact_path(
        boundary, Path(relative).as_posix(), label=label
    )
    if os.path.normcase(os.fspath(bounded)) != os.path.normcase(os.fspath(candidate)):
        raise ArtifactSafetyError(f"{label} is outside WORK")
    return bounded


def stringify(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(stringify(item) for item in value)
    if not isinstance(value, dict):
        return ""
    kind = value.get("t")
    content = value.get("c")
    if kind == "Str":
        return str(content)
    if kind == "Code":
        return str(content[1])
    if kind == "Math":
        return str(content[1])
    if kind in {"Space", "SoftBreak", "LineBreak"}:
        return " "
    return stringify(content)


def text_to_inlines(text: str) -> list[dict]:
    words = text.split()
    result: list[dict] = []
    for index, word in enumerate(words):
        if index:
            result.append({"t": "Space"})
        result.append({"t": "Str", "c": word})
    return result


def split_inlines_on_breaks(inlines: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = [[]]
    for inline in inlines:
        if inline.get("t") in {"SoftBreak", "LineBreak"}:
            if lines[-1]:
                lines.append([])
            continue
        lines[-1].append(copy.deepcopy(inline))
    return [line for line in lines if line]


def split_bilingual_slash(inlines: list[dict]) -> tuple[list[dict], list[dict]] | None:
    text = stringify(inlines)
    if not CJK_RE.search(text) or "/" not in text:
        return None
    slash_index = None
    for index, inline in enumerate(inlines):
        if inline.get("t") == "Str" and inline.get("c") == "/":
            slash_index = index
            break
        if inline.get("t") in {"Strong", "Emph"}:
            child = inline.get("c", [])
            nested = split_bilingual_slash(child)
            if nested:
                left, right = nested
                return ([{"t": inline["t"], "c": left}], [{"t": inline["t"], "c": right}])
    if slash_index is None:
        return None
    left = copy.deepcopy(inlines[:slash_index])
    right = copy.deepcopy(inlines[slash_index + 1 :])
    while left and left[-1].get("t") == "Space":
        left.pop()
    while right and right[0].get("t") == "Space":
        right.pop(0)
    if not left or not right or CJK_RE.search(stringify(left)) or not CJK_RE.search(stringify(right)):
        return None
    return left, right


def clean_literal_emphasis(inlines: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for inline in inlines:
        item = copy.deepcopy(inline)
        if item.get("t") in {"Strong", "Emph", "Span", "Quoted", "Link", "Image"}:
            content = item.get("c")
            if item["t"] in {"Span", "Link", "Image"}:
                item["c"][1] = clean_literal_emphasis(content[1])
            elif item["t"] == "Quoted":
                item["c"][1] = clean_literal_emphasis(content[1])
            else:
                item["c"] = clean_literal_emphasis(content)
        elif item.get("t") == "Str":
            value = str(item.get("c", ""))
            match = re.fullmatch(r"\*\*(.+)\*\*", value)
            if match:
                item = {"t": "Strong", "c": [{"t": "Str", "c": match.group(1)}]}
            elif "**" in value:
                # Some legacy bilingual Markdown keeps the Chinese half of an
                # emphasized label as literal asterisks after slash-splitting.
                # Preserve semantic text without leaking Markdown punctuation
                # into the rendered DOCX. Code spans are separate AST nodes and
                # therefore remain untouched (including Python's ** operator).
                item["c"] = value.replace("**", "")
        cleaned.append(item)
    return cleaned


def split_para(block: dict) -> list[dict]:
    kind = block.get("t")
    if kind not in {"Para", "Plain"}:
        return [copy.deepcopy(block)]
    result: list[dict] = []
    for line in split_inlines_on_breaks(block.get("c", [])):
        line = clean_literal_emphasis(line)
        slash = split_bilingual_slash(line)
        if slash:
            result.append({"t": kind, "c": slash[0]})
            result.append({"t": kind, "c": slash[1]})
        else:
            result.append({"t": kind, "c": line})
    return result


def classify(block: dict) -> str:
    text = stringify(block)
    if CJK_RE.search(text):
        return "zh"
    return "en"


def split_list(block: dict) -> tuple[dict | None, dict | None]:
    kind = block.get("t")
    if kind not in {"BulletList", "OrderedList"}:
        return None, None
    items = block["c"] if kind == "BulletList" else block["c"][1]
    english_items: list[list[dict]] = []
    chinese_items: list[list[dict]] = []
    for item in items:
        english: list[dict] = []
        chinese: list[dict] = []
        for child in item:
            for piece in split_para(child):
                (chinese if classify(piece) == "zh" else english).append(piece)
        if english:
            english_items.append(english)
        if chinese:
            chinese_items.append(chinese)
    if kind == "BulletList":
        return (
            {"t": kind, "c": english_items} if english_items else None,
            {"t": kind, "c": chinese_items} if chinese_items else None,
        )
    attrs = copy.deepcopy(block["c"][0])
    return (
        {"t": kind, "c": [attrs, english_items]} if english_items else None,
        {"t": kind, "c": [attrs, chinese_items]} if chinese_items else None,
    )


def group_problem(block: dict) -> dict:
    english: list[dict] = []
    chinese: list[dict] = []
    for child in block.get("c", []):
        if child.get("t") in {"BulletList", "OrderedList"}:
            en_list, zh_list = split_list(child)
            if en_list:
                english.append(en_list)
            if zh_list:
                chinese.append(zh_list)
            continue
        for piece in split_para(child):
            (chinese if classify(piece) == "zh" else english).append(piece)
    if not english or not chinese:
        return copy.deepcopy(block)
    separator = {"t": "HorizontalRule"}
    begin = {"t": "Para", "c": text_to_inlines(PROBLEM_BEGIN)}
    end = {"t": "Para", "c": text_to_inlines(PROBLEM_END)}
    return {"t": "BlockQuote", "c": [begin] + english + [separator] + chinese + [end]}


def normalize_block(block: dict) -> list[dict]:
    kind = block.get("t")
    if kind in {"Para", "Plain"}:
        return split_para(block)
    if kind == "BlockQuote":
        children: list[dict] = []
        for child in block.get("c", []):
            children.extend(normalize_block(child))
        return [{"t": kind, "c": children}]
    if kind == "BulletList":
        items: list[list[dict]] = []
        for item in block.get("c", []):
            normalized: list[dict] = []
            for child in item:
                normalized.extend(normalize_block(child))
            items.append(normalized)
        return [{"t": kind, "c": items}]
    if kind == "OrderedList":
        attrs, source_items = block.get("c", [None, []])
        items: list[list[dict]] = []
        for item in source_items:
            normalized: list[dict] = []
            for child in item:
                normalized.extend(normalize_block(child))
            items.append(normalized)
        return [{"t": kind, "c": [copy.deepcopy(attrs), items]}]
    if kind == "Div":
        attrs, source_children = block.get("c", [None, []])
        children: list[dict] = []
        for child in source_children:
            children.extend(normalize_block(child))
        return [{"t": kind, "c": [copy.deepcopy(attrs), children]}]
    return [copy.deepcopy(block)]


def strip_duplicate_heading_number(inlines: list[dict], expected: str | None) -> list[dict]:
    if not expected:
        return inlines
    text = stringify(inlines).strip()
    match = LEADING_NUMBER_RE.match(text)
    if not match or match.group(1) != expected:
        return inlines
    return text_to_inlines(text[match.end() :])


def transform_headers(blocks: list[dict]) -> list[dict]:
    output: list[dict] = []
    previous_number: str | None = None
    previous_level: int | None = None
    previous_text: str | None = None
    previous_was_english = False
    for block in blocks:
        if block.get("t") != "Header":
            output.append(block)
            if block.get("t") not in {"HorizontalRule"}:
                previous_number = None
                previous_level = None
                previous_text = None
                previous_was_english = False
            continue
        level, attrs, inlines = block["c"]
        slash = split_bilingual_slash(inlines)
        if slash:
            english, chinese = slash
            english_text = stringify(english).strip()
            number_match = LEADING_NUMBER_RE.match(english_text)
            number = number_match.group(1) if number_match else None
            chinese = strip_duplicate_heading_number(chinese, number)
            output.append({"t": "Header", "c": [level, attrs, english]})
            output.append({"t": "Para", "c": chinese})
            previous_number = None
            previous_level = None
            previous_text = None
            previous_was_english = False
            continue
        text = stringify(inlines).strip()
        if previous_was_english and previous_level == level and previous_text == text:
            # A bilingual source can legitimately use the same spelling on
            # both sides (for example "AdamW / AdamW").  Once flattened, that
            # becomes two identical adjacent headers; keep just one.
            continue
        if CJK_RE.search(text) and previous_was_english and previous_level == level:
            inlines = strip_duplicate_heading_number(inlines, previous_number)
            output.append({"t": "Para", "c": inlines})
            previous_number = None
            previous_level = None
            previous_text = None
            previous_was_english = False
            continue
        output.append(block)
        match = LEADING_NUMBER_RE.match(text)
        previous_number = match.group(1) if match else None
        previous_level = level
        previous_text = text
        previous_was_english = not bool(CJK_RE.search(text))
    return output


def _transform_v1(document: dict, profile: dict) -> dict:
    """The byte-behavior compatible V2.2 assignment transformation."""
    blocks: list[dict] = []
    problem_count = 0
    for block in document.get("blocks", []):
        semantic = (
            semantic_match(profile, stringify(block))
            if block.get("t") == "BlockQuote"
            else None
        )
        if semantic and semantic["role"] == "problem" and semantic["docx_regroup"]:
            blocks.append(group_problem(block))
            problem_count += 1
        else:
            blocks.extend(normalize_block(block))
    document = copy.deepcopy(document)
    document["blocks"] = transform_headers(blocks)
    document.setdefault("meta", {})["v2-problem-group-count"] = {
        "t": "MetaString",
        "c": str(problem_count),
    }
    return document


def _segment_node_id(block: dict) -> str | None:
    if block.get("t") not in {"RawBlock", "RawInline"}:
        return None
    content = block.get("c")
    if not isinstance(content, list) or len(content) != 2:
        return None
    rendered = str(content[1])
    match = SEGMENT_MARKER_RE.search(rendered)
    return match.group(1) if match else None


def _split_segments(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    prefix: list[dict] = []
    segments: list[dict] = []
    current: dict | None = None
    for block in blocks:
        node_id = _segment_node_id(block)
        if node_id is not None:
            current = {"node_id": node_id, "marker": copy.deepcopy(block), "blocks": []}
            segments.append(current)
            continue
        if current is None:
            prefix.append(copy.deepcopy(block))
        else:
            current["blocks"].append(copy.deepcopy(block))
    return prefix, segments


def _generic_marker(
    kind: str, role: str, style: str, membership: str, group_id: str
) -> dict:
    return {
        "t": "Para",
        "c": text_to_inlines(f"{kind}|{role}|{style}|{membership}|{group_id}"),
    }


def _group_structural_segments(
    segments: list[dict], *, role: str, style: str, membership: str, group_id: str
) -> dict:
    english: list[dict] = []
    chinese: list[dict] = []
    target_blocks_total = 0
    for segment in segments:
        source_blocks = 0
        target_blocks = 0
        for block in segment["blocks"]:
            if block.get("t") == "BlockQuote":
                target_blocks += 1
                for child in block.get("c", []):
                    chinese.extend(normalize_block(child))
            else:
                source_blocks += 1
                english.extend(normalize_block(block))
        target_blocks_total += target_blocks
        if (source_blocks == 0 and target_blocks != 0) or target_blocks > 1:
            raise ValueError(
                f"structural group {group_id} member {segment['node_id']} "
                "must be an empty grouped alias or materialize as source with at most "
                "one target BlockQuote"
            )
    if not english or not chinese or target_blocks_total == 0:
        raise ValueError(f"structural group {group_id} is not bilingual")
    if membership == "anchor-only" and (
        len(segments) != 1
        or target_blocks_total != 1
    ):
        raise ValueError(
            f"anchor-only structural group {group_id} must contain exactly its anchor "
            "source and one target BlockQuote"
        )
    return {
        "t": "BlockQuote",
        "c": [
            _generic_marker(GENERIC_BEGIN, role, style, membership, group_id),
            *english,
            {"t": "HorizontalRule"},
            *chinese,
            _generic_marker(GENERIC_END, role, style, membership, group_id),
        ],
    }


def _transform_v2(
    document: dict,
    profile: dict,
    semantic_groups: list[dict] | None,
) -> dict:
    if semantic_groups is None:
        raise ValueError("schema V2 DOCX transformation requires frozen semantic groups")
    contract = profile_contract(profile)
    roles = {item["role"]: item for item in contract["roles"]}
    structural_groups: dict[str, dict] = {}
    member_owner: dict[str, str] = {}
    grouped_counts = {role: 0 for role in roles}
    complete_counts = {role: 0 for role in roles}
    anchor_only_counts = {role: 0 for role in roles}
    for group in semantic_groups:
        if not isinstance(group, dict):
            raise ValueError("semantic groups must be objects")
        membership = group.get("membership")
        if membership not in {"complete", "anchor-only"}:
            continue
        role = group.get("role")
        spec = roles.get(role)
        members = group.get("member_node_ids")
        group_id = group.get("id")
        if (
            spec is None
            or spec["grouping"] != "structural-container"
            or not isinstance(group_id, str)
            or not group_id
            or not isinstance(members, list)
            or not members
        ):
            if spec is not None and spec["grouping"] != "structural-container":
                continue
            raise ValueError(f"invalid structural group: {group_id!r}")
        if membership == "anchor-only" and members != [group.get("anchor_node_id")]:
            raise ValueError(
                f"anchor-only structural group {group_id} must contain only its anchor"
            )
        structural_groups[group_id] = {
            "role": role,
            "style": spec["style"],
            "members": list(members),
            "membership": membership,
        }
        grouped_counts[role] += 1
        (complete_counts if membership == "complete" else anchor_only_counts)[role] += 1
        for member in members:
            if member in member_owner:
                raise ValueError(f"node belongs to multiple structural groups: {member}")
            member_owner[member] = group_id

    prefix, segments = _split_segments(document.get("blocks", []))
    segment_indices: dict[str, int] = {}
    for index, segment in enumerate(segments):
        node_id = segment["node_id"]
        if node_id in segment_indices:
            raise ValueError(f"duplicate bilingual segment marker: {node_id}")
        segment_indices[node_id] = index

    starts: dict[int, tuple[dict, list[dict]]] = {}
    covered: set[int] = set()
    for group_id, group in structural_groups.items():
        try:
            indices = [segment_indices[member] for member in group["members"]]
        except KeyError as exc:
            raise ValueError(
                f"structural group {group_id} member is absent from Markdown: {exc.args[0]}"
            ) from exc
        contiguous = list(range(min(indices), min(indices) + len(indices)))
        if sorted(indices) != contiguous:
            raise ValueError(
                f"structural group {group_id} members are not contiguous"
            )
        if indices != contiguous:
            indexed_segments = [segments[index] for index in contiguous]
            if (
                indices[0] != contiguous[-1]
                or any(segment["blocks"] for segment in indexed_segments[:-1])
            ):
                raise ValueError(
                    f"structural group {group_id} marker order is not source order"
                )
        if any(index in covered for index in indices):
            raise ValueError(f"structural group overlaps another group: {group_id}")
        covered.update(indices)
        starts[min(indices)] = (group, [segments[index] for index in indices])

    output = prefix
    index = 0
    while index < len(segments):
        start = starts.get(index)
        if start is not None:
            group, members = start
            group_id = member_owner[members[0]["node_id"]]
            output.append(
                _group_structural_segments(
                    members,
                    role=group["role"],
                    style=group["style"],
                    membership=group["membership"],
                    group_id=group_id,
                )
            )
            index += len(members)
            continue
        if index in covered:
            index += 1
            continue
        segment = segments[index]
        output.append(segment["marker"])
        for block in segment["blocks"]:
            output.extend(normalize_block(block))
        index += 1

    transformed = copy.deepcopy(document)
    transformed["blocks"] = transform_headers(output)
    transformed.setdefault("meta", {})["v23-structural-group-counts"] = {
        "t": "MetaString",
        "c": json.dumps(grouped_counts, sort_keys=True, separators=(",", ":")),
    }
    transformed["meta"]["v23-complete-structural-group-counts"] = {
        "t": "MetaString",
        "c": json.dumps(complete_counts, sort_keys=True, separators=(",", ":")),
    }
    transformed["meta"]["v23-anchor-only-callout-counts"] = {
        "t": "MetaString",
        "c": json.dumps(anchor_only_counts, sort_keys=True, separators=(",", ":")),
    }
    return transformed


def transform(
    document: dict,
    profile: dict | None = None,
    *,
    semantic_groups: list[dict] | None = None,
) -> dict:
    active_profile = profile or load_profile()
    if active_profile.get("schema_version") == 1:
        return _transform_v1(document, active_profile)
    return _transform_v2(document, active_profile, semantic_groups)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    try:
        if args.work_dir is None:
            input_path = args.input
            output_path = args.output
            document = json.loads(input_path.read_text(encoding="utf-8"))
            profile = load_profile(args.profile)
            work_dir = None
        else:
            work_dir = lexical_absolute_path(args.work_dir)
            validate_artifact_directory(work_dir)
            stage_dir = work_dir / "output" / "docx-build"
            validate_artifact_tree(stage_dir, boundary=work_dir, allow_missing=False)
            input_path = _bounded_cli_path(
                stage_dir, args.input, label="DOCX AST input"
            )
            output_path = _bounded_cli_path(
                stage_dir, args.output, label="DOCX AST output"
            )
            input_key = tuple(part.casefold() for part in input_path.relative_to(stage_dir).parts)
            output_key = tuple(part.casefold() for part in output_path.relative_to(stage_dir).parts)
            if input_key == output_key:
                raise ArtifactSafetyError("DOCX AST input and output roles must be distinct")
            validate_artifact_file(input_path, boundary=stage_dir)
            if os.path.lexists(output_path.parent):
                validate_artifact_file(
                    output_path, boundary=stage_dir, allow_missing=True
                )
            document = json_loads_strict(
                read_artifact_text(input_path, boundary=stage_dir)
            )
            profile = load_work_profile(work_dir, args.profile)
    except (ArtifactSafetyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if not isinstance(document, dict):
        raise SystemExit("DOCX AST input must be a JSON object")
    transformed = transform(document, profile)
    rendered = json.dumps(transformed, ensure_ascii=False, allow_nan=False) + "\n"
    if work_dir is None:
        output_path.write_text(rendered, encoding="utf-8")
    else:
        prepare_artifact_directory(output_path.parent, boundary=stage_dir)
        atomic_write_text(output_path, rendered, boundary=stage_dir)
    print(output_path)


if __name__ == "__main__":
    try:
        main()
    except ArtifactSafetyError as exc:
        raise SystemExit(str(exc)) from exc
