#!/usr/bin/env python3
"""Focused V2.3 Profile/semantic-contract regression tests.

This file intentionally stays separate from the V2.2 self-test so the legacy test
count and its historical assertions remain unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Callable

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from common import json_loads_strict, write_json, write_jsonl
from profile import (
    PROFILE_DIR,
    canonical_profile_sha256,
    load_profile,
    profile_contract,
    role_inventory,
    semantic_match,
    validate_profile,
)
from semantic_registry import registered_constraint_ids, registered_style_ids


ASSIGNMENT_FILE_SHA256 = "58920601161479315f3673c2505f8d3b8e1915decf6c92f7931769b0b35b72e2"
ASSIGNMENT_CANONICAL_SHA256 = (
    "8ce2863ab72adc1ac11f415576060afbbdf39ab7d4f62fc7f25b88b31539c774"
)


def expect_value_error(
    profile: dict, contains: str, *, mutate: Callable[[dict], None]
) -> None:
    invalid = copy.deepcopy(profile)
    mutate(invalid)
    try:
        validate_profile(invalid)
    except ValueError as exc:
        if contains not in str(exc):
            raise AssertionError(
                f"expected error containing {contains!r}, got {str(exc)!r}"
            ) from exc
    else:
        raise AssertionError(f"invalid Profile unexpectedly passed: {contains}")


def main() -> None:
    results: list[str] = []

    assignment_path = PROFILE_DIR / "assignment-en-zh.json"
    assignment_bytes = assignment_path.read_bytes()
    repository_bytes = assignment_bytes.replace(b"\r\n", b"\n")
    assert b"\r" not in repository_bytes
    assert hashlib.sha256(repository_bytes).hexdigest() == ASSIGNMENT_FILE_SHA256
    assignment = load_profile("assignment-en-zh")
    assert canonical_profile_sha256(assignment) == ASSIGNMENT_CANONICAL_SHA256
    results.append("assignment Profile byte and canonical hashes are unchanged")

    assignment_before = copy.deepcopy(assignment)
    assignment_contract = profile_contract(assignment)
    assert assignment == assignment_before
    assert assignment_contract["source_schema_version"] == 1
    assert assignment_contract["adapter"] == "native-text-pdf"
    assert list(assignment_contract["role_inventory"]) == [
        "problem",
        "example",
        "tip",
    ]
    assert all(
        item["minimum"] == 0
        and item["maximum"] is None
        and item["output"] == "bilingual"
        for item in assignment_contract["role_inventory"].values()
    )
    problem_match = semantic_match(
        assignment, "Problem (profile_fixture): Test", include_target=True
    )
    assert problem_match == {
        **assignment["semantics"]["groups"][0],
        "matched_language": "source",
        "identifier": "profile_fixture",
    }
    assert semantic_match(assignment, "示例（fixture）：测试")["role"] == "example"
    assert semantic_match(assignment, "Low-Resource Tip: Test")["role"] == "tip"
    mutable_inventory = role_inventory(assignment)
    mutable_inventory["problem"]["minimum"] = 99
    assert role_inventory(assignment)["problem"]["minimum"] == 0
    results.append("schema V1 behavior is preserved through an immutable compatibility view")

    academic = load_profile("academic-paper-en-zh")
    lecture = load_profile("lecture-notes-en-zh")
    for value, expected_id in (
        (academic, "academic-paper-en-zh"),
        (lecture, "lecture-notes-en-zh"),
    ):
        contract = profile_contract(value)
        assert contract["source_schema_version"] == 2
        assert contract["profile_id"] == expected_id
        assert contract["adapter"] == "mineru-import"
        assert set(contract["role_inventory"]) == {
            item["role"] for item in contract["roles"]
        }
        assert all(
            policy["style"] in registered_style_ids()
            for policy in contract["role_inventory"].values()
        )
        assert set(contract["constraints"]) <= registered_constraint_ids()
    results.append("academic-paper and lecture-notes schema V2 Profiles validate")

    academic_inventory = role_inventory(academic)
    assert academic_inventory["title"]["minimum"] == 1
    assert academic_inventory["title"]["maximum"] == 1
    assert academic_inventory["abstract"]["grouping"] == "structural-container"
    assert academic_inventory["author-affiliation"]["output"] == "source-only"
    assert academic_inventory["figure"]["output"] == "visual-once"
    assert academic_inventory["table-footnote"]["minimum"] == 0
    assert academic_inventory["references"]["minimum"] == 1
    assert semantic_match(academic, "Abstract: Motivation")["role"] == "abstract"
    assert semantic_match(
        academic, "Ada Example — Department of Reproducible Learning"
    )["role"] == "author-affiliation"
    assert semantic_match(academic, "参考文献：")["role"] == "references"
    results.append("academic-paper roles encode executable inventory and disposition rules")

    lecture_contract = profile_contract(lecture)
    lecture_roles = {item["role"]: item for item in lecture_contract["roles"]}
    theorem_family = ("theorem", "lemma", "proposition", "corollary")
    assert {lecture_roles[role]["style"] for role in theorem_family} == {"theorem"}
    assert all(
        lecture_roles[role]["grouping"] == "structural-container"
        for role in theorem_family + ("definition", "proof")
    )
    assert semantic_match(lecture, "Theorem 2.1: Stability")["role"] == "theorem"
    chinese_proof = semantic_match(lecture, "证明：由定义可得。")
    assert chinese_proof["role"] == "proof"
    assert chinese_proof["matched_language"] == "target"
    assert role_inventory(lecture)["proof"]["minimum"] == 0
    results.append("lecture theorem-family roles share style without losing role identity")

    expect_value_error(
        academic,
        "unsupported profile schema_version",
        mutate=lambda value: value.__setitem__("schema_version", 3),
    )
    for invalid_schema_version in (2.0, True):
        expect_value_error(
            academic,
            "unsupported profile schema_version",
            mutate=lambda value, invalid=invalid_schema_version: value.__setitem__(
                "schema_version", invalid
            ),
        )

    academic_text = (PROFILE_DIR / "academic-paper-en-zh.json").read_text(
        encoding="utf-8"
    )
    duplicate_schema = academic_text.replace(
        '  "schema_version": 2,',
        '  "schema_version": 2,\n  "schema_version": 2,',
        1,
    )
    with tempfile.TemporaryDirectory(prefix="profile-json-test-") as temp_dir:
        duplicate_path = Path(temp_dir) / "duplicate-profile.json"
        duplicate_path.write_text(
            duplicate_schema, encoding="utf-8", newline="\n"
        )
        try:
            load_profile(duplicate_path)
        except ValueError as exc:
            assert "duplicate JSON object key: 'schema_version'" in str(exc), str(exc)
        else:
            raise AssertionError("duplicate Profile JSON key unexpectedly passed")
    results.append("Profile JSON rejects duplicate keys and non-integer schema versions")

    for field in ("minimum_global_fivegram_coverage", "warn_page_below"):
        for invalid_threshold in (
            False,
            0,
            -1,
            1.01,
            "0.95",
        ):
            expect_value_error(
                academic,
                f"profile.qa.{field} must be a finite number in (0, 1]",
                mutate=lambda value, name=field, invalid=invalid_threshold: value[
                    "qa"
                ].__setitem__(name, invalid),
            )
        for non_finite in (float("nan"), float("inf"), float("-inf")):
            expect_value_error(
                academic,
                "JSON numbers must be finite",
                mutate=lambda value, name=field, invalid=non_finite: value[
                    "qa"
                ].__setitem__(name, invalid),
            )

    expect_value_error(
        academic,
        "minimum_native_text_page_ratio must be a finite number in (0, 1]",
        mutate=lambda value: value["input"].__setitem__(
            "minimum_native_text_page_ratio", 10**400
        ),
    )

    strict_numeric_cases = {
        "NaN": "non-finite JSON number is not allowed",
        "Infinity": "non-finite JSON number is not allowed",
        "-Infinity": "non-finite JSON number is not allowed",
        "1e9999": "outside the finite float range",
        "-1e9999": "outside the finite float range",
    }
    for token, expected_error in strict_numeric_cases.items():
        try:
            json_loads_strict(f'{{"threshold": {token}}}')
        except ValueError as exc:
            assert expected_error in str(exc), str(exc)
        else:
            raise AssertionError(f"non-finite JSON token unexpectedly passed: {token}")

    with tempfile.TemporaryDirectory(prefix="profile-strict-json-") as temp_dir:
        temp_root = Path(temp_dir)
        for token in ("false", "0", "-1", "NaN", "Infinity", "-1e9999"):
            invalid_path = temp_root / f"threshold-{len(token)}-{ord(token[0])}.json"
            invalid_path.write_text(
                academic_text.replace(
                    '"minimum_global_fivegram_coverage": 0.95',
                    f'"minimum_global_fivegram_coverage": {token}',
                    1,
                ),
                encoding="utf-8",
                newline="\n",
            )
            try:
                load_profile(invalid_path)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"invalid Profile threshold unexpectedly passed: {token}"
                )

        for writer, destination in (
            (lambda path: write_json(path, {"value": float("nan")}), temp_root / "nan.json"),
            (
                lambda path: write_jsonl(path, [{"value": float("inf")}]),
                temp_root / "infinity.jsonl",
            ),
        ):
            try:
                writer(destination)
            except ValueError as exc:
                assert "Out of range float values" in str(exc), str(exc)
            else:
                raise AssertionError("non-finite JSON serialization unexpectedly passed")
    results.append("Profile thresholds and JSON numeric values fail closed")

    class CustomList(list):
        pass

    programmatic_cases: list[tuple[str, dict, str]] = []
    non_finite_profile = copy.deepcopy(academic)
    non_finite_profile["label"] = float("nan")
    programmatic_cases.append(
        ("non-finite scalar", non_finite_profile, "JSON numbers must be finite")
    )
    tuple_surrogate_profile = copy.deepcopy(academic)
    tuple_surrogate_profile["label"] = ("\ud800",)
    programmatic_cases.append(
        (
            "tuple containing surrogate",
            tuple_surrogate_profile,
            "JSON values must use only",
        )
    )
    custom_container_profile = copy.deepcopy(academic)
    custom_container_profile["label"] = CustomList(["custom"])
    programmatic_cases.append(
        ("custom container", custom_container_profile, "JSON values must use only")
    )
    non_string_key_profile = copy.deepcopy(academic)
    non_string_key_profile["metadata"] = {1: "not-a-JSON-object-key"}
    programmatic_cases.append(
        ("non-string object key", non_string_key_profile, "keys must be strings")
    )
    circular_profile = copy.deepcopy(academic)
    circular_profile["cycle"] = circular_profile
    programmatic_cases.append(
        ("circular reference", circular_profile, "circular references")
    )
    for label, invalid_profile, expected_error in programmatic_cases:
        for operation in (validate_profile, canonical_profile_sha256):
            try:
                operation(invalid_profile)
            except ValueError as exc:
                assert expected_error in str(exc), (label, operation.__name__, str(exc))
            else:
                raise AssertionError(
                    f"{label} unexpectedly passed {operation.__name__}"
                )
    results.append("programmatic Profiles must be acyclic native finite JSON values")

    surrogate_profile = academic_text.replace(
        '"header_label": "Academic paper study edition"',
        '"header_label": "\\ud800"',
        1,
    )
    valid_non_bmp_profile = academic_text.replace(
        '"header_label": "Academic paper study edition"',
        '"header_label": "Academic \\ud83d\\ude00"',
        1,
    )
    with tempfile.TemporaryDirectory(prefix="profile-unicode-json-") as temp_dir:
        temp_root = Path(temp_dir)
        surrogate_path = temp_root / "surrogate.json"
        surrogate_path.write_text(surrogate_profile, encoding="utf-8", newline="\n")
        try:
            load_profile(surrogate_path)
        except ValueError as exc:
            assert "unpaired surrogates" in str(exc), str(exc)
        else:
            raise AssertionError("unpaired Profile surrogate unexpectedly passed")

        non_bmp_path = temp_root / "non-bmp.json"
        non_bmp_path.write_text(valid_non_bmp_profile, encoding="utf-8", newline="\n")
        non_bmp_profile = load_profile(non_bmp_path)
        assert non_bmp_profile["render"]["docx"]["header_label"] == "Academic 😀"
        assert canonical_profile_sha256(non_bmp_profile)
    results.append("Profile JSON rejects unpaired surrogates and preserves non-BMP text")
    expect_value_error(
        academic,
        "unsupported input adapter",
        mutate=lambda value: value["input"].__setitem__("adapter", "unknown-adapter"),
    )
    expect_value_error(
        academic,
        "unsupported semantic style",
        mutate=lambda value: value["semantics"]["roles"][0].__setitem__(
            "style", "unknown-style"
        ),
    )
    expect_value_error(
        academic,
        "unsupported output disposition",
        mutate=lambda value: value["semantics"]["roles"][0].__setitem__(
            "output", "drop-silently"
        ),
    )
    expect_value_error(
        academic,
        "does not support structural containers",
        mutate=lambda value: value["semantics"]["roles"][5].__setitem__(
            "grouping", "structural-container"
        ),
    )
    expect_value_error(
        academic,
        "target_pattern requires source_pattern",
        mutate=lambda value: value["semantics"]["roles"][0].__setitem__(
            "selectors", [{"target_pattern": "^标题"}]
        ),
    )

    def duplicate_role(value: dict) -> None:
        value["semantics"]["roles"].append(
            copy.deepcopy(value["semantics"]["roles"][0])
        )

    expect_value_error(academic, "duplicate semantic role", mutate=duplicate_role)

    def duplicate_selector(value: dict) -> None:
        value["semantics"]["roles"][5]["selectors"][0] = copy.deepcopy(
            value["semantics"]["roles"][0]["selectors"][0]
        )

    expect_value_error(
        academic, "semantic selector is assigned more than once", mutate=duplicate_selector
    )
    expect_value_error(
        academic,
        "must exactly cover semantic roles",
        mutate=lambda value: value["qa"]["role_inventory"].pop("title"),
    )
    expect_value_error(
        academic,
        "maximum must be null or at least minimum",
        mutate=lambda value: value["qa"]["role_inventory"]["title"].__setitem__(
            "maximum", 0
        ),
    )
    expect_value_error(
        academic,
        "must explicitly cover all auxiliary roles",
        mutate=lambda value: value["semantics"]["auxiliary_dispositions"].pop(
            "page-number"
        ),
    )
    expect_value_error(
        academic,
        "unsupported semantic constraint",
        mutate=lambda value: value["qa"]["constraints"].append("unknown-constraint"),
    )
    expect_value_error(
        assignment,
        "unsupported input adapter",
        mutate=lambda value: value["input"].__setitem__("adapter", "unknown-adapter"),
    )
    results.append("invalid adapters, styles, selectors, inventories, and constraints fail closed")

    print(
        json.dumps(
            {"status": "passed", "tests": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
