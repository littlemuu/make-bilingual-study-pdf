#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from common import json_loads_strict, sha256_text, validate_json_value
from safe_artifacts import (
    ArtifactSafetyError,
    artifact_paths_same_entry,
    lexical_absolute_path,
    lexical_paths_overlap,
    read_artifact_text,
    validate_artifact_file,
)
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
FROZEN_WORK_ARTIFACTS = ("manifest.json", "document-ir.json", "source-audit.json")
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def _is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        or getattr(status, "st_reparse_tag", 0)
    )


def _reject_unsafe_work_status(
    status: os.stat_result, label: str, *, require_directory: bool = False
) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise ValueError(f"symbolic links are not allowed for Profile binding: {label}")
    if _is_reparse_point(status):
        raise ValueError(f"reparse points are not allowed for Profile binding: {label}")
    if require_directory:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"Profile work path must be a regular directory: {label}")
        return
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(
            f"Profile binding requires a regular file or absent target: {label}"
        )
    if status.st_nlink != 1:
        raise ValueError(
            f"multiply linked files are not allowed for Profile binding: {label}"
        )


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(
        right.st_mode
    )


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and left.st_nlink == right.st_nlink
    )


def _absolute_work_path(work_dir: Path) -> Path:
    return Path(os.path.abspath(work_dir.expanduser()))


def _work_directory_paths(work_dir: Path) -> tuple[Path, ...]:
    parts = work_dir.parts
    return tuple(Path(*parts[:index]) for index in range(1, len(parts) + 1))


def _inspect_work_directory(
    work_dir: Path,
) -> tuple[Path, tuple[tuple[Path, os.stat_result], ...]]:
    work_dir = _absolute_work_path(work_dir)
    identities: list[tuple[Path, os.stat_result]] = []
    try:
        for current in _work_directory_paths(work_dir):
            status = os.lstat(current)
            _reject_unsafe_work_status(status, str(current), require_directory=True)
            identities.append((current, status))
    except OSError as exc:
        raise ValueError(f"cannot inspect Profile work directory: {exc}") from exc
    return work_dir, tuple(identities)


def prepare_profile_work_directory(work_dir: Path) -> Path:
    """Create WORK without following a symbolic-link or reparse-point ancestor."""
    work_dir = _absolute_work_path(work_dir)
    identities: list[tuple[Path, os.stat_result]] = []
    for current in _work_directory_paths(work_dir):
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            _recheck_work_directory(tuple(identities))
            try:
                os.mkdir(current)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ValueError(
                    f"cannot create Profile work directory safely: {exc}"
                ) from exc
            try:
                status = os.lstat(current)
            except OSError as exc:
                raise ValueError(
                    f"cannot inspect created Profile work directory: {exc}"
                ) from exc
        except OSError as exc:
            raise ValueError(f"cannot inspect Profile work directory: {exc}") from exc
        _reject_unsafe_work_status(status, str(current), require_directory=True)
        identities.append((current, status))
        _recheck_work_directory(tuple(identities))
    return work_dir


def _recheck_work_directory(
    identities: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    for path, expected_status in identities:
        try:
            current_status = os.lstat(path)
            _reject_unsafe_work_status(
                current_status, str(path), require_directory=True
            )
        except OSError as exc:
            raise ValueError(f"Profile work directory changed: {exc}") from exc
        if not _same_directory_identity(expected_status, current_status):
            raise ValueError("Profile work directory identity changed during binding")


def _profile_target_status(path: Path) -> os.stat_result | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"cannot inspect Profile binding target: {exc}") from exc
    _reject_unsafe_work_status(status, str(path))
    return status


def validate_profile_binding_target(work_dir: Path) -> Path:
    """Validate WORK/profile.json without reading or following its directory entry."""
    work_dir, directory_identities = _inspect_work_directory(work_dir)
    _profile_target_status(work_dir / "profile.json")
    _recheck_work_directory(directory_identities)
    return work_dir


