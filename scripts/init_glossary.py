#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import read_jsonl, sha256_file, write_json
from profile import canonical_profile_sha256, load_work_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a reviewable per-document glossary before translation planning."
    )
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    work_dir = args.work_dir.expanduser().resolve()
    blocks_path = work_dir / "blocks.jsonl"
    if not blocks_path.is_file():
        raise SystemExit(f"missing source blocks: {blocks_path}")
    translation_dir = work_dir / "translation"
    translation_dir.mkdir(exist_ok=True)
    glossary_path = translation_dir / "glossary.json"
    if glossary_path.exists() and not args.force:
        raise SystemExit(f"glossary already exists: {glossary_path}")

    blocks = read_jsonl(blocks_path)
    try:
        profile = load_work_profile(work_dir)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    glossary = {
        "schema_version": 1,
        "profile_id": profile["id"],
        "profile_sha256": canonical_profile_sha256(profile),
        "target_language": profile["translation"]["target_language"],
        "source_blocks_sha256": sha256_file(blocks_path),
        "instructions": (
            "Review before planning. Add repeated domain terms only; use enforce=true "
            "only when every occurrence should contain one of the approved targets."
        ),
        "terms": [],
    }
    write_json(glossary_path, glossary)
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
    main()
