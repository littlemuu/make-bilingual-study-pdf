#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import read_json, sha256_text, write_json


SKILL_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = SKILL_DIR / "profiles"
DEFAULT_PROFILE_ID = "assignment-en-zh"
PROFILE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REQUIRED_ROLES = {"problem", "example", "tip"}


def canonical_profile_sha256(profile: dict[str, Any]) -> str:
    payload = json.dumps(
        profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(payload)


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("profile must be a JSON object")
    if profile.get("schema_version") != 1:
        raise ValueError("unsupported profile schema_version")
    profile_id = profile.get("id")
    if not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id):
        raise ValueError("profile id must use lowercase hyphen-case")

    input_config = profile.get("input")
    if not isinstance(input_config, dict):
        raise ValueError("profile.input must be an object")
    if input_config.get("adapter") not in {"native-text-pdf"}:
        raise ValueError("unsupported input adapter")
    ratio = input_config.get("minimum_native_text_page_ratio")
    if not isinstance(ratio, (int, float)) or not 0 < float(ratio) <= 1:
        raise ValueError("minimum_native_text_page_ratio must be in (0, 1]")
    minimum_characters = input_config.get("minimum_text_characters_per_page")
    if not isinstance(minimum_characters, int) or minimum_characters < 1:
        raise ValueError("minimum_text_characters_per_page must be a positive integer")

    translation = profile.get("translation")
    if not isinstance(translation, dict):
        raise ValueError("profile.translation must be an object")
    if not isinstance(translation.get("target_language"), str):
        raise ValueError("profile.translation.target_language is required")
    if translation.get("reading_order") != "source-then-target":
        raise ValueError("V2.2 supports source-then-target reading order only")
    target_pattern = translation.get("target_text_pattern")
    if not isinstance(target_pattern, str):
        raise ValueError("profile.translation.target_text_pattern is required")
    try:
        re.compile(target_pattern)
    except re.error as exc:
        raise ValueError(f"invalid target_text_pattern: {exc}") from exc

    semantics = profile.get("semantics")
    groups = semantics.get("groups") if isinstance(semantics, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError("profile.semantics.groups must be a nonempty array")
    roles: set[str] = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"semantic group {index} must be an object")
        role = group.get("role")
        if not isinstance(role, str) or not PROFILE_ID_RE.fullmatch(role):
            raise ValueError(f"semantic group {index} has an invalid role")
        if role in roles:
            raise ValueError(f"duplicate semantic role: {role}")
        roles.add(role)
        for field in ("source_pattern", "target_pattern"):
            pattern = group.get(field)
            if not isinstance(pattern, str):
                raise ValueError(f"semantic group {role} is missing {field}")
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                raise ValueError(f"invalid {role} {field}: {exc}") from exc
        if group.get("style") not in REQUIRED_ROLES:
            raise ValueError(f"semantic group {role} has an unsupported style")
        if not isinstance(group.get("docx_regroup"), bool):
            raise ValueError(f"semantic group {role} docx_regroup must be boolean")

    primary_role = profile.get("qa", {}).get("primary_semantic_role")
    if primary_role not in roles:
        raise ValueError("qa.primary_semantic_role must name a semantic group role")
    docx = profile.get("render", {}).get("docx")
    if not isinstance(docx, dict):
        raise ValueError("profile.render.docx must be an object")
    for field in (
        "latin_font",
        "cjk_font",
        "code_font",
        "title",
        "header_label",
        "footer_label",
    ):
        if not isinstance(docx.get(field), str) or not docx[field].strip():
            raise ValueError(f"profile.render.docx.{field} is required")
    return profile


def load_profile(reference: str | Path | None = None) -> dict[str, Any]:
    if reference is None:
        path = PROFILE_DIR / f"{DEFAULT_PROFILE_ID}.json"
    else:
        candidate = Path(reference).expanduser()
        if candidate.is_file():
            path = candidate.resolve()
        else:
            name = str(reference)
            if not PROFILE_ID_RE.fullmatch(name):
                raise ValueError(f"profile does not exist: {reference}")
            path = PROFILE_DIR / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"profile does not exist: {path}")
    return validate_profile(read_json(path))


def bind_profile(
    work_dir: Path, reference: str | Path | None = None, *, force: bool = False
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    path = work_dir / "profile.json"
    requested = load_profile(reference)
    if path.is_file():
        existing = validate_profile(read_json(path))
        if canonical_profile_sha256(existing) == canonical_profile_sha256(requested):
            return existing
        if not force:
            raise ValueError(
                "work directory is bound to a different profile; use --force only when "
                "intentionally invalidating downstream artifacts"
            )
    write_json(path, requested)
    return requested


def load_work_profile(
    work_dir: Path, reference: str | Path | None = None
) -> dict[str, Any]:
    bound_path = work_dir.resolve() / "profile.json"
    if reference is None and bound_path.is_file():
        return validate_profile(read_json(bound_path))
    requested = load_profile(reference)
    if bound_path.is_file():
        bound = validate_profile(read_json(bound_path))
        if canonical_profile_sha256(bound) != canonical_profile_sha256(requested):
            raise ValueError("profile override does not match the work directory binding")
    return requested


def target_text_pattern(profile: dict[str, Any]) -> re.Pattern[str]:
    return re.compile(profile["translation"]["target_text_pattern"])


def semantic_match(
    profile: dict[str, Any], text: str, *, include_target: bool = True
) -> dict[str, Any] | None:
    for group in profile["semantics"]["groups"]:
        fields = ["source_pattern"]
        if include_target:
            fields.append("target_pattern")
        for field in fields:
            match = re.search(group[field], text.strip(), re.I)
            if match:
                result = dict(group)
                result["matched_language"] = "target" if field == "target_pattern" else "source"
                result["identifier"] = match.groupdict().get("identifier")
                return result
    return None


def semantic_group(profile: dict[str, Any], role: str) -> dict[str, Any]:
    for group in profile["semantics"]["groups"]:
        if group["role"] == role:
            return group
    raise ValueError(f"profile has no semantic role: {role}")
