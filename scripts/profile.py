#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from common import read_json, sha256_text, write_json
from semantic_registry import (
    AUXILIARY_ROLES,
    GROUPING_MODES,
    OUTPUT_DISPOSITIONS,
    SELECTOR_FIELDS,
    get_constraint,
    get_style,
)


SKILL_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = SKILL_DIR / "profiles"
DEFAULT_PROFILE_ID = "assignment-en-zh"
PROFILE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SELECTOR_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
LEGACY_STYLE_IDS = frozenset({"problem", "example", "tip"})
DOCX_FIELDS = (
    "latin_font",
    "cjk_font",
    "code_font",
    "title",
    "header_label",
    "footer_label",
)


def canonical_profile_sha256(profile: dict[str, Any]) -> str:
    """Hash the raw Profile, never its derived compatibility contract."""
    payload = json.dumps(
        profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256_text(payload)


def _valid_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(PROFILE_ID_RE.fullmatch(value))


def _valid_selector_token(value: Any) -> bool:
    return isinstance(value, str) and bool(SELECTOR_TOKEN_RE.fullmatch(value))


def _compile_pattern(pattern: Any, field: str) -> None:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(f"{field} must be a nonempty regex string")
    try:
        re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"invalid {field}: {exc}") from exc


def _validate_common(profile: dict[str, Any], *, schema_version: int) -> None:
    profile_id = profile.get("id")
    if not _valid_identifier(profile_id):
        raise ValueError("profile id must use lowercase hyphen-case")

    input_config = profile.get("input")
    if not isinstance(input_config, dict):
        raise ValueError("profile.input must be an object")
    adapter_id = input_config.get("adapter")
    if not isinstance(adapter_id, str):
        raise ValueError("profile.input.adapter is required")
    # Import lazily so the Profile data model stays independent of adapter
    # implementations while still validating against the one authoritative registry.
    from adapters import get_adapter

    get_adapter(adapter_id)
    ratio = input_config.get("minimum_native_text_page_ratio")
    if (
        not isinstance(ratio, (int, float))
        or isinstance(ratio, bool)
        or not 0 < float(ratio) <= 1
    ):
        raise ValueError("minimum_native_text_page_ratio must be in (0, 1]")
    minimum_characters = input_config.get("minimum_text_characters_per_page")
    if (
        not isinstance(minimum_characters, int)
        or isinstance(minimum_characters, bool)
        or minimum_characters < 1
    ):
        raise ValueError("minimum_text_characters_per_page must be a positive integer")
    if schema_version == 2 and (
        not isinstance(input_config.get("source_language"), str)
        or not input_config["source_language"].strip()
    ):
        raise ValueError("profile.input.source_language is required")

    translation = profile.get("translation")
    if not isinstance(translation, dict):
        raise ValueError("profile.translation must be an object")
    if not isinstance(translation.get("target_language"), str):
        raise ValueError("profile.translation.target_language is required")
    if translation.get("reading_order") != "source-then-target":
        raise ValueError("only source-then-target reading order is supported")
    _compile_pattern(
        translation.get("target_text_pattern"),
        "profile.translation.target_text_pattern",
    )
    if schema_version == 2 and (
        not isinstance(translation.get("policy"), str)
        or not translation["policy"].strip()
    ):
        raise ValueError("profile.translation.policy is required")

    docx = profile.get("render", {}).get("docx")
    if not isinstance(docx, dict):
        raise ValueError("profile.render.docx must be an object")
    for field in DOCX_FIELDS:
        if not isinstance(docx.get(field), str) or not docx[field].strip():
            raise ValueError(f"profile.render.docx.{field} is required")


