#!/usr/bin/env python3
"""Freeze the historical assignment-en-zh V1 Profile and prove a candidate V2 is equivalent.

Work package A (Issue #18). These tests turn the issue's known historical anchors into
executable ground truth: the V1 Profile bytes/canonical hash, the full `profile_contract()`
result, `semantic_match()` behavior, and the grouping/style/output/disposition policies.
A candidate schema-V2 representation is then built from the documented mapping and its
contract is proven byte-equivalent to the frozen V1 contract.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import profile as profile_module  # noqa: E402
from semantic_registry import AUXILIARY_ROLES  # noqa: E402


FIXTURE_DIR = REPOSITORY / "tests" / "fixtures" / "profiles"
V1_FIXTURE = FIXTURE_DIR / "assignment-en-zh-v1.json"
V1_CONTRACT_FIXTURE = FIXTURE_DIR / "assignment-en-zh-v1-contract.json"
PRODUCTION_PROFILE = SCRIPTS.parent / "profiles" / "assignment-en-zh.json"

# Historical anchors from Issue #18. These must never be edited to match a drift.
FROZEN_LF_SHA256 = "58920601161479315f3673c2505f8d3b8e1915decf6c92f7931769b0b35b72e2"
FROZEN_CANONICAL_SHA256 = "8ce2863ab72adc1ac11f415576060afbbdf39ab7d4f62fc7f25b88b31539c774"

AUXILIARY_OMITTED = {role: "artifact-omitted" for role in sorted(AUXILIARY_ROLES)}

# A line-number independent, explicit source allowlist.  It is intentionally
# conservative: every remaining V1/legacy profile branch in normal runtime is
# named here, including the two generic schema-1 metadata readers that sit on
# the freeze chain.  Work package C must shrink this list; a newly added branch
# or a stale documented entry fails closed.
V1_RUNTIME_ALLOWLIST = Counter({
    ('audit_docx.py', 'function', '_freeze_v1_expected_counts'): 1,
    ('audit_docx.py', 'function', '_preflight_v1_paths'): 1,
    ('audit_docx.py', 'schema-branch', "ir.get('schema_version') != 2"): 1,
    ('audit_docx.py', 'schema-branch', "profile.get('schema_version') != 2"): 1,
    ('audit_docx.py', 'schema-branch', "profile.get('schema_version') == 2"): 2,
    ('audit_docx.py', 'symbol', 'V1_DOCX_AUDIT_CHECKS'): 2,
    ('audit_outputs.py', 'schema-branch', "profile.get('schema_version') == 2"): 1,
    ('audit_outputs.py', 'schema-branch', "semantic_contract['source_schema_version'] == 2"): 1,
    ('audit_source.py', 'schema-branch', "evidence.get('schema_version') != 1"): 1,
    ('audit_source.py', 'schema-branch', "profile.get('schema_version') == 2"): 1,
    ('audit_translation.py', 'schema-branch', "plan.get('schema_version') != 2"): 2,
    ('build_docx.py', 'function', '_preflight_v1_paths'): 1,
    ('build_docx.py', 'schema-branch', "ir.get('schema_version') != 2"): 1,
    ('build_docx.py', 'schema-branch', "profile.get('schema_version') == 1"): 1,
    ('build_docx.py', 'schema-branch', "profile['schema_version'] == 1"): 4,
    ('build_docx.py', 'schema-branch', "profile['schema_version'] == 2"): 6,
    ('build_docx.py', 'schema-branch', "requested_profile.get('schema_version') == 2"): 2,
    ('build_outputs.py', 'function', '_legacy_output_policy'): 1,
    ('build_outputs.py', 'schema-branch', "contract['source_schema_version'] == 1"): 2,
    ('build_outputs.py', 'schema-branch', "contract['source_schema_version'] == 2"): 2,
    ('build_outputs.py', 'schema-branch', "semantic_contract['source_schema_version'] == 2"): 1,
    ('compile_docx_pdf.py', 'schema-branch', "context['schema_version'] == 1"): 2,
    ('compile_docx_pdf.py', 'schema-branch', "context['schema_version'] == 2"): 3,
    ('compile_docx_pdf.py', 'schema-branch', "ir.get('schema_version') != 2"): 1,
    ('compile_docx_pdf.py', 'schema-branch', "profile.get('schema_version') != 2"): 1,
    ('document_ir.py', 'function', '_build_document_ir_v1'): 1,
    ('document_ir.py', 'schema-branch', "profile.get('schema_version') == 1"): 1,
    ('docx_ast.py', 'function', '_transform_v1'): 1,
    ('docx_ast.py', 'schema-branch', "active_profile.get('schema_version') == 1"): 1,
    ('job_state.py', 'derived-schema', "requires_docx = schema_v2 or any((key in compile_hint for key in ('docx', 'docx_audit_sha256', 'docx_audit_bindings')))"): 1,
    ('job_state.py', 'derived-schema', "schema_v2 = profile.get('schema_version') == 2 or ir.get('schema_version') == 2 or 'docx_audit_bindings' in compile_hint"): 1,
    ('job_state.py', 'schema-branch', "ir.get('schema_version') == 2"): 1,
    ('job_state.py', 'schema-branch', "profile.get('schema_version') == 2"): 1,
    ('pipeline.py', 'schema-branch', "profile.get('schema_version') == 2"): 3,
    ('prepare_translation.py', 'schema-branch', "contract['source_schema_version'] == 1"): 2,
    ('prepare_translation.py', 'schema-branch', "contract['source_schema_version'] == 2"): 3,
    ('profile.py', 'function', '_validate_v1'): 1,
    ('profile.py', 'schema-branch', "profile.get('schema_version') == 1"): 2,
    ('profile.py', 'schema-branch', "profile['schema_version'] == 1"): 1,
    ('profile.py', 'schema-branch', 'schema_version == 1'): 1,
    ('profile.py', 'schema-branch', 'schema_version == 2'): 2,
    ('profile.py', 'schema-branch', 'schema_version not in {1, 2}'): 1,
    ('profile.py', 'schema-branch', 'type(schema_version) is not int'): 1,
    ('release_check.py', 'profile-contracts', 'PROFILE_CONTRACTS'): 1,
    ('release_check.py', 'schema-branch', "manifest.get('schema_version') != 1"): 1,
    ('release_check.py', 'schema-branch', 'type(actual_schema_version) is not int'): 1,
    ('release_check.py', 'schema-branch', "type(manifest.get('schema_version')) is not int"): 1,
    ('translation_utils.py', 'schema-branch', "glossary.get('schema_version') != 1"): 1,
    ('visual_utils.py', 'derived-schema', "schema_v2 = 'docx_audit_bindings' in compile_report"): 1,
    ('visual_utils.py', 'derived-schema', "schema_v2 = schema_v2 or profile.get('schema_version') == 2"): 1,
    ('visual_utils.py', 'schema-branch', "profile.get('schema_version') == 2"): 1,
})

NATIVE_KIND_ROLE_SPECS = (
    ("heading", "heading", "section-heading", "bilingual"),
    ("list-item", "list", "body", "bilingual"),
    ("paragraph", "prose", "body", "bilingual"),
    ("caption", "caption", "caption", "bilingual"),
    ("math-with-text", "math_with_text", "equation", "bilingual"),
    ("code", "code", "code", "source-only"),
    ("math", "math", "equation", "visual-once"),
    ("image", "image", "visual", "visual-once"),
    ("artifact", "artifact", "body", "artifact-omitted"),
    ("caption-continuation", "caption_continuation", "caption", "bilingual"),
    ("visual-content", "visual_content", "visual", "visual-once"),
)


def collect_v1_runtime_markers(root: Path = SCRIPTS) -> Counter[tuple[str, str, str]]:
    """Collect syntax-level V1 branches without trusting line numbers."""
    markers: list[tuple[str, str, str]] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                "v1" in node.name.lower() or node.name == "_legacy_output_policy"
            ):
                markers.append((relative, "function", node.name))
            elif isinstance(node, ast.Name) and node.id.startswith("V1_"):
                markers.append((relative, "symbol", node.id))
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "PROFILE_CONTRACTS"
                for target in node.targets
            ):
                markers.append((relative, "profile-contracts", "PROFILE_CONTRACTS"))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "schema_v2",
                        "requires_docx",
                    }:
                        markers.append(
                            (relative, "derived-schema", f"{target.id} = {ast.unparse(node.value)}")
                        )
            elif isinstance(node, ast.Compare):
                expression = ast.unparse(node)
                schema_expression = (
                    "schema_version" in expression
                    or "source_schema_version" in expression
                )
                if schema_expression:
                    markers.append((relative, "schema-branch", expression))
    return Counter(markers)


def _lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _v1_group_to_v2_role(group: dict) -> dict:
    """Reference mapping from Issue #18's target table (not the production migrator)."""
    return {
        "role": group["role"],
        "selectors": [
            {
                "source_pattern": group["source_pattern"],
                "target_pattern": group["target_pattern"],
            }
        ],
        "style": group["style"],
        "grouping": "structural-container" if group["docx_regroup"] else "none",
        "output": "bilingual",
    }


