#!/usr/bin/env python3
"""Small registries shared by Profile validation and downstream semantic stages.

This module deliberately contains data-only capabilities.  Importing it must not pull
in python-docx, an extraction backend, or any renderer.  A registered name therefore
means that V2.3 has a defined semantic contract for the capability; the concrete
stage remains responsible for implementing and auditing that contract.
"""
from __future__ import annotations

from dataclasses import dataclass


OUTPUT_DISPOSITIONS = frozenset(
    {
        "bilingual",
        "source-only",
        "visual-once",
        "artifact-omitted",
    }
)
GROUPING_MODES = frozenset({"none", "structural-container"})
SELECTOR_FIELDS = frozenset(
    {
        "adapter_role",
        "node_types",
        "sub_types",
        "text_levels",
        "source_pattern",
        "target_pattern",
    }
)
AUXILIARY_ROLES = frozenset(
    {
        "page-header",
        "page-footer",
        "page-number",
        "marginalia",
    }
)


@dataclass(frozen=True)
class StyleSpec:
    id: str
    family: str
    supports_structural_container: bool = False
    requires_single_divider: bool = False
    legacy_assignment_style: bool = False


_STYLES = {
    spec.id: spec
    for spec in (
        # V2.2 styles.  Their identifiers and renderer behavior are compatibility
        # promises for assignment-en-zh.
        StyleSpec("problem", "callout", True, True, True),
        StyleSpec("example", "callout", True, True, True),
        StyleSpec("tip", "callout", True, True, True),
        # Shared structural study-document styles.
        StyleSpec("document-title", "title"),
        StyleSpec("author-affiliation", "byline"),
        StyleSpec("abstract", "callout", True, True),
        StyleSpec("section-heading", "heading"),
        StyleSpec("body", "body"),
        StyleSpec("caption", "caption"),
        StyleSpec("footnote", "footnote"),
        StyleSpec("reference", "reference"),
        StyleSpec("equation", "equation"),
        StyleSpec("code", "code"),
        StyleSpec("visual", "visual"),
        StyleSpec("table", "table"),
        # Lecture-note semantic containers.
        StyleSpec("definition", "callout", True, True),
        StyleSpec("theorem", "callout", True, True),
        StyleSpec("proof", "proof", True, True),
        StyleSpec("note", "callout", True, True),
        StyleSpec("warning", "callout", True, True),
        StyleSpec("exercise", "callout", True, True),
    )
}


def registered_style_ids() -> frozenset[str]:
    return frozenset(_STYLES)


def get_style(style_id: str) -> StyleSpec:
    try:
        return _STYLES[style_id]
    except KeyError as exc:
        raise ValueError(f"unsupported semantic style: {style_id}") from exc


@dataclass(frozen=True)
class ConstraintSpec:
    id: str
    description: str


_CONSTRAINTS = {
    spec.id: spec
    for spec in (
        ConstraintSpec(
            "heading-hierarchy-v1",
            "Heading levels remain ordered and never jump by more than one level.",
        ),
        ConstraintSpec(
            "visual-relations-v1",
            "Captions and table footnotes retain a valid visual/table parent relation.",
        ),
        ConstraintSpec(
            "academic-paper-order-v1",
            "Title, byline, abstract, body sections, and references remain in paper order.",
        ),
        ConstraintSpec(
            "lecture-proof-order-v1",
            "A proof follows a theorem-family item in the same lecture-note section.",
        ),
    )
}


def registered_constraint_ids() -> frozenset[str]:
    return frozenset(_CONSTRAINTS)


def get_constraint(constraint_id: str) -> ConstraintSpec:
    try:
        return _CONSTRAINTS[constraint_id]
    except KeyError as exc:
        raise ValueError(f"unsupported semantic constraint: {constraint_id}") from exc