def _validate_v1(profile: dict[str, Any]) -> None:
    semantics = profile.get("semantics")
    groups = semantics.get("groups") if isinstance(semantics, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError("profile.semantics.groups must be a nonempty array")
    roles: set[str] = set()
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"semantic group {index} must be an object")
        role = group.get("role")
        if not _valid_identifier(role):
            raise ValueError(f"semantic group {index} has an invalid role")
        if role in roles:
            raise ValueError(f"duplicate semantic role: {role}")
        roles.add(role)
        for field in ("source_pattern", "target_pattern"):
            _compile_pattern(group.get(field), f"{role} {field}")
        style = group.get("style")
        style_spec = get_style(style)
        if style not in LEGACY_STYLE_IDS or not style_spec.legacy_assignment_style:
            raise ValueError(f"semantic group {role} has an unsupported V1 style")
        if not isinstance(group.get("docx_regroup"), bool):
            raise ValueError(f"semantic group {role} docx_regroup must be boolean")

    primary_role = profile.get("qa", {}).get("primary_semantic_role")
    if primary_role not in roles:
        raise ValueError("qa.primary_semantic_role must name a semantic group role")


def _validate_string_list(value: Any, field: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not _valid_selector_token(item) for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} must be a nonempty array of unique identifiers")


def _validate_selector(selector: Any, *, role: str, index: int) -> None:
    field = f"semantic role {role} selector {index}"
    if not isinstance(selector, dict) or not selector:
        raise ValueError(f"{field} must be a nonempty object")
    unknown = sorted(set(selector) - SELECTOR_FIELDS)
    if unknown:
        raise ValueError(f"{field} has unsupported fields: {unknown}")
    if "adapter_role" in selector and not _valid_selector_token(selector["adapter_role"]):
        raise ValueError(f"{field}.adapter_role must be an identifier")
    for name in ("node_types", "sub_types"):
        if name in selector:
            _validate_string_list(selector[name], f"{field}.{name}")
    if "text_levels" in selector:
        levels = selector["text_levels"]
        if (
            not isinstance(levels, list)
            or not levels
            or any(
                not isinstance(level, int) or isinstance(level, bool) or level < 0
                for level in levels
            )
            or len(levels) != len(set(levels))
        ):
            raise ValueError(
                f"{field}.text_levels must be a nonempty array of unique nonnegative integers"
            )
    for name in ("source_pattern", "target_pattern"):
        if name in selector:
            _compile_pattern(selector[name], f"{field}.{name}")
    if "target_pattern" in selector and "source_pattern" not in selector:
        raise ValueError(f"{field}.target_pattern requires source_pattern")
    source_evidence = {
        "adapter_role",
        "node_types",
        "sub_types",
        "text_levels",
        "source_pattern",
    }
    if not source_evidence.intersection(selector):
        raise ValueError(f"{field} has no source-side matching evidence")


