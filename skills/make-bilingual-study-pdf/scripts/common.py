#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from safe_artifacts import atomic_write_text, read_artifact_text


PROBLEM_RE = re.compile(r"(?:Problem|问题)\s*[（(]([a-z0-9_]+)", re.I)
PLACEHOLDER_RE = re.compile(r"⟦K\d{3}⟧")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", text).strip()


def repair_pdf_linebreaks(text: str) -> str:
    """Repair display-only PDF line wrapping while preserving paragraph breaks."""
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=[A-Za-z])-[ \t]*\n(?=[a-z])", "-", text)
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"(https?://[^\s\n]*[./_-])[ \t]*\n[ \t]*([a-z0-9][^\s\n]*)",
            r"\1\2",
            text,
        )
        text = re.sub(
            r"(https?://[^\s\n]*[./_-])[ \t]+([a-z0-9][^\s\n]*)",
            r"\1\2",
            text,
        )
    paragraphs = re.split(r"\n[ \t]*\n+", text)
    repaired = [re.sub(r"[ \t]*\n[ \t]*", " ", item).strip() for item in paragraphs]
    return "\n\n".join(item for item in repaired if item).strip()


def ascii_tokens(text: str) -> list[str]:
    return re.findall(
        r"[a-z_][a-z0-9_]*(?:'[a-z]+)?|\d+(?:\.\d+)*",
        normalize_text(text).lower(),
    )


def ngrams(items: list[str], size: int = 5) -> list[tuple[str, ...]]:
    return [
        tuple(items[index : index + size])
        for index in range(max(0, len(items) - size + 1))
    ]


def problem_ids(text: str) -> list[str]:
    return PROBLEM_RE.findall(text)


def contains_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text))


def placeholder_counts(text: str) -> Counter[str]:
    return Counter(PLACEHOLDER_RE.findall(text))


def relative_path(path: Path, start: Path) -> str:
    """Return a stable POSIX-style relative path for generated documents."""
    return Path(path).resolve().relative_to(Path(start).resolve()).as_posix()


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=path.parent,
    )


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for value in values
    )
    atomic_write_text(path, payload, boundary=path.parent)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"JSON number is outside the finite float range: {value}")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def validate_json_value(value: Any) -> None:
    """Require an acyclic tree of native, finite JSON values."""
    active_container_ids: set[int] = set()

    def visit(item: Any) -> None:
        item_type = type(item)
        if item is None or item_type in {bool, int}:
            return
        if item_type is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if item_type is str:
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise ValueError("JSON strings must not contain unpaired surrogates")
            return
        if item_type not in {list, dict}:
            raise ValueError(
                "JSON values must use only null, boolean, integer, finite float, "
                "string, array, and object types"
            )

        container_id = id(item)
        if container_id in active_container_ids:
            raise ValueError("JSON values must not contain circular references")
        active_container_ids.add(container_id)
        try:
            if item_type is list:
                for child in item:
                    visit(child)
            else:
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError("JSON object keys must be strings")
                    visit(key)
                    visit(child)
        finally:
            active_container_ids.remove(container_id)

    visit(value)


def json_loads_strict(payload: str) -> Any:
    """Parse strict finite JSON made only of Unicode scalar-value strings."""
    value = json.loads(
        payload,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_float=_parse_finite_json_float,
        parse_constant=_reject_json_constant,
    )
    validate_json_value(value)
    return value


def read_json(path: Path) -> Any:
    return json_loads_strict(
        read_artifact_text(path, boundary=path.parent, encoding="utf-8")
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = []
    payload = read_artifact_text(path, boundary=path.parent, encoding="utf-8")
    for line_number, raw in enumerate(payload.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            values.append(json_loads_strict(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return values
