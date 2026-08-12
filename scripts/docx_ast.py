#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path

from profile import load_profile, semantic_match

CJK_RE = re.compile(r"[\u3400-\u9fff]")
LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s+")
PROBLEM_BEGIN = "V2-PROBLEM-CALLOUT-BEGIN"
PROBLEM_END = "V2-PROBLEM-CALLOUT-END"


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


def transform(document: dict, profile: dict | None = None) -> dict:
    active_profile = profile or load_profile()
    blocks: list[dict] = []
    problem_count = 0
    for block in document.get("blocks", []):
        semantic = (
            semantic_match(active_profile, stringify(block))
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", default="assignment-en-zh")
    args = parser.parse_args()
    document = json.loads(args.input.read_text(encoding="utf-8"))
    try:
        profile = load_profile(args.profile)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    transformed = transform(document, profile)
    args.output.write_text(json.dumps(transformed, ensure_ascii=False) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