def build_candidate_v2(v1: dict) -> dict:
    """Build the documented V2 candidate without changing visible V1 behavior.

    V1 has an implicit legacy fallback for ordinary translatable prose.  V2 has
    no implicit fallback, so its explicit ``paragraph`` node-type selector is
    required to preserve those blocks' bilingual disposition.  It is not a new
    assignment heading selector and is deliberately asserted as expected V2-only
    metadata below.
    """
    groups = v1["semantics"]["groups"]
    roles = [_v1_group_to_v2_role(group) for group in groups]
    roles.extend(
        {
            "role": role,
            "selectors": [{"node_types": [kind]}],
            "style": style,
            "grouping": "none",
            "output": output,
        }
        for role, kind, style, output in NATIVE_KIND_ROLE_SPECS
    )
    return {
        "schema_version": 2,
        "id": v1["id"],
        "label": v1["label"],
        "input": copy.deepcopy(v1["input"]),
        "translation": copy.deepcopy(v1["translation"]),
        "semantics": {
            "roles": roles,
            "auxiliary_dispositions": copy.deepcopy(AUXILIARY_OMITTED),
        },
        "render": copy.deepcopy(v1["render"]),
        "qa": {
            "role_inventory": {
                **{
                    group["role"]: {"minimum": 0, "maximum": None}
                    for group in groups
                },
                **{
                    role: {"minimum": 0, "maximum": None}
                    for role, _kind, _style, _output in NATIVE_KIND_ROLE_SPECS
                },
            },
            "constraints": [],
            "minimum_global_fivegram_coverage": v1["qa"][
                "minimum_global_fivegram_coverage"
            ],
            "warn_page_below": v1["qa"]["warn_page_below"],
            "require_external_uri_inventory": v1["qa"][
                "require_external_uri_inventory"
            ],
            "require_exact_target_font": v1["qa"]["require_exact_target_font"],
        },
    }


