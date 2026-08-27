#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from audit_source import validate_source_audit_binding
from common import (
    ascii_tokens,
    json_loads_strict,
    ngrams,
    placeholder_counts,
    sha256_text,
)
from profile import canonical_profile_sha256, load_work_profile, target_text_pattern
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
)
from translation_utils import expected_placeholder_counts, restore_placeholders
from translation_utils import glossary_term_present, validate_glossary


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


def _atomic_write_jsonl(
    path: Path, values: list[dict[str, Any]], work_dir: Path
) -> None:
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
    atomic_write_text(path, payload, boundary=work_dir)


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    return os.path.lexists(path)


def _preflight_flat_directory(
    directory: Path, work_dir: Path, *, missing_ok: bool = False
) -> list[Path]:
    """Return a stable flat-file inventory after rejecting every unsafe entry."""
    if (
        validate_artifact_tree(
            directory, boundary=work_dir, allow_missing=missing_ok
        )
        is None
    ):
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


def _batch_artifact_path(
    translation_dir: Path,
    value: object,
    *,
    label: str,
    directory: str,
) -> Path:
    """Accept only the canonical translation-relative batch path form."""
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a canonical translation-relative path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value != posix.as_posix()
        or len(posix.parts) != 2
        or posix.parts[0] != directory
        or not re.fullmatch(r"part-[0-9]{4}\.jsonl", posix.parts[1])
    ):
        raise ValueError(f"{label} must be {directory}/part-NNNN.jsonl")
    return translation_dir.joinpath(*posix.parts)


