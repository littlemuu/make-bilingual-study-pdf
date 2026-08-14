#!/usr/bin/env python3
"""Focused V2.3 Profile/semantic-contract regression tests.

This file intentionally stays separate from the V2.2 self-test so the legacy test
count and its historical assertions remain unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Callable

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


ASSIGNMENT_FILE_SHA256 = "27b5baedd62f75c7a980ccc554f2d063fd529b55c92fed596cbe59d77eba10d0"
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
    assert hashlib.sha256(assignment_bytes).hexdigest() == ASSIGNMENT_FILE_SHA256
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
