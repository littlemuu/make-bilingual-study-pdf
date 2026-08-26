#!/usr/bin/env python3
"""Check or normalize installable Skill text files to repository LF bytes."""
from __future__ import annotations

import argparse
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
BINARY_SUFFIXES = {".pdf", ".png", ".pyc", ".pyo"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true", help="Rewrite non-LF line endings in place."
    )
    args = parser.parse_args()
    changed: list[str] = []
    for path in sorted(SKILL_ROOT.rglob("*")):
        if (
            not path.is_file()
            or "__pycache__" in path.parts
            or path.suffix.lower() in BINARY_SUFFIXES
        ):
            continue
        payload = path.read_bytes()
        normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if normalized == payload:
            continue
        relative = path.relative_to(REPOSITORY).as_posix()
        changed.append(relative)
        if args.fix:
            path.write_bytes(normalized)
    if changed and not args.fix:
        print("non-LF Skill files:")
        for relative in changed:
            print(f"- {relative}")
        return 1
    action = "normalized" if args.fix else "verified"
    print(f"{action} Skill line endings: {len(changed)} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
