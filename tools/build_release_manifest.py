#!/usr/bin/env python3
"""Build the deterministic manifest for the complete installable Skill subtree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from _payload_fs import (
    DirectoryChain,
    atomic_replace_bytes,
    hash_regular_file,
    inspect_regular_file,
    iter_safe_files,
    lexical_absolute,
    portable_path_key,
    read_regular_bytes,
    repository_for_skill_root as _repository_for_skill_root,
    valid_payload_path,
    validate_skill_root_chain as _validate_skill_root_chain,
)


REPOSITORY = lexical_absolute(Path(__file__)).parent.parent
DEFAULT_SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
MANIFEST_NAME = "release-manifest.json"
SKILL_NAME = "make-bilingual-study-pdf"
SKILL_DIRECTORY = "make-bilingual-study-pdf"


def repository_for_skill_root(root: Path) -> Path:
    return _repository_for_skill_root(
        root,
        default_root=DEFAULT_SKILL_ROOT,
        default_repository=REPOSITORY,
        skill_directory=SKILL_DIRECTORY,
    )


def validate_skill_root_chain(
    root: Path, expected: DirectoryChain | None = None
) -> DirectoryChain:
    return _validate_skill_root_chain(
        repository_for_skill_root(root),
        root,
        SKILL_DIRECTORY,
        expected,
    )


def tree_sha256(records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        line = f"{record['path']}\0{record['size']}\0{record['sha256']}\n"
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def is_generated_cache(relative: PurePosixPath) -> bool:
    return "__pycache__" in relative.parts and relative.suffix in {".pyc", ".pyo"}


def _payload_records_and_version(
    root: Path, expected_chain: DirectoryChain
) -> tuple[list[dict[str, Any]], bytes]:
    repository = repository_for_skill_root(root)
    records: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    manifest_key = portable_path_key(MANIFEST_NAME)
    version_payload: bytes | None = None
    files = iter_safe_files(
        repository, root, SKILL_DIRECTORY, expected_chain
    )
    for path, relative, status in files:
        name = relative.as_posix()
        if name == MANIFEST_NAME or is_generated_cache(relative):
            continue
        if not valid_payload_path(name):
            raise ValueError(f"payload path is not portable: {name!r}")
        folded = portable_path_key(name)
        if folded == manifest_key:
            raise ValueError(
                f"payload path conflicts with reserved manifest name: {name!r}"
            )
        if folded in casefolded and casefolded[folded] != name:
            raise ValueError(
                "case-insensitive payload path collision: "
                f"{casefolded[folded]!r}, {name!r}"
            )
        casefolded[folded] = name
        if name == "VERSION":
            version_payload = read_regular_bytes(
                repository,
                root,
                SKILL_DIRECTORY,
                path,
                name,
                status,
                expected_chain,
            )
            size = len(version_payload)
            digest = hashlib.sha256(version_payload).hexdigest()
        else:
            size, digest = hash_regular_file(
                repository,
                root,
                SKILL_DIRECTORY,
                path,
                name,
                status,
                expected_chain,
            )
        records.append({"path": name, "size": size, "sha256": digest})
    records.sort(key=lambda record: record["path"])
    if version_payload is None:
        raise ValueError("payload is missing VERSION")
    return records, version_payload


def payload_records(
    root: Path, expected_chain: DirectoryChain
) -> list[dict[str, Any]]:
    records, _ = _payload_records_and_version(root, expected_chain)
    return records


def build_manifest(root: Path, expected_chain: DirectoryChain) -> dict[str, Any]:
    records, raw_version = _payload_records_and_version(root, expected_chain)
    version = raw_version.decode("utf-8").strip()
    return {
        "schema_version": 1,
        "skill": SKILL_NAME,
        "version": version,
        "hash_algorithm": "sha256",
        "tree_sha256": tree_sha256(records),
        "files": records,
    }


def encoded_manifest(root: Path, expected_chain: DirectoryChain) -> bytes:
    return (
        json.dumps(
            build_manifest(root, expected_chain),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
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
    root = lexical_absolute(args.skill_root)
    repository = repository_for_skill_root(root)
    target = root / MANIFEST_NAME
    try:
        chain = validate_skill_root_chain(root)
        target_status = inspect_regular_file(
            repository,
            root,
            SKILL_DIRECTORY,
            target,
            MANIFEST_NAME,
            chain,
            allow_missing=True,
        )
        manifest = build_manifest(root, chain)
        expected = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"manifest generation failed: {exc}")
        return 1
    if args.check:
        if target_status is None:
            print(f"manifest check failed: {MANIFEST_NAME} is missing")
            return 1
        try:
            actual = read_regular_bytes(
                repository,
                root,
                SKILL_DIRECTORY,
                target,
                MANIFEST_NAME,
                target_status,
                chain,
            )
        except (OSError, ValueError) as exc:
            print(f"manifest check failed: {exc}")
            return 1
        if actual != expected:
            print(f"manifest check failed: regenerate {MANIFEST_NAME}")
            return 1
        print(f"manifest check passed: {len(manifest['files'])} files")
        return 0
    try:
        atomic_replace_bytes(
            repository,
            root,
            SKILL_DIRECTORY,
            target,
            MANIFEST_NAME,
            expected,
            target_status,
            chain,
            temporary_prefix=f".{MANIFEST_NAME}.",
        )
    except (OSError, ValueError) as exc:
        print(f"manifest write failed: {exc}")
        return 1
    print(f"wrote {MANIFEST_NAME}: {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