class V1FixtureFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = profile_module.validate_profile(
            json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        )
        cls.candidate_v2 = build_candidate_v2(cls.fixture)
        cls.v1_contract = profile_module.profile_contract(cls.fixture)
        cls.v2_contract = profile_module.profile_contract(cls.candidate_v2)

    def test_fixture_file_hash_is_frozen(self) -> None:
        self.assertEqual(_lf_sha256(V1_FIXTURE), FROZEN_LF_SHA256)

    def test_fixture_bytes_match_the_production_profile(self) -> None:
        self.assertEqual(
            V1_FIXTURE.read_bytes().replace(b"\r\n", b"\n"),
            PRODUCTION_PROFILE.read_bytes().replace(b"\r\n", b"\n"),
        )

    def test_fixture_identity(self) -> None:
        self.assertEqual(self.fixture["schema_version"], 1)
        self.assertEqual(self.fixture["id"], "assignment-en-zh")

    def test_fixture_canonical_hash_is_frozen(self) -> None:
        self.assertEqual(
            profile_module.canonical_profile_sha256(self.fixture),
            FROZEN_CANONICAL_SHA256,
        )

    def test_profile_contract_is_frozen(self) -> None:
        frozen = json.loads(V1_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(self.v1_contract, frozen)

    def test_contract_identity_and_adapter(self) -> None:
        self.assertEqual(self.v1_contract["contract_version"], 1)
        self.assertEqual(self.v1_contract["source_schema_version"], 1)
        self.assertEqual(self.v1_contract["profile_id"], "assignment-en-zh")
        self.assertEqual(self.v1_contract["adapter"], "native-text-pdf")
        self.assertEqual(
            [role["role"] for role in self.v1_contract["roles"]],
            ["problem", "example", "tip"],
        )

    def test_grouping_style_output_frozen(self) -> None:
        inventory = self.v1_contract["role_inventory"]
        self.assertEqual(
            inventory["problem"],
            {
                "minimum": 0,
                "maximum": None,
                "style": "problem",
                "grouping": "structural-container",
                "output": "bilingual",
            },
        )
        self.assertEqual(
            inventory["example"],
            {
                "minimum": 0,
                "maximum": None,
                "style": "example",
                "grouping": "none",
                "output": "bilingual",
            },
        )
        self.assertEqual(
            inventory["tip"],
            {
                "minimum": 0,
                "maximum": None,
                "style": "tip",
                "grouping": "none",
                "output": "bilingual",
            },
        )

    def test_auxiliary_dispositions_and_constraints_frozen(self) -> None:
        self.assertEqual(self.v1_contract["auxiliary_dispositions"], AUXILIARY_OMITTED)
        self.assertEqual(self.v1_contract["constraints"], [])


class SemanticMatchFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = profile_module.validate_profile(
            json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        )

    def _match(self, text: str, **kwargs):
        return profile_module.semantic_match(self.fixture, text, **kwargs)

    def test_source_positives(self) -> None:
        self.assertEqual(self._match("Problem (p1): solve this")["role"], "problem")
        self.assertEqual(self._match("Example (e2): shown below")["role"], "example")
        self.assertEqual(self._match("Low-Resource Tip: avoid X")["role"], "tip")

    def test_target_positives(self) -> None:
        self.assertEqual(self._match("问题（p1）：求解")["role"], "problem")
        self.assertEqual(self._match("示例（e2）：如下所示")["role"], "example")
        self.assertEqual(self._match("低资源提示：避免 X")["role"], "tip")

    def test_matched_language(self) -> None:
        self.assertEqual(
            self._match("Problem (p1): solve this")["matched_language"], "source"
        )
        self.assertEqual(
            self._match("问题（p1）：求解")["matched_language"], "target"
        )

    def test_identifier_behavior(self) -> None:
        self.assertEqual(
            self._match("Problem (abc_1): solve")["identifier"], "abc_1"
        )
        # The identifier group requires at least one [a-z0-9_] character.
        self.assertIsNone(self._match("Problem (): solve"))

    def test_negatives(self) -> None:
        for text in (
            "Question (q1): solve",
            "Exercise 1: solve",
            "Task (t1): solve",
            "Part (a): solve",
            "Hint: solve",
            "Note: solve",
            "Problem without an identifier",
            "A prefix Problem (p1) is not anchored",
        ):
            self.assertIsNone(self._match(text), text)

    def test_include_target_false_ignores_target_only(self) -> None:
        self.assertIsNone(self._match("问题（p1）：求解", include_target=False))
        self.assertEqual(
            self._match("Problem (p1): solve", include_target=False)["role"], "problem"
        )


class CandidateV2DifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = profile_module.validate_profile(
            json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        )
        cls.candidate_v2 = build_candidate_v2(cls.fixture)
        # The candidate must itself be a valid schema-V2 Profile.
        cls.validated_v2 = profile_module.validate_profile(cls.candidate_v2)
        cls.v1_contract = profile_module.profile_contract(cls.fixture)
        cls.v2_contract = profile_module.profile_contract(cls.validated_v2)

    def test_candidate_v2_is_schema_v2(self) -> None:
        self.assertEqual(self.validated_v2["schema_version"], 2)
        self.assertEqual(self.validated_v2["id"], "assignment-en-zh")

    def test_contract_keeps_historical_roles_and_records_complete_native_policy(self) -> None:
        self.assertEqual(self.v1_contract["source_schema_version"], 1)
        self.assertEqual(self.v2_contract["source_schema_version"], 2)
        for key in (
            "contract_version",
            "profile_id",
            "adapter",
            "auxiliary_dispositions",
            "constraints",
        ):
            self.assertEqual(self.v1_contract[key], self.v2_contract[key], key)
        self.assertEqual(self.v2_contract["roles"][:3], self.v1_contract["roles"])
        for offset, (role, kind, style, output) in enumerate(NATIVE_KIND_ROLE_SPECS, 3):
            self.assertEqual(self.v2_contract["roles"][offset], {
                "role": role,
                "selectors": [{"node_types": [kind]}],
                "style": style,
                "grouping": "none",
                "output": output,
                "docx_regroup": False,
            })
            self.assertEqual(self.v2_contract["role_inventory"][role]["output"], output)
        for role in ("problem", "example", "tip"):
            self.assertEqual(
                self.v1_contract["role_inventory"][role],
                self.v2_contract["role_inventory"][role],
                role,
            )

    def test_semantic_match_equivalent_on_shared_texts(self) -> None:
        for text in (
            "Problem (p1): solve",
            "问题（p1）：求解",
            "Example (e2): shown",
            "示例（e2）：如下",
            "Low-Resource Tip: avoid",
            "低资源提示：避免",
            "Question (q1): solve",
            "Part (a): solve",
        ):
            v1 = profile_module.semantic_match(self.fixture, text)
            v2 = profile_module.semantic_match(self.validated_v2, text)
            if v1 is None:
                self.assertIsNone(v2, text)
                continue
            self.assertEqual(v1["role"], v2["role"], text)
            self.assertEqual(v1["matched_language"], v2["matched_language"], text)
            self.assertEqual(v1["identifier"], v2["identifier"], text)


class V1BranchAuditTests(unittest.TestCase):
    """The documented V1 source audit must be executable and fail closed."""

    def test_runtime_markers_match_the_complete_allowlist(self) -> None:
        self.assertEqual(collect_v1_runtime_markers(), V1_RUNTIME_ALLOWLIST)

    def test_unregistered_v1_branch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-source-audit-extra-") as temp:
            root = Path(temp)
            (root / "new_runtime.py").write_text(
                "def _validate_v1(profile):\n"
                "    return profile.get('schema_version') == 1\n",
                encoding="utf-8",
            )
            self.assertEqual(
                set(collect_v1_runtime_markers(root)),
                {
                    ("new_runtime.py", "function", "_validate_v1"),
                    (
                        "new_runtime.py",
                        "schema-branch",
                        "profile.get('schema_version') == 1",
                    ),
                },
            )

    def test_duplicate_dispatch_addition_and_removal_change_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-duplicate-dispatch-") as temp:
            root = Path(temp)
            path = root / "pipeline.py"
            statement = "if profile.get('schema_version') == 2: pass\n"
            key = ("pipeline.py", "schema-branch", "profile.get('schema_version') == 2")
            for count in (3, 4, 2):
                path.write_text(statement * count, encoding="utf-8")
                self.assertEqual(collect_v1_runtime_markers(root)[key], count)

    def test_missing_allowlist_entry_is_detected(self) -> None:
        actual = collect_v1_runtime_markers()
        incomplete = V1_RUNTIME_ALLOWLIST - Counter({
            ("profile.py", "function", "_validate_v1"): 1
        })
        self.assertNotEqual(actual, incomplete)

    def test_schema_v2_comparison_and_derived_dispatch_are_detected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-source-audit-v2-dispatch-") as temp:
            root = Path(temp)
            (root / "dispatch.py").write_text(
                "schema_v2 = profile.get('schema_version') == 2\n"
                "requires_docx = schema_v2 or bool(compile_hint)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                set(collect_v1_runtime_markers(root)),
                {
                    (
                        "dispatch.py",
                        "derived-schema",
                        "schema_v2 = profile.get('schema_version') == 2",
                    ),
                    (
                        "dispatch.py",
                        "derived-schema",
                        "requires_docx = schema_v2 or bool(compile_hint)",
                    ),
                    (
                        "dispatch.py",
                        "schema-branch",
                        "profile.get('schema_version') == 2",
                    ),
                },
            )


class V1AdversarialTests(unittest.TestCase):
    def _mutated(self, **changes) -> dict:
        value = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        for key, replacement in changes.items():
            value[key] = replacement
        return value

    def test_unsupported_schema_rejected(self) -> None:
        with self.assertRaises(ValueError):
            profile_module.validate_profile(self._mutated(schema_version=3))

    def test_non_legacy_style_rejected(self) -> None:
        groups = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))["semantics"][
            "groups"
        ]
        groups.append(
            {
                "role": "question",
                "source_pattern": "^Question\\s*[（(](?P<identifier>[a-z0-9_]+)[)）]",
                "target_pattern": "^问题\\s*[（(](?P<identifier>[a-z0-9_]+)[)）]",
                "docx_regroup": False,
                "style": "note",
            }
        )
        mutated = self._mutated()
        mutated["semantics"] = {"groups": groups}
        with self.assertRaisesRegex(ValueError, "V1 style"):
            profile_module.validate_profile(mutated)

    def test_duplicate_role_rejected(self) -> None:
        groups = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))["semantics"][
            "groups"
        ]
        groups.append(copy.deepcopy(groups[0]))
        mutated = self._mutated()
        mutated["semantics"] = {"groups": groups}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            profile_module.validate_profile(mutated)


if __name__ == "__main__":
    unittest.main()
