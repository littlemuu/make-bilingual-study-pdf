#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from audit_source import validate_source_audit_binding
from common import json_loads_strict
from profile import canonical_profile_sha256, load_work_profile
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    prepare_artifact_directory,
    read_artifact_text,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
)


def _read_jsonl(path: Path, work_dir: Path) -> list[dict]:
    values: list[dict] = []
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


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    validate_artifact_file(path, boundary=work_dir, allow_missing=True)
    return os.path.lexists(path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a reviewable per-document glossary before translation planning."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    _, source_binding_errors = validate_source_audit_binding(
        work_dir, work_dir / "source-audit.json"
    )
    if source_binding_errors:
        raise SystemExit(
            "source audit bindings are stale; glossary initialization is blocked: "
            + "; ".join(source_binding_errors)
        )
    blocks_path = work_dir / "blocks.jsonl"
    blocks = _read_jsonl(blocks_path, work_dir)
    blocks_sha256 = sha256_artifact(blocks_path, boundary=work_dir)
    try:
        profile = load_work_profile(work_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    translation_dir = work_dir / "translation"
    prepare_artifact_directory(translation_dir, boundary=work_dir)
    glossary_path = translation_dir / "glossary.json"
    if _artifact_exists(glossary_path, work_dir) and not args.force:
        raise SystemExit(f"glossary already exists: {glossary_path}")

    glossary = {
        "schema_version": 1,
        "profile_id": profile["id"],
        "profile_sha256": canonical_profile_sha256(profile),
        "target_language": profile["translation"]["target_language"],
        "source_blocks_sha256": blocks_sha256,
        "instructions": (
            "Review before planning. Add repeated domain terms only; use enforce=true "
            "only when every occurrence should contain one of the approved targets."
        ),
        "terms": [],
    }
    _atomic_write_json(glossary_path, glossary, work_dir)
    print(
        json.dumps(
            {
                "glossary": str(glossary_path),
                "source_blocks": len(blocks),
                "terms": 0,
                "next": "review terms, then run prepare_translation.py",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except ArtifactSafetyError as exc:
        raise SystemExit(str(exc)) from exc