def _validate_v2(profile: dict[str, Any]) -> None:
    semantics = profile.get("semantics")
    if not isinstance(semantics, dict):
        raise ValueError("profile.semantics must be an object")
    roles_value = semantics.get("roles")
    if not isinstance(roles_value, list) or not roles_value:
        raise ValueError("profile.semantics.roles must be a nonempty array")

    roles: set[str] = set()
    selector_keys: set[str] = set()
    for index, role_spec in enumerate(roles_value):
        if not isinstance(role_spec, dict):
            raise ValueError(f"semantic role {index} must be an object")
        unknown = sorted(
            set(role_spec) - {"role", "selectors", "style", "grouping", "output"}
        )
        if unknown:
            raise ValueError(f"semantic role {index} has unsupported fields: {unknown}")
        role = role_spec.get("role")
        if not _valid_identifier(role):
            raise ValueError(f"semantic role {index} has an invalid role")
        if role in roles:
            raise ValueError(f"duplicate semantic role: {role}")
        roles.add(role)

        selectors = role_spec.get("selectors")
        if not isinstance(selectors, list) or not selectors:
            raise ValueError(f"semantic role {role} selectors must be a nonempty array")
        for selector_index, selector in enumerate(selectors):
            _validate_selector(selector, role=role, index=selector_index)
            selector_key = json.dumps(
                selector, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if selector_key in selector_keys:
                raise ValueError(
                    f"semantic selector is assigned more than once: {role}[{selector_index}]"
                )
            selector_keys.add(selector_key)

        style = role_spec.get("style")
        if not isinstance(style, str):
            raise ValueError(f"semantic role {role} style is required")
        style_spec = get_style(style)
        grouping = role_spec.get("grouping")
        if grouping not in GROUPING_MODES:
            raise ValueError(f"semantic role {role} has an unsupported grouping mode")
        output = role_spec.get("output")
        if output not in OUTPUT_DISPOSITIONS:
            raise ValueError(f"semantic role {role} has an unsupported output disposition")
        if grouping == "structural-container":
            if not style_spec.supports_structural_container:
                raise ValueError(
                    f"semantic role {role} style does not support structural containers"
                )
            if output != "bilingual":
                raise ValueError(
                    f"semantic role {role} structural containers must be bilingual"
                )

    auxiliary = semantics.get("auxiliary_dispositions")
    if not isinstance(auxiliary, dict):
        raise ValueError("profile.semantics.auxiliary_dispositions must be an object")
    auxiliary_keys = set(auxiliary)
    if auxiliary_keys != AUXILIARY_ROLES:
        missing = sorted(AUXILIARY_ROLES - auxiliary_keys)
        extra = sorted(auxiliary_keys - AUXILIARY_ROLES)
        raise ValueError(
            "profile.semantics.auxiliary_dispositions must explicitly cover "
            f"all auxiliary roles (missing={missing}, extra={extra})"
        )
    for role, disposition in auxiliary.items():
        if disposition not in OUTPUT_DISPOSITIONS:
            raise ValueError(
                f"auxiliary role {role} has an unsupported output disposition"
            )

    qa = profile.get("qa")
    if not isinstance(qa, dict):
        raise ValueError("profile.qa must be an object")
    inventory = qa.get("role_inventory")
    if not isinstance(inventory, dict):
        raise ValueError("profile.qa.role_inventory must be an object")
    inventory_keys = set(inventory)
    if inventory_keys != roles:
        missing = sorted(roles - inventory_keys)
        extra = sorted(inventory_keys - roles)
        raise ValueError(
            "profile.qa.role_inventory must exactly cover semantic roles "
            f"(missing={missing}, extra={extra})"
        )
    for role, bounds in inventory.items():
        if not isinstance(bounds, dict) or set(bounds) != {"minimum", "maximum"}:
            raise ValueError(
                f"profile.qa.role_inventory.{role} must contain minimum and maximum"
            )
        minimum = bounds["minimum"]
        maximum = bounds["maximum"]
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < 0
        ):
            raise ValueError(f"role inventory {role} minimum must be nonnegative")
        if maximum is not None and (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < minimum
        ):
            raise ValueError(
                f"role inventory {role} maximum must be null or at least minimum"
            )

    constraints = qa.get("constraints")
    if not isinstance(constraints, list):
        raise ValueError("profile.qa.constraints must be an array")
    if len(constraints) != len(set(constraints)):
        raise ValueError("profile.qa.constraints must not contain duplicates")
    for constraint_id in constraints:
        if not isinstance(constraint_id, str):
            raise ValueError("profile.qa.constraints values must be identifiers")
        get_constraint(constraint_id)


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(profile, dict):
        raise ValueError("profile must be a JSON object")
    schema_version = profile.get("schema_version")
    if schema_version not in {1, 2}:
        raise ValueError("unsupported profile schema_version")
    _validate_common(profile, schema_version=schema_version)
    if schema_version == 1:
        _validate_v1(profile)
    else:
        _validate_v2(profile)
    return profile