def _open_profile_descriptor(path: Path) -> int:
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(path),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def _read_bound_profile(
    path: Path, expected_status: os.stat_result
) -> dict[str, Any]:
    before = _profile_target_status(path)
    if before is None or not _same_file_metadata(expected_status, before):
        raise ValueError("Profile binding changed before it could be read")
    try:
        descriptor = _open_profile_descriptor(path)
    except OSError as exc:
        raise ValueError(f"cannot open bound Profile safely: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        after_open = _profile_target_status(path)
        if after_open is None:
            raise ValueError("Profile binding disappeared while opening")
        _reject_unsafe_work_status(opened, str(path))
        if not (
            _same_file_metadata(before, opened)
            and _same_file_metadata(opened, after_open)
        ):
            raise ValueError("Profile binding changed while opening")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        after_read = _profile_target_status(path)
        if after_read is None or not (
            _same_file_metadata(opened, finished)
            and _same_file_metadata(finished, after_read)
        ):
            raise ValueError("Profile binding changed while reading")
    finally:
        os.close(descriptor)
    try:
        payload = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("bound Profile is not valid UTF-8") from exc
    return validate_profile(json_loads_strict(payload))


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("failed to write bound Profile")
        remaining = remaining[written:]


def _atomic_replace_profile(
    work_dir: Path,
    path: Path,
    payload: bytes,
    expected_status: os.stat_result | None,
    directory_identities: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    descriptor = -1
    temp_path: Path | None = None
    temp_status: os.stat_result | None = None
    try:
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=".profile-", suffix=".tmp", dir=work_dir
        )
        temp_path = Path(raw_temp_path)
        temp_status = os.fstat(descriptor)
        _reject_unsafe_work_status(temp_status, str(temp_path))
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        finished = os.fstat(descriptor)
        if finished.st_size != len(payload) or not os.path.samestat(
            temp_status, finished
        ):
            raise ValueError("temporary Profile changed while writing")
        os.close(descriptor)
        descriptor = -1

        current_temp = _profile_target_status(temp_path)
        if current_temp is None or not _same_file_metadata(finished, current_temp):
            raise ValueError("temporary Profile changed before publication")
        _recheck_work_directory(directory_identities)
        current_target = _profile_target_status(path)
        if expected_status is None:
            if current_target is not None:
                raise ValueError("Profile binding target appeared during publication")
        elif current_target is None or not _same_file_metadata(
            expected_status, current_target
        ):
            raise ValueError("Profile binding changed before publication")

        os.replace(temp_path, path)
        published = _profile_target_status(path)
        if published is None or not os.path.samestat(finished, published):
            raise ValueError("published Profile identity does not match temporary file")
        _recheck_work_directory(directory_identities)
        temp_path = None
    except OSError as exc:
        raise ValueError(f"cannot publish bound Profile safely: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_path is not None and temp_status is not None:
            try:
                current_temp = os.lstat(temp_path)
                if os.path.samestat(temp_status, current_temp):
                    os.unlink(temp_path)
            except FileNotFoundError:
                pass


def canonical_profile_sha256(profile: dict[str, Any]) -> str:
    """Hash the raw Profile, never its derived compatibility contract."""
    validate_json_value(profile)
    payload = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
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


def validate_unit_interval_number(value: Any, field: str) -> float:
    """Return a non-boolean finite number in the supported unit interval."""
    if (
        type(value) not in {int, float}
        or (type(value) is float and not math.isfinite(value))
        or not 0 < value <= 1
    ):
        raise ValueError(f"{field} must be a finite number in (0, 1]")
    return float(value)


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
    validate_unit_interval_number(ratio, "minimum_native_text_page_ratio")
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

    qa = profile.get("qa")
    if not isinstance(qa, dict):
        raise ValueError("profile.qa must be an object")
    for field in ("minimum_global_fivegram_coverage", "warn_page_below"):
        validate_unit_interval_number(qa.get(field), f"profile.qa.{field}")


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
    validate_json_value(profile)
    schema_version = profile.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
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


def _profile_reference_path(
    reference: str | Path | None,
) -> tuple[Path, bool]:
    if reference is None:
        return lexical_absolute_path(PROFILE_DIR / f"{DEFAULT_PROFILE_ID}.json"), False

    name = str(reference)
    candidate = Path(reference).expanduser()
    explicit_path = (
        isinstance(reference, Path)
        or candidate.is_absolute()
        or "/" in name
        or "\\" in name
        or bool(candidate.suffix)
        or os.path.lexists(candidate)
    )
    if explicit_path:
        return lexical_absolute_path(candidate), True
    if not PROFILE_ID_RE.fullmatch(name):
        raise ValueError(f"profile does not exist: {reference}")
    return lexical_absolute_path(PROFILE_DIR / f"{name}.json"), False


def _profile_paths_same_entry(left: Path, right: Path) -> bool:
    try:
        return artifact_paths_same_entry(left, right)
    except ArtifactSafetyError as exc:
        raise ValueError(f"unsafe Profile path identity: {exc}") from exc


def _reject_noncanonical_work_profile_path(path: Path) -> None:
    """Do not let a Profile input double as a generated WORK artifact."""
    for ancestor in (path.parent, *path.parents):
        if not (
            os.path.lexists(ancestor / "manifest.json")
            and os.path.lexists(ancestor / "blocks.jsonl")
        ):
            continue
        canonical = lexical_absolute_path(ancestor / "profile.json")
        if not _profile_paths_same_entry(path, canonical):
            raise ValueError(
                "a Profile path inside WORK must be the canonical WORK/profile.json"
            )
        return


def _validate_work_profile_reference(
    work_dir: Path, reference: str | Path | None
) -> None:
    if reference is None:
        return
    path, explicit_path = _profile_reference_path(reference)
    if not explicit_path:
        return
    absolute_work = lexical_absolute_path(work_dir)
    if not lexical_paths_overlap(path, absolute_work):
        return
    canonical = absolute_work / "profile.json"
    if not _profile_paths_same_entry(path, canonical):
        raise ValueError(
            "a Profile override inside WORK must be the canonical WORK/profile.json"
        )


def load_profile(reference: str | Path | None = None) -> dict[str, Any]:
    path, explicit_path = _profile_reference_path(reference)
    if explicit_path:
        _reject_noncanonical_work_profile_path(path)
    try:
        validate_artifact_file(path, boundary=path.parent, allow_missing=True)
        if not os.path.lexists(path):
            raise ValueError(f"profile does not exist: {path}")
        payload = read_artifact_text(path, boundary=path.parent)
    except ArtifactSafetyError as exc:
        raise ValueError(f"unsafe Profile path: {exc}") from exc
    try:
        profile = json_loads_strict(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid Profile JSON: {exc}") from exc
    if not isinstance(profile, dict):
        raise ValueError("Profile must be a JSON object")
    return validate_profile(profile)


def _bind_validated_profile(
    work_dir: Path, requested: dict[str, Any], *, force: bool
) -> dict[str, Any]:
    work_dir, directory_identities = _inspect_work_directory(work_dir)
    path = work_dir / "profile.json"
    existing_status = _profile_target_status(path)
    if existing_status is not None and not force:
        existing = _read_bound_profile(path, existing_status)
        _recheck_work_directory(directory_identities)
        if canonical_profile_sha256(existing) == canonical_profile_sha256(requested):
            return existing
        raise ValueError(
            "work directory is bound to a different profile; use --force only when "
            "intentionally invalidating downstream artifacts"
        )
    payload = (
        json.dumps(requested, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    _atomic_replace_profile(
        work_dir,
        path,
        payload,
        existing_status,
        directory_identities,
    )
    return requested


def bind_profile(
    work_dir: Path, reference: str | Path | None = None, *, force: bool = False
) -> dict[str, Any]:
    work_dir = validate_profile_binding_target(work_dir)
    _validate_work_profile_reference(work_dir, reference)
    return _bind_validated_profile(work_dir, load_profile(reference), force=force)


def load_work_profile(
    work_dir: Path, reference: str | Path | None = None
) -> dict[str, Any]:
    work_dir, directory_identities = _inspect_work_directory(work_dir)
    _validate_work_profile_reference(work_dir, reference)
    bound_path = work_dir / "profile.json"
    bound_status = _profile_target_status(bound_path)
    if bound_status is not None:
        bound = _read_bound_profile(bound_path, bound_status)
        _recheck_work_directory(directory_identities)
        if reference is not None:
            requested = load_profile(reference)
            if canonical_profile_sha256(bound) != canonical_profile_sha256(requested):
                raise ValueError(
                    "profile override does not match the work directory binding"
                )
        return bound

    frozen_artifacts: list[str] = []
    for name in FROZEN_WORK_ARTIFACTS:
        try:
            os.lstat(work_dir / name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect frozen work artifacts: {exc}") from exc
        frozen_artifacts.append(name)
    _recheck_work_directory(directory_identities)
    if frozen_artifacts:
        raise ValueError(
            "frozen work directory is missing profile.json "
            f"(artifacts present: {', '.join(frozen_artifacts)})"
        )
    raise ValueError(
        "work directory is not bound to a Profile; bind one explicitly for a new task"
    )


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
