#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from common import (
    ascii_tokens,
    contains_cjk,
    ngrams,
    placeholder_counts,
    read_json,
    read_jsonl,
    sha256_file,
    sha256_text,
    write_json,
    write_jsonl,
)
from translation_utils import expected_placeholder_counts, restore_placeholders
from translation_utils import glossary_term_present, validate_glossary


def english_word_count(text: str) -> int:
    return len(re.findall(r"\b[A-Za-z]{2,}\b", text))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reject missing, duplicate, stale, untranslated, or placeholder-damaging translations."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument(
        "--progress",
        action="store_true",
        help="write an incomplete progress report without returning a nonzero exit code",
    )
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    translation_dir = work_dir / "translation"
    plan_path = translation_dir / "plan.json"
    if not plan_path.is_file():
        raise SystemExit(f"missing translation plan: {plan_path}")
    plan = read_json(plan_path)

    source_hash_checks = {
        "manifest.json": plan["source_manifest_sha256"],
        "blocks.jsonl": plan["source_blocks_sha256"],
        "source-audit.json": plan["source_audit_sha256"],
        "translation/glossary.json": plan["glossary_sha256"],
    }
    failures: list[str] = []
    warnings: list[str] = []
    for filename, expected_hash in source_hash_checks.items():
        path = work_dir / filename
        if not path.is_file() or sha256_file(path) != expected_hash:
            failures.append(f"source artifact changed after planning: {filename}")

    try:
        glossary_terms = validate_glossary(
            read_json(work_dir / "translation" / "glossary.json")
        )
    except (ValueError, FileNotFoundError) as exc:
        glossary_terms = []
        failures.append(f"invalid glossary: {exc}")

    request_by_id: dict[str, dict[str, Any]] = {}
    request_order: list[str] = []
    request_file_failures = []
    for batch in plan.get("batches", []):
        request_path = translation_dir / batch["request_file"]
        if not request_path.is_file():
            request_file_failures.append(f"missing {batch['request_file']}")
            continue
        if sha256_file(request_path) != batch["request_sha256"]:
            request_file_failures.append(f"changed {batch['request_file']}")
        for request in read_jsonl(request_path):
            request_id = request.get("id")
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
    response_files = sorted((translation_dir / "responses").glob("*.jsonl"))
    for response_path in response_files:
        try:
            entries = read_jsonl(response_path)
        except ValueError as exc:
            failures.append(str(exc))
            continue
        for entry in entries:
            entry_id = entry.get("id")
            if entry_id in response_entries:
                duplicate_ids.append(str(entry_id))
            else:
                response_entries[str(entry_id)] = entry

    expected_ids = plan.get("expected_ids", [])
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
            and not contains_cjk(restored)
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

    merged_path = translation_dir / "translations-merged.jsonl"
    if merged_path.exists():
        merged_path.unlink()

    incomplete = bool(missing_ids)
    hard_failures = bool(failures)
    if not hard_failures and not incomplete:
        write_jsonl(merged_path, restored_entries)
        status = "passed"
    elif hard_failures:
        status = "failed"
    else:
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
    write_json(translation_dir / "translation-audit.json", report)
    console_report = dict(report)
    if len(missing_ids) > 30:
        console_report["missing_ids"] = missing_ids[:20] + [
            f"... {len(missing_ids) - 20} more; see translation-audit.json"
        ]
    print(json.dumps(console_report, ensure_ascii=False, indent=2))
    if status != "passed" and not (args.progress and status == "incomplete"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