def _profile_contract_validated(profile: dict[str, Any]) -> dict[str, Any]:
    roles: list[dict[str, Any]] = []
    inventory: dict[str, dict[str, Any]] = {}
    if profile["schema_version"] == 1:
        for group in profile["semantics"]["groups"]:
            grouping = (
                "structural-container" if group["docx_regroup"] else "none"
            )
            normalized = {
                "role": group["role"],
                "selectors": [
                    {
                        "source_pattern": group["source_pattern"],
                        "target_pattern": group["target_pattern"],
                    }
                ],
                "style": group["style"],
                "grouping": grouping,
                "output": "bilingual",
                # Compatibility alias for existing DOCX consumers.
                "docx_regroup": group["docx_regroup"],
                "source_pattern": group["source_pattern"],
                "target_pattern": group["target_pattern"],
            }
            roles.append(normalized)
            inventory[group["role"]] = {
                "minimum": 0,
                "maximum": None,
                "style": group["style"],
                "grouping": grouping,
                "output": "bilingual",
            }
        auxiliary = {role: "artifact-omitted" for role in sorted(AUXILIARY_ROLES)}
        constraints: list[str] = []
    else:
        for role_spec in profile["semantics"]["roles"]:
            normalized = copy.deepcopy(role_spec)
            normalized["docx_regroup"] = (
                role_spec["grouping"] == "structural-container"
            )
            for selector in normalized["selectors"]:
                if "source_pattern" in selector:
                    normalized.setdefault("source_pattern", selector["source_pattern"])
                if "target_pattern" in selector:
                    normalized.setdefault("target_pattern", selector["target_pattern"])
            roles.append(normalized)
            bounds = profile["qa"]["role_inventory"][role_spec["role"]]
            inventory[role_spec["role"]] = {
                "minimum": bounds["minimum"],
                "maximum": bounds["maximum"],
                "style": role_spec["style"],
                "grouping": role_spec["grouping"],
                "output": role_spec["output"],
            }
        auxiliary = copy.deepcopy(
            profile["semantics"]["auxiliary_dispositions"]
        )
        constraints = list(profile["qa"]["constraints"])

    return {
        "contract_version": 1,
        "source_schema_version": profile["schema_version"],
        "profile_id": profile["id"],
        "adapter": profile["input"]["adapter"],
        "roles": roles,
        "role_inventory": inventory,
        "auxiliary_dispositions": auxiliary,
        "constraints": constraints,
    }


def profile_contract(profile: dict[str, Any]) -> dict[str, Any]:
    """Return a unified V2.3 semantic view without mutating the raw Profile."""
    validate_profile(profile)
    return _profile_contract_validated(profile)


def role_inventory(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return declared role bounds and policies in deterministic Profile order."""
    return copy.deepcopy(profile_contract(profile)["role_inventory"])


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
    # Preserve the exact V1 return shape for existing callers and fixtures.
    if profile.get("schema_version") == 1:
        for group in profile["semantics"]["groups"]:
            fields = ["source_pattern"]
            if include_target:
                fields.append("target_pattern")
            for field in fields:
                match = re.search(group[field], text.strip(), re.I)
                if match:
                    result = dict(group)
                    result["matched_language"] = (
                        "target" if field == "target_pattern" else "source"
                    )
                    result["identifier"] = match.groupdict().get("identifier")
                    return result
        return None

    for role_spec in _profile_contract_validated(profile)["roles"]:
        for selector in role_spec["selectors"]:
            fields = ["source_pattern"]
            if include_target:
                fields.append("target_pattern")
            for field in fields:
                pattern = selector.get(field)
                if pattern is None:
                    continue
                match = re.search(pattern, text.strip(), re.I)
                if match:
                    result = copy.deepcopy(role_spec)
                    result["matched_language"] = (
                        "target" if field == "target_pattern" else "source"
                    )
                    result["identifier"] = match.groupdict().get("identifier")
                    result["matched_selector"] = copy.deepcopy(selector)
                    return result
    return None


def semantic_group(profile: dict[str, Any], role: str) -> dict[str, Any]:
    if profile.get("schema_version") == 1:
        for group in profile["semantics"]["groups"]:
            if group["role"] == role:
                return group
    else:
        for role_spec in _profile_contract_validated(profile)["roles"]:
            if role_spec["role"] == role:
                return role_spec
    raise ValueError(f"profile has no semantic role: {role}")
