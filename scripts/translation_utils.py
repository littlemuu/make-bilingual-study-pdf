#!/usr/bin/env python3
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable


PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "url",
        re.compile(
            r"https?://(?:[^\s<>\]\[{}]*[./_?&=%#-][ \t]*\n[ \t]*)*"
            r"[^\s<>\]\[{}]+"
        ),
        120,
    ),
    (
        "email",
        re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        115,
    ),
    (
        "path",
        re.compile(
            r"(?<!\w)(?:(?:[A-Za-z]:[\\/])|(?:\.{0,2}/))[A-Za-z0-9_./\\-]+"
        ),
        110,
    ),
    ("citation", re.compile(r"\[(?:\d+[a-z]?)(?:\s*[,;–-]\s*\d+[a-z]?)*\]"), 105),
    ("cli_flag", re.compile(r"(?<!\w)--?[a-z][a-z0-9_-]*"), 100),
    ("version", re.compile(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])"), 98),
    (
        "dotted_identifier",
        re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"),
        95,
    ),
    (
        "file_name",
        re.compile(
            r"\b[A-Za-z0-9_-]+\.(?:py|jsonl?|ya?ml|toml|md|tex|pdf|txt|csv|tsv|npy|npz|pt|pth|bin)\b",
            re.I,
        ),
        94,
    ),
    (
        "identifier",
        re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b"),
        90,
    ),
    (
        "dtype",
        re.compile(r"\b(?:bfloat16|float(?:16|32|64)|u?int(?:8|16|32|64))\b", re.I),
        88,
    ),
    (
        "number",
        re.compile(
            r"(?<![\w.])(?:\d+(?:\.\d+)?(?:e[+-]?\d+)?)(?:\s?(?:%|KiB|MiB|GiB|TiB|KB|MB|GB|TB|ms|s|Hz|kHz|MHz|GHz))?(?![\w.])",
            re.I,
        ),
        60,
    ),
]

PROBLEM_ID_RE = re.compile(r"\bProblem\s*[（(]\s*([a-z0-9_]+)", re.I)


def _candidate_spans(block: dict[str, Any]) -> list[dict[str, Any]]:
    source = block["source"]
    candidates: list[dict[str, Any]] = []
    for span in block.get("protected_spans", []):
        start, end = int(span["start"]), int(span["end"])
        if source[start:end].strip():
            candidates.append(
                {
                    "start": start,
                    "end": end,
                    "role": f"font_{span['role']}",
                    "priority": 100,
                }
            )

    for match in PROBLEM_ID_RE.finditer(source):
        candidates.append(
            {
                "start": match.start(1),
                "end": match.end(1),
                "role": "problem_id",
                "priority": 118,
            }
        )

    for role, pattern, priority in PATTERNS:
        for match in pattern.finditer(source):
            candidates.append(
                {
                    "start": match.start(),
                    "end": match.end(),
                    "role": role,
                    "priority": priority,
                }
            )
    return candidates


def select_non_overlapping_spans(block: dict[str, Any]) -> list[dict[str, Any]]:
    source = block["source"]
    accepted: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    candidates = sorted(
        _candidate_spans(block),
        key=lambda item: (
            -item["priority"],
            -(item["end"] - item["start"]),
            item["start"],
        ),
    )
    for item in candidates:
        start, end = item["start"], item["end"]
        if start < 0 or end > len(source) or end <= start:
            continue
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        accepted.append(item)
        occupied.append((start, end))
    return sorted(accepted, key=lambda item: item["start"])


def protect_source(block: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    source = block["source"]
    spans = select_non_overlapping_spans(block)
    protected: list[dict[str, Any]] = []
    pieces: list[str] = []
    cursor = 0
    for number, span in enumerate(spans, start=1):
        placeholder = f"⟦K{number:03d}⟧"
        pieces.append(source[cursor : span["start"]])
        pieces.append(placeholder)
        value = source[span["start"] : span["end"]]
        if span["role"] == "url":
            # A PDF may wrap a URL between glyph runs.  Store a canonical value so
            # restoring the placeholder cannot introduce a space into the link.
            value = re.sub(r"[ \t]*\n[ \t]*", "", value)
        protected.append(
            {
                "placeholder": placeholder,
                "value": value,
                "role": span["role"],
            }
        )
        cursor = span["end"]
    pieces.append(source[cursor:])
    return "".join(pieces), protected


def restore_placeholders(text: str, protected: Iterable[dict[str, Any]]) -> str:
    result = text
    for item in protected:
        result = result.replace(item["placeholder"], item["value"])
    return result


def expected_placeholder_counts(protected: Iterable[dict[str, Any]]) -> Counter[str]:
    return Counter(item["placeholder"] for item in protected)


def validate_glossary(glossary: dict[str, Any]) -> list[dict[str, Any]]:
    if glossary.get("schema_version") != 1:
        raise ValueError("glossary schema_version must be 1")
    if glossary.get("target_language") != "zh-CN":
        raise ValueError("glossary target_language must be zh-CN")
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, bool]] = set()
    for index, raw in enumerate(glossary.get("terms", []), start=1):
        source = raw.get("source")
        targets = raw.get("targets")
        case_sensitive = bool(raw.get("case_sensitive", False))
        enforce = bool(raw.get("enforce", False))
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"glossary term {index} has no source")
        if (
            not isinstance(targets, list)
            or not targets
            or any(not isinstance(item, str) or not item.strip() for item in targets)
        ):
            raise ValueError(f"glossary term {source!r} needs one or more targets")
        key = (source if case_sensitive else source.lower(), case_sensitive)
        if key in seen:
            raise ValueError(f"duplicate glossary source: {source!r}")
        seen.add(key)
        validated.append(
            {
                "source": source,
                "targets": targets,
                "case_sensitive": case_sensitive,
                "enforce": enforce,
                "notes": raw.get("notes", ""),
            }
        )
    return validated


def glossary_term_present(source: str, term: dict[str, Any]) -> bool:
    flags = 0 if term.get("case_sensitive") else re.I
    pattern = re.escape(term["source"])
    if re.match(r"\w", term["source"]):
        pattern = r"(?<!\w)" + pattern
    if re.search(r"\w$", term["source"]):
        pattern += r"(?!\w)"
    return bool(re.search(pattern, source, flags))
