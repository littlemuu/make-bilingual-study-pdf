#!/usr/bin/env python3
"""Run pinned Skill validation and the repository interface metadata contract."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
SKILL_NAME = "make-bilingual-study-pdf"
OPENAI_YAML = Path("agents") / "openai.yaml"
INTERFACE_FIELDS = {
    "display_name",
    "short_description",
    "default_prompt",
    "icon_small",
    "icon_large",
    "brand_color",
}
REQUIRED_INTERFACE_FIELDS = {
    "display_name",
    "short_description",
    "default_prompt",
    "icon_small",
    "icon_large",
}
POLICY_FIELDS = {"products", "allow_implicit_invocation"}
DEPENDENCY_TOOL_FIELDS = {
    "type",
    "value",
    "description",
    "transport",
    "url",
}
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def is_skill_invocation_boundary(character: str) -> bool:
    """Return whether a character can safely delimit an invocation token."""
    if not character or character.isspace():
        return True
    category = unicodedata.category(character)
    return (
        character != "$"
        and category[0] not in {"C", "L", "M", "N"}
        and category not in {"Pc", "Pd"}
    )


def validate_skill_invocation(prompt: str) -> tuple[bool, str]:
    """Require one exact, standalone ASCII Skill invocation in a prompt."""
    expected = f"${SKILL_NAME}"
    dollar_positions = [
        index for index, character in enumerate(prompt) if character == "$"
    ]
    if len(dollar_positions) != 1:
        return False, (
            "openai.yaml interface.default_prompt must contain exactly one "
            f"Skill invocation token {expected!r}; found {len(dollar_positions)} "
            "dollar markers"
        )

    start = dollar_positions[0]
    end = start + len(expected)
    before = prompt[start - 1] if start else ""
    after = prompt[end] if end < len(prompt) else ""
    if (
        prompt[start:end] != expected
        or not is_skill_invocation_boundary(before)
        or not is_skill_invocation_boundary(after)
    ):
        return False, (
            "openai.yaml interface.default_prompt must contain exactly one "
            f"standalone Skill invocation token {expected!r}"
        )
    return True, ""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    seen: set[object] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        seen.add(key)
    return super(UniqueKeyLoader, loader).construct_mapping(node, deep=deep)


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def reject_yaml_surrogates(value: object, *, label: str) -> None:
    """Reject surrogate code points in every parsed YAML string."""
    active_container_ids: set[int] = set()

    def visit(item: object) -> None:
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise ValueError(
                    f"{label} strings must not contain surrogate code points"
                )
            return
        if not isinstance(item, (dict, list, tuple, set, frozenset)):
            return

        container_id = id(item)
        if container_id in active_container_ids:
            raise ValueError(f"{label} must not contain recursive YAML aliases")
        active_container_ids.add(container_id)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    visit(key)
                    visit(child)
            else:
                for child in item:
                    visit(child)
        finally:
            active_container_ids.remove(container_id)

    visit(value)


def check_duplicate_keys(skill_root: Path) -> tuple[bool, str]:
    skill_md = skill_root / "SKILL.md"
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"cannot read SKILL.md: {exc}"

    match = FRONTMATTER_RE.match(content)
    if not match:
        return False, "SKILL.md does not contain closed LF-delimited frontmatter"

    try:
        data = yaml.load(match.group(1), Loader=UniqueKeyLoader)
        reject_yaml_surrogates(data, label="SKILL.md frontmatter")
    except (yaml.YAMLError, ValueError) as exc:
        return False, f"invalid or ambiguous SKILL.md frontmatter: {exc}"
    return True, "SKILL.md frontmatter has unique keys"


def mapping_has_only(
    value: object, allowed: set[str], *, label: str
) -> tuple[bool, str]:
    if not isinstance(value, dict):
        return False, f"{label} must be a mapping"
    if any(not isinstance(key, str) for key in value):
        return False, f"{label} keys must be strings"
    unexpected = set(value) - allowed
    if unexpected:
        return False, f"{label} has unexpected keys: {sorted(unexpected)!r}"
    return True, ""


def validate_yaml_node_styles(node: yaml.Node) -> tuple[bool, str]:
    seen: set[int] = set()

    def visit(value: yaml.Node, path: str, *, is_key: bool = False) -> str | None:
        identity = id(value)
        if identity in seen:
            return f"{path} must not use YAML aliases"
        seen.add(identity)
        if isinstance(value, yaml.ScalarNode):
            if is_key:
                if value.tag != "tag:yaml.org,2002:str" or value.style is not None:
                    return f"{path} keys must be unquoted strings"
            elif (
                value.tag == "tag:yaml.org,2002:str"
                and value.style not in {"'", '"'}
            ):
                return f"{path} string values must be quoted"
            return None
        if isinstance(value, yaml.MappingNode):
            for key_node, child in value.value:
                key_path = f"{path}.<key>"
                failure = visit(key_node, key_path, is_key=True)
                if failure:
                    return failure
                child_name = (
                    key_node.value
                    if isinstance(key_node, yaml.ScalarNode)
                    else "<value>"
                )
                failure = visit(child, f"{path}.{child_name}")
                if failure:
                    return failure
            return None
        if isinstance(value, yaml.SequenceNode):
            for index, child in enumerate(value.value):
                failure = visit(child, f"{path}[{index}]")
                if failure:
                    return failure
            return None
        return f"{path} uses an unsupported YAML node"

    failure = visit(node, "agents/openai.yaml")
    return (False, failure) if failure else (True, "")


def is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        or getattr(status, "st_reparse_tag", 0)
    )


def validate_icon_path(skill_root: Path, value: str, label: str) -> tuple[bool, str]:
    if not value.startswith("./assets/") or "\\" in value:
        return False, f"{label} must be a safe path below ./assets/"
    relative_text = value[2:]
    parts = relative_text.split("/")
    if (
        not parts
        or parts[0] != "assets"
        or any(part in {"", ".", ".."} for part in parts)
        or any(
            part.endswith((".", " "))
            or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            for part in parts
        )
    ):
        return False, f"{label} must be a safe path below ./assets/"

    current = skill_root
    try:
        for index, part in enumerate(parts):
            current /= part
            status = current.lstat()
            if stat.S_ISLNK(status.st_mode) or is_reparse_point(status):
                return False, f"{label} must not traverse a link or reparse point"
            if index < len(parts) - 1:
                if not stat.S_ISDIR(status.st_mode):
                    return False, f"{label} parent must be a directory"
            elif not stat.S_ISREG(status.st_mode):
                return False, f"{label} target must be a regular file"
    except OSError as exc:
        return False, f"{label} target is unavailable: {exc}"
    return True, ""


def check_openai_yaml(skill_root: Path) -> tuple[bool, str]:
    path = skill_root / OPENAI_YAML
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return False, f"cannot read {OPENAI_YAML.as_posix()}: {exc}"
    try:
        data = yaml.load(content, Loader=UniqueKeyLoader)
        node = yaml.compose(content, Loader=yaml.SafeLoader)
        reject_yaml_surrogates(data, label=OPENAI_YAML.as_posix())
    except (yaml.YAMLError, ValueError) as exc:
        return False, f"invalid or ambiguous {OPENAI_YAML.as_posix()}: {exc}"
    if node is None:
        return False, f"{OPENAI_YAML.as_posix()} must not be empty"
    valid, message = validate_yaml_node_styles(node)
    if not valid:
        return valid, message

    valid, message = mapping_has_only(
        data, {"interface", "dependencies", "policy"}, label=OPENAI_YAML.as_posix()
    )
    if not valid:
        return valid, message
    assert isinstance(data, dict)

    valid, message = mapping_has_only(
        data.get("interface"), INTERFACE_FIELDS, label="openai.yaml interface"
    )
    if not valid:
        return valid, message
    interface = data["interface"]
    assert isinstance(interface, dict)
    missing = REQUIRED_INTERFACE_FIELDS - set(interface)
    if missing:
        return False, f"openai.yaml interface is missing fields: {sorted(missing)!r}"
    for field in REQUIRED_INTERFACE_FIELDS:
        value = interface[field]
        if not isinstance(value, str) or not value.strip():
            return False, f"openai.yaml interface.{field} must be a non-empty string"
    short_description = interface["short_description"]
    if not 25 <= len(short_description) <= 64:
        return False, "openai.yaml interface.short_description must be 25-64 characters"
    valid, message = validate_skill_invocation(interface["default_prompt"])
    if not valid:
        return valid, message
    for field in ("icon_small", "icon_large"):
        valid, message = validate_icon_path(
            skill_root, interface[field], f"openai.yaml interface.{field}"
        )
        if not valid:
            return valid, message
    if "brand_color" in interface:
        brand_color = interface["brand_color"]
        if not isinstance(brand_color, str) or not re.fullmatch(
            r"#[0-9A-Fa-f]{6}", brand_color
        ):
            return False, "openai.yaml interface.brand_color must be #RRGGBB"

    if "policy" in data:
        valid, message = mapping_has_only(
            data["policy"], POLICY_FIELDS, label="openai.yaml policy"
        )
        if not valid:
            return valid, message
        policy = data["policy"]
        assert isinstance(policy, dict)
        if "products" in policy:
            products = policy["products"]
            if (
                not isinstance(products, list)
                or not products
                or any(not isinstance(item, str) or not item.strip() for item in products)
                or len(products) != len(set(products))
            ):
                return False, "openai.yaml policy.products must be unique non-empty strings"
        if "allow_implicit_invocation" in policy and not isinstance(
            policy["allow_implicit_invocation"], bool
        ):
            return False, "openai.yaml policy.allow_implicit_invocation must be boolean"

    if "dependencies" in data:
        valid, message = mapping_has_only(
            data["dependencies"], {"tools"}, label="openai.yaml dependencies"
        )
        if not valid:
            return valid, message
        dependencies = data["dependencies"]
        assert isinstance(dependencies, dict)
        tools = dependencies.get("tools")
        if not isinstance(tools, list) or not tools:
            return False, "openai.yaml dependencies.tools must be a non-empty list"
        for index, tool in enumerate(tools):
            label = f"openai.yaml dependencies.tools[{index}]"
            valid, message = mapping_has_only(
                tool, DEPENDENCY_TOOL_FIELDS, label=label
            )
            if not valid:
                return valid, message
            assert isinstance(tool, dict)
            missing = DEPENDENCY_TOOL_FIELDS - set(tool)
            if missing:
                return False, f"{label} is missing fields: {sorted(missing)!r}"
            if any(
                not isinstance(tool[field], str) or not tool[field].strip()
                for field in DEPENDENCY_TOOL_FIELDS
            ):
                return False, f"{label} fields must be non-empty strings"
            if tool["type"] != "mcp":
                return False, f"{label}.type must be 'mcp'"

    return True, f"{OPENAI_YAML.as_posix()} satisfies the Skill interface contract"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_root", type=Path)
    parser.add_argument("--upstream-validator", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.upstream_validator.is_file():
        print(
            f"upstream Skill validator not found: {args.upstream_validator}",
            file=sys.stderr,
        )
        return 1

    upstream_environment = os.environ.copy()
    upstream_environment["PYTHONUTF8"] = "1"
    upstream = subprocess.run(
        [sys.executable, str(args.upstream_validator), str(args.skill_root)],
        check=False,
        env=upstream_environment,
    )
    if upstream.returncode != 0:
        return upstream.returncode

    valid, message = check_duplicate_keys(args.skill_root)
    print(message)
    if not valid:
        return 1
    valid, message = check_openai_yaml(args.skill_root)
    print(message)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