def validate_translation_plan_binding(
    work_dir: Path,
    plan_path: Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate every current input and request bound by a translation plan."""
    work_dir = lexical_absolute_path(work_dir)
    translation_dir = work_dir / "translation"
    plan_path = plan_path or translation_dir / "plan.json"
    errors: list[str] = []
    try:
        validate_artifact_directory(work_dir)
        validate_artifact_file(plan_path, boundary=work_dir, allow_missing=True)
    except ArtifactSafetyError as exc:
        return None, [f"translation plan path is unsafe: {exc}"]
    if not os.path.lexists(plan_path):
        return None, ["translation plan is missing"]
    try:
        plan = _read_json(plan_path, work_dir)
    except (ArtifactSafetyError, OSError, ValueError) as exc:
        return None, [f"translation plan cannot be read: {exc}"]
    if not isinstance(plan, dict):
        return None, ["translation plan must be a JSON object"]
    if plan.get("schema_version") != 2:
        errors.append("translation plan schema_version is invalid")

    try:
        profile = load_work_profile(work_dir)
        if plan.get("profile_id") != profile["id"]:
            errors.append("translation plan Profile id binding is missing or stale")
        if plan.get("profile_sha256") != canonical_profile_sha256(profile):
            errors.append("translation plan canonical Profile binding is missing or stale")
        if plan.get("target_language") != profile["translation"]["target_language"]:
            errors.append("translation plan target language binding is missing or stale")
    except (ArtifactSafetyError, KeyError, OSError, ValueError) as exc:
        errors.append(f"translation plan Profile binding cannot be validated: {exc}")

    bound_files = {
        "profile_file_sha256": work_dir / "profile.json",
        "document_ir_sha256": work_dir / "document-ir.json",
        "source_manifest_sha256": work_dir / "manifest.json",
        "source_blocks_sha256": work_dir / "blocks.jsonl",
        "source_audit_sha256": work_dir / "source-audit.json",
        "glossary_sha256": translation_dir / "glossary.json",
    }
    for field, path in bound_files.items():
        try:
            current_hash = (
                sha256_artifact(path, boundary=work_dir)
                if _artifact_exists(path, work_dir)
                else None
            )
        except (ArtifactSafetyError, OSError) as exc:
            errors.append(f"translation plan {field} artifact is unsafe: {exc}")
            continue
        if current_hash is None or plan.get(field) != current_hash:
            errors.append(f"translation plan {field} binding is missing or stale")

    try:
        manifest = _read_json(work_dir / "manifest.json", work_dir)
        if not isinstance(manifest, dict):
            raise ValueError("source manifest must be a JSON object")
        if plan.get("source_pdf_sha256") != manifest.get("source_sha256"):
            errors.append("translation plan source PDF binding is missing or stale")
    except (ArtifactSafetyError, OSError, ValueError) as exc:
        errors.append(f"translation plan source manifest cannot be validated: {exc}")

    batches = plan.get("batches")
    if not isinstance(batches, list):
        errors.append("translation plan batches must be an array")
        batches = []
    if plan.get("batch_count") != len(batches):
        errors.append("translation plan batch_count is missing or stale")

    declared_requests: list[Path] = []
    request_ids: list[str] = []
    segment_total = 0
    seen_requests: set[Path] = set()
    seen_responses: set[Path] = set()
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            errors.append(f"translation plan batch {index} must be an object")
            continue
        try:
            request_path = _batch_artifact_path(
                translation_dir,
                batch.get("request_file"),
                label=f"batches[{index}].request_file",
                directory="requests",
            )
            response_path = _batch_artifact_path(
                translation_dir,
                batch.get("response_file"),
                label=f"batches[{index}].response_file",
                directory="responses",
            )
            if request_path.name != response_path.name:
                raise ValueError(
                    f"batches[{index}] request_file and response_file must share a basename"
                )
            if request_path in seen_requests or response_path in seen_responses:
                raise ValueError(f"translation plan batch {index} repeats an artifact path")
            seen_requests.add(request_path)
            seen_responses.add(response_path)
            declared_requests.append(request_path)
            if batch.get("part") != index + 1:
                errors.append(f"translation plan batch {index} has a stale part number")
            if not _artifact_exists(request_path, work_dir):
                errors.append(f"translation request is missing: {batch.get('request_file')}")
                continue
            if batch.get("request_sha256") != sha256_artifact(
                request_path, boundary=work_dir
            ):
                errors.append(f"translation request binding is stale: {batch.get('request_file')}")
            entries = _read_jsonl(request_path, work_dir)
        except (ArtifactSafetyError, OSError, ValueError) as exc:
            errors.append(f"translation plan batch {index} is invalid: {exc}")
            continue
        ids = [entry.get("id") for entry in entries]
        if any(not isinstance(item, str) or not item for item in ids):
            errors.append(f"translation request IDs are invalid: {batch.get('request_file')}")
            continue
        if (
            not ids
            or batch.get("segment_count") != len(ids)
            or batch.get("first_id") != ids[0]
            or batch.get("last_id") != ids[-1]
        ):
            errors.append(f"translation request metadata is stale: {batch.get('request_file')}")
        request_ids.extend(ids)
        segment_total += len(ids)

    try:
        request_inventory = _preflight_flat_directory(
            translation_dir / "requests", work_dir, missing_ok=True
        )
        if request_inventory != sorted(declared_requests, key=lambda path: path.name):
            errors.append("translation request file set is missing or stale")
    except (ArtifactSafetyError, OSError) as exc:
        errors.append(f"translation requests are unsafe: {exc}")
    if plan.get("expected_segment_count") != segment_total:
        errors.append("translation plan expected_segment_count is missing or stale")
    if plan.get("expected_ids") != request_ids or len(set(request_ids)) != len(request_ids):
        errors.append("translation plan expected_ids binding is missing or stale")
    _, source_binding_errors = validate_source_audit_binding(
        work_dir, work_dir / "source-audit.json"
    )
    errors.extend(
        f"source audit freeze chain: {message}" for message in source_binding_errors
    )
    return plan, errors


def validate_translation_audit_binding(
    work_dir: Path,
    audit_path: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate a passed translation audit against plan, merged, and responses."""
    work_dir = lexical_absolute_path(work_dir)
    translation_dir = work_dir / "translation"
    plan_path = translation_dir / "plan.json"
    merged_path = translation_dir / "translations-merged.jsonl"
    audit_path = audit_path or translation_dir / "translation-audit.json"
    report = _read_json(audit_path, work_dir)
    if not isinstance(report, dict):
        raise ValueError("translation audit must be a JSON object")

    errors: list[str] = []
    if not isinstance(report.get("status"), str) or report.get("status") != "passed":
        errors.append("translation audit status is not passed")
    if report.get("failures") != []:
        errors.append("passed translation audit failures must be an empty array")

    empty_list_fields = (
        "missing_ids",
        "extra_ids",
        "duplicate_ids",
        "invalid_source_hash_ids",
        "empty_translation_ids",
        "untranslated_ids",
        "source_copy_ids",
    )
    for field in empty_list_fields:
        if report.get(field) != []:
            errors.append(f"passed translation audit {field} must be an empty array")
    for field in ("placeholder_failures", "glossary_failures"):
        if report.get(field) != {}:
            errors.append(f"passed translation audit {field} must be an empty object")
    if report.get("merged_output") != "translation/translations-merged.jsonl":
        errors.append("passed translation audit merged_output is missing or invalid")

    plan, plan_binding_errors = validate_translation_plan_binding(
        work_dir, plan_path
    )
    errors.extend(
        f"translation plan freeze chain: {message}"
        for message in plan_binding_errors
    )
    expected_count = (
        plan.get("expected_segment_count") if isinstance(plan, dict) else None
    )
    counts = {
        field: report.get(field)
        for field in ("expected_segments", "response_segments", "validated_segments")
    }
    if (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or expected_count < 0
    ):
        errors.append("translation plan expected segment count is invalid")
    for field, value in counts.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"passed translation audit {field} is invalid")
    if any(value != expected_count for value in counts.values()):
        errors.append("passed translation audit segment counts are inconsistent")
    for label, path, field in (
        ("translation plan", plan_path, "plan_sha256"),
        ("merged translations", merged_path, "merged_sha256"),
    ):
        try:
            current_hash = (
                sha256_artifact(path, boundary=work_dir)
                if _artifact_exists(path, work_dir)
                else None
            )
        except (ArtifactSafetyError, OSError) as exc:
            errors.append(f"{label} is unsafe: {exc}")
            continue
        if current_hash is None or report.get(field) != current_hash:
            errors.append(f"{label} binding is missing or stale")

    expected_bindings = report.get("response_bindings")
    if not isinstance(expected_bindings, list):
        errors.append("translation response bindings are missing or invalid")
        expected_bindings = []
    parsed_bindings: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    for index, item in enumerate(expected_bindings):
        if not isinstance(item, dict):
            errors.append(f"translation response binding {index} is invalid")
            continue
        try:
            path = _batch_artifact_path(
                translation_dir,
                item.get("path"),
                label=f"response_bindings[{index}].path",
                directory="responses",
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        expected_hash = item.get("sha256")
        if path in seen_paths or not isinstance(expected_hash, str):
            errors.append(f"translation response binding {index} is invalid")
            continue
        seen_paths.add(path)
        parsed_bindings.append({"path": item["path"], "sha256": expected_hash})

    try:
        response_inventory = _preflight_flat_directory(
            translation_dir / "responses", work_dir, missing_ok=True
        )
        current_bindings = [
            {
                "path": f"responses/{path.name}",
                "sha256": sha256_artifact(path, boundary=work_dir),
            }
            for path in response_inventory
        ]
    except (ArtifactSafetyError, OSError) as exc:
        errors.append(f"translation responses are unsafe: {exc}")
        current_bindings = []
    if parsed_bindings != current_bindings:
        errors.append("translation response file set or hashes are stale")
    response_files = report.get("response_files")
    expected_response_files = [Path(item["path"]).name for item in current_bindings]
    if (
        not isinstance(response_files, list)
        or any(not isinstance(value, str) or not value for value in response_files)
        or len(response_files) != len(set(response_files))
        or response_files != expected_response_files
    ):
        errors.append("translation audit response file inventory is missing or stale")
    return report, errors


def english_word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z]{2,}\b", text))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reject missing, duplicate, stale, untranslated, or "
            "placeholder-damaging translations."
        )
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument(
        "--progress",
        action="store_true",
        help="write an incomplete progress report without returning a nonzero exit code",
    )
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    translation_dir = work_dir / "translation"
    validate_artifact_directory(translation_dir, boundary=work_dir)
    plan_path = translation_dir / "plan.json"
    merged_path = translation_dir / "translations-merged.jsonl"
    report_path = translation_dir / "translation-audit.json"
    _artifact_exists(merged_path, work_dir)
    _artifact_exists(report_path, work_dir)
    # A previous pass must stop being authoritative before any fallible plan
    # read or validation. Both targets are proven safe before either is removed.
    remove_artifact_file(merged_path, boundary=work_dir, missing_ok=True)
    remove_artifact_file(report_path, boundary=work_dir, missing_ok=True)

    _, source_binding_errors = validate_source_audit_binding(
        work_dir, work_dir / "source-audit.json"
    )
    if source_binding_errors:
        raise SystemExit(
            "source audit bindings are stale; translation audit is blocked: "
            + "; ".join(source_binding_errors)
        )

    plan = _read_json(plan_path, work_dir)
    if not isinstance(plan, dict):
        raise SystemExit("translation plan must be a JSON object")

    failures: list[str] = []
    warnings: list[str] = []
    source_hash_checks = {
        "manifest.json": plan.get("source_manifest_sha256"),
        "blocks.jsonl": plan.get("source_blocks_sha256"),
        "source-audit.json": plan.get("source_audit_sha256"),
        "translation/glossary.json": plan.get("glossary_sha256"),
    }
    if plan.get("profile_file_sha256"):
        source_hash_checks["profile.json"] = plan["profile_file_sha256"]
    if plan.get("document_ir_sha256"):
        source_hash_checks["document-ir.json"] = plan["document_ir_sha256"]
    if plan.get("schema_version") != 2:
        failures.append("unsupported translation plan schema; rerun prepare_translation.py")
    for filename, expected_hash in source_hash_checks.items():
        path = work_dir / filename
        try:
            actual_hash = (
                sha256_artifact(path, boundary=work_dir)
                if _artifact_exists(path, work_dir)
                else None
            )
        except ArtifactSafetyError as exc:
            failures.append(f"unsafe source artifact {filename}: {exc}")
            continue
        if actual_hash != expected_hash:
            failures.append(f"source artifact changed after planning: {filename}")

    try:
        profile = load_work_profile(work_dir)
        target_pattern = target_text_pattern(profile)
        if plan.get("profile_id") not in (None, profile["id"]):
            failures.append("translation plan profile does not match the work directory")
        if plan.get("profile_sha256") != canonical_profile_sha256(profile):
            failures.append("translation plan canonical profile hash does not match")
        if plan.get("target_language") != profile["translation"]["target_language"]:
            failures.append("translation plan target language does not match the profile")
    except (ArtifactSafetyError, ValueError) as exc:
        profile = None
        target_pattern = re.compile(r"[\u3400-\u9fff]")
        failures.append(f"invalid profile binding: {exc}")

    glossary_path = work_dir / "translation" / "glossary.json"
    try:
        if not _artifact_exists(glossary_path, work_dir):
            raise FileNotFoundError(glossary_path)
        glossary_terms = validate_glossary(_read_json(glossary_path, work_dir))
    except (ArtifactSafetyError, ValueError, FileNotFoundError) as exc:
        glossary_terms = []
        failures.append(f"invalid glossary: {exc}")

    request_by_id: dict[str, dict[str, Any]] = {}
    request_order: list[str] = []
    request_file_failures: list[str] = []
    parsed_batches: list[tuple[dict[str, Any], Path, Path]] = []
    declared_response_paths: list[Path] = []
    declared_request_set: set[Path] = set()
    declared_response_set: set[Path] = set()
    batches = plan.get("batches", [])
    if not isinstance(batches, list):
        failures.append("translation plan batches must be an array")
        batches = []
    for index, batch in enumerate(batches):
        if not isinstance(batch, dict):
            failures.append(f"translation plan batch {index} must be an object")
            continue
        try:
            request_path = _batch_artifact_path(
                translation_dir,
                batch.get("request_file"),
                label=f"batches[{index}].request_file",
                directory="requests",
            )
            response_path = _batch_artifact_path(
                translation_dir,
                batch.get("response_file"),
                label=f"batches[{index}].response_file",
                directory="responses",
            )
            if request_path.name != response_path.name:
                raise ValueError(
                    f"batches[{index}] request_file and response_file "
                    "must share a basename"
                )
        except ValueError as exc:
            failures.append(str(exc))
            continue
        duplicate_metadata = False
        if request_path in declared_request_set:
            failures.append(
                f"duplicate request_file in translation plan: {batch.get('request_file')}"
            )
            duplicate_metadata = True
        if response_path in declared_response_set:
            failures.append(
                f"duplicate response_file in translation plan: {batch.get('response_file')}"
            )
            duplicate_metadata = True
        if duplicate_metadata:
            continue
        declared_request_set.add(request_path)
        declared_response_set.add(response_path)
        declared_response_paths.append(response_path)
        parsed_batches.append((batch, request_path, response_path))

    try:
        request_inventory = _preflight_flat_directory(
            translation_dir / "requests", work_dir, missing_ok=True
        )
    except ArtifactSafetyError as exc:
        failures.append(f"translation requests directory is unsafe: {exc}")
        request_inventory = []
    try:
        response_inventory = _preflight_flat_directory(
            translation_dir / "responses", work_dir, missing_ok=True
        )
    except ArtifactSafetyError as exc:
        failures.append(f"translation responses directory is unsafe: {exc}")
        response_inventory = []

    actual_request_jsonl = {
        path for path in request_inventory if path.suffix == ".jsonl"
    }
    actual_response_jsonl = {
        path for path in response_inventory if path.suffix == ".jsonl"
    }
    unexpected_request_files = sorted(
        path.name for path in actual_request_jsonl - declared_request_set
    )
    unexpected_response_files = sorted(
        path.name for path in set(response_inventory) - declared_response_set
    )
    if unexpected_request_files:
        failures.append(
            f"undeclared translation request files: {unexpected_request_files}"
        )
    if unexpected_response_files:
        failures.append(
            f"undeclared translation response files: {unexpected_response_files}"
        )

    for batch, request_path, _response_path in parsed_batches:
        if request_path not in actual_request_jsonl:
            request_file_failures.append(f"missing {batch.get('request_file')}")
            continue
        try:
            request_hash = sha256_artifact(request_path, boundary=work_dir)
            entries = _read_jsonl(request_path, work_dir)
        except (ArtifactSafetyError, ValueError) as exc:
            request_file_failures.append(str(exc))
            continue
        if request_hash != batch.get("request_sha256"):
            request_file_failures.append(f"changed {batch.get('request_file')}")
        for request in entries:
            request_id = request.get("id")
            if not isinstance(request_id, str) or not request_id:
                request_file_failures.append(
                    f"invalid request id in {batch.get('request_file')}: {request_id!r}"
                )
                continue
            if request_id in request_by_id:
                request_file_failures.append(f"duplicate request id {request_id}")
                continue
            request_by_id[request_id] = request
            request_order.append(request_id)
    if request_file_failures:
        failures.append(f"translation requests are invalid: {request_file_failures}")
    if request_order != plan.get("expected_ids", []):
        failures.append("request ID order/set does not match the translation plan")

    response_entries: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    response_files = [
        path
        for path in declared_response_paths
        if path in actual_response_jsonl
    ]
    for response_path in response_files:
        try:
            entries = _read_jsonl(response_path, work_dir)
        except (ArtifactSafetyError, ValueError) as exc:
            failures.append(str(exc))
            continue
        for entry in entries:
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id:
                failures.append(f"invalid response id in {response_path.name}: {entry_id!r}")
                continue
            if entry_id in response_entries:
                duplicate_ids.append(entry_id)
            else:
                response_entries[entry_id] = entry

    raw_expected_ids = plan.get("expected_ids", [])
    if (
        not isinstance(raw_expected_ids, list)
        or any(not isinstance(item, str) or not item for item in raw_expected_ids)
        or len(set(raw_expected_ids)) != len(raw_expected_ids)
    ):
        failures.append("translation plan expected_ids must be unique nonempty strings")
        expected_ids: list[str] = []
    else:
        expected_ids = raw_expected_ids
    expected_set = set(expected_ids)
    response_set = set(response_entries)
    missing_ids = [item for item in expected_ids if item not in response_set]
    extra_ids = sorted(response_set - expected_set)
    if duplicate_ids:
        failures.append(f"duplicate response IDs: {sorted(set(duplicate_ids))}")
    if extra_ids:
        failures.append(f"unexpected response IDs: {extra_ids}")

    invalid_hash_ids: list[str] = []
    empty_ids: list[str] = []
    placeholder_failures: dict[str, dict[str, Any]] = {}
    untranslated_ids: list[str] = []
    source_copy_ids: list[str] = []
    glossary_failures: dict[str, list[str]] = {}
    restored_entries: list[dict[str, Any]] = []
    for request_id in expected_ids:
        if request_id not in response_entries or request_id not in request_by_id:
            continue
        request = request_by_id[request_id]
        response = response_entries[request_id]
        if response.get("source_sha256") != request["source_sha256"]:
            invalid_hash_ids.append(request_id)
        translation = response.get("translation")
        if not isinstance(translation, str) or not translation.strip():
            empty_ids.append(request_id)
            continue
        expected_counts = expected_placeholder_counts(request["protected_tokens"])
        actual_counts = placeholder_counts(translation)
        if actual_counts != expected_counts:
            placeholder_failures[request_id] = {
                "expected": dict(expected_counts),
                "actual": dict(actual_counts),
            }
            continue
        restored = restore_placeholders(translation.strip(), request["protected_tokens"])
        source_for_language_check = re.sub(
            r"⟦K\d{3}⟧", "", request["source_for_translation"]
        )
        if (
            english_word_count(source_for_language_check) >= 4
            and not target_pattern.search(restored)
        ):
            untranslated_ids.append(request_id)
        source_grams = ngrams(ascii_tokens(source_for_language_check), 5)
        translation_grams = set(ngrams(ascii_tokens(restored), 5))
        source_copy_ratio = (
            sum(gram in translation_grams for gram in source_grams) / len(source_grams)
            if source_grams
            else 0.0
        )
        if len(source_grams) >= 8 and source_copy_ratio >= 0.80:
            source_copy_ids.append(request_id)
        missing_terms = []
        for term in glossary_terms:
            if (
                term.get("enforce")
                and glossary_term_present(request["source"], term)
                and not any(target in restored for target in term["targets"])
            ):
                missing_terms.append(term["source"])
        if missing_terms:
            glossary_failures[request_id] = missing_terms
        restored_entries.append(
            {
                "id": request_id,
                "page": request["page"],
                "kind": request["kind"],
                "source_sha256": request["source_sha256"],
                "translation_with_placeholders": translation.strip(),
                "translation": restored,
                "translation_sha256": sha256_text(restored),
            }
        )

    if invalid_hash_ids:
        failures.append(f"stale or wrong source hashes: {invalid_hash_ids}")
    if empty_ids:
        failures.append(f"empty translations: {empty_ids}")
    if placeholder_failures:
        failures.append(
            f"placeholder multiset changed for IDs: {sorted(placeholder_failures)}"
        )
    if untranslated_ids:
        failures.append(
            f"translations contain no Chinese despite English prose: {untranslated_ids}"
        )
    if source_copy_ids:
        failures.append(
            f"translations substantially copy unchanged English prose: {source_copy_ids}"
        )
    if glossary_failures:
        failures.append(
            f"enforced glossary targets missing for IDs: {sorted(glossary_failures)}"
        )

    incomplete = bool(missing_ids)
    hard_failures = bool(failures)
    if not hard_failures and not incomplete:
        _atomic_write_jsonl(merged_path, restored_entries, work_dir)
        status = "passed"
    elif hard_failures:
        remove_artifact_file(merged_path, boundary=work_dir, missing_ok=True)
        status = "failed"
    else:
        remove_artifact_file(merged_path, boundary=work_dir, missing_ok=True)
        status = "incomplete"

    report = {
        "status": status,
        "expected_segments": len(expected_ids),
        "response_segments": len(response_entries),
        "validated_segments": len(restored_entries),
        "response_files": [path.name for path in response_files],
        "missing_ids": missing_ids,
        "extra_ids": extra_ids,
        "duplicate_ids": sorted(set(duplicate_ids)),
        "invalid_source_hash_ids": invalid_hash_ids,
        "empty_translation_ids": empty_ids,
        "untranslated_ids": untranslated_ids,
        "source_copy_ids": source_copy_ids,
        "placeholder_failures": placeholder_failures,
        "glossary_failures": glossary_failures,
        "merged_output": (
            "translation/translations-merged.jsonl" if status == "passed" else None
        ),
        "warnings": warnings,
        "failures": failures,
    }
    if status == "passed":
        report.update(
            {
                "plan_sha256": sha256_artifact(plan_path, boundary=work_dir),
                "merged_sha256": sha256_artifact(merged_path, boundary=work_dir),
                "response_bindings": [
                    {
                        "path": f"responses/{path.name}",
                        "sha256": sha256_artifact(path, boundary=work_dir),
                    }
                    for path in response_inventory
                ],
            }
        )
    _atomic_write_json(report_path, report, work_dir)
    console_report = dict(report)
    if len(missing_ids) > 30:
        console_report["missing_ids"] = missing_ids[:20] + [
            f"... {len(missing_ids) - 20} more; see translation-audit.json"
        ]
    print(json.dumps(console_report, ensure_ascii=False, indent=2))
    if status != "passed" and not (args.progress and status == "incomplete"):
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (ArtifactSafetyError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
