#!/usr/bin/env python3
"""Build the deterministic manifest for the complete installable Skill subtree."""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
MANIFEST_NAME = "release-manifest.json"
SKILL_NAME = "make-bilingual-study-pdf"
WINDOWS_RESERVED_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"|?*')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        line = f"{record['path']}\0{record['size']}\0{record['sha256']}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def is_generated_cache(relative: PurePosixPath) -> bool:
    return "__pycache__" in relative.parts and relative.suffix in {".pyc", ".pyo"}


def valid_payload_path(value: str) -> bool:
    if not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        return False
    if unicodedata.normalize("NFC", value) != value:
        return False
    for part in path.parts:
        if part in {"", ".", ".."} or part.endswith((".", " ")):
            return False
        if any(ord(character) < 32 for character in part):
            return False
        if any(character in WINDOWS_FORBIDDEN_CHARACTERS for character in part):
            return False
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED_STEMS:
            return False
    return True


def portable_path_key(value: str) -> str:
    path = PurePosixPath(value)
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def payload_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    manifest_key = portable_path_key(MANIFEST_NAME)
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in payload: {relative}")
        if relative.as_posix() == MANIFEST_NAME or is_generated_cache(relative):
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        if not valid_payload_path(name):
            raise ValueError(f"payload path is not portable: {name!r}")
        folded = portable_path_key(name)
        if folded == manifest_key:
            raise ValueError(f"payload path conflicts with reserved manifest name: {name!r}")
        if folded in casefolded and casefolded[folded] != name:
            raise ValueError(
                f"case-insensitive payload path collision: {casefolded[folded]!r}, {name!r}"
            )
        casefolded[folded] = name
        records.append(
            {"path": name, "size": path.stat().st_size, "sha256": sha256_file(path)}
        )
    records.sort(key=lambda record: record["path"])
    return records


def build_manifest(root: Path) -> dict[str, Any]:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    records = payload_records(root)
    return {
        "schema_version": 1,
        "skill": SKILL_NAME,
        "version": version,
        "hash_algorithm": "sha256",
        "tree_sha256": tree_sha256(records),
        "files": records,
    }


def encoded_manifest(root: Path) -> bytes:
    return (
        json.dumps(build_manifest(root), ensure_ascii=False, indent=2, sort_keys=False)
        + "\n"
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill-root",
        type=Path,
        default=DEFAULT_SKILL_ROOT,
        help="Installable Skill subtree (defaults to the repository's canonical path).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed manifest differs instead of rewriting it.",
    )
    args = parser.parse_args()
    root = args.skill_root.resolve()
    target = root / MANIFEST_NAME
    try:
        manifest = build_manifest(root)
        expected = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"manifest generation failed: {exc}")
        return 1
    if args.check:
        try:
            actual = target.read_bytes()
        except OSError as exc:
            print(f"manifest check failed: {exc}")
            return 1
        if actual != expected:
            print(f"manifest check failed: regenerate {MANIFEST_NAME}")
            return 1
        print(f"manifest check passed: {len(manifest['files'])} files")
        return 0
    try:
        target.write_bytes(expected)
    except OSError as exc:
        print(f"manifest write failed: {exc}")
        return 1
    print(f"wrote {MANIFEST_NAME}: {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
