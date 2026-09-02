#!/usr/bin/env python3
"""Check or normalize installable Skill text files to repository LF bytes."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path, PurePosixPath

from _payload_fs import (
    DirectoryChain,
    WINDOWS_REPARSE_ATTRIBUTE,
    atomic_replace_bytes,
    is_reparse_point,
    iter_safe_files,
    lexical_absolute,
    read_regular_bytes as _read_regular_bytes,
    validate_skill_root_chain as _validate_skill_root_chain,
)


REPOSITORY = lexical_absolute(Path(__file__)).parent.parent
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
SKILL_DIRECTORY = "make-bilingual-study-pdf"
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".svg",
    ".tex",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {"VERSION"}
PASSTHROUGH_BINARY_SUFFIXES = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".webp",
}


def validate_skill_root_chain(
    repository: Path,
    root: Path,
    expected: DirectoryChain | None = None,
) -> DirectoryChain:
    return _validate_skill_root_chain(
        repository,
        root,
        SKILL_DIRECTORY,
        expected,
        context="Skill tree",
    )


def read_regular_bytes(
    repository: Path,
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
) -> bytes:
    return _read_regular_bytes(
        repository,
        root,
        SKILL_DIRECTORY,
        path,
        label,
        expected_status,
        expected_chain,
        context="Skill tree",
    )


def display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(REPOSITORY).as_posix()
    except ValueError:
        return path.relative_to(root).as_posix()


def classify_payload(relative: PurePosixPath) -> str:
    suffix = relative.suffix.lower()
    if relative.name in TEXT_FILENAMES or suffix in TEXT_SUFFIXES:
        return "text"
    if suffix in PASSTHROUGH_BINARY_SUFFIXES:
        return "binary"
    raise ValueError(
        "unsupported file type in Skill tree; add an explicit text or binary "
        f"classification before release: {relative.as_posix()}"
    )


def validate_text_payload(payload: bytes, label: str) -> None:
    if b"\x00" in payload:
        raise ValueError(f"NUL bytes are not allowed in Skill text files: {label}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Skill text file is not valid UTF-8: {label}") from exc


def find_changes(
    repository: Path,
    root: Path,
    expected_chain: DirectoryChain,
) -> list[tuple[Path, str, os.stat_result, bytes, bytes]]:
    files = iter_safe_files(
        repository,
        root,
        SKILL_DIRECTORY,
        expected_chain,
        context="Skill tree",
    )
    classifications = [
        (path, relative, status, classify_payload(relative))
        for path, relative, status in files
    ]
    changes: list[tuple[Path, str, os.stat_result, bytes, bytes]] = []
    for path, relative, status, payload_type in classifications:
        if payload_type == "binary":
            continue
        label = relative.as_posix()
        payload = read_regular_bytes(
            repository, root, path, label, status, expected_chain
        )
        validate_text_payload(payload, label)
        normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if normalized != payload:
            changes.append(
                (path, display_path(root, path), status, payload, normalized)
            )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true", help="Rewrite non-LF line endings in place."
    )
    args = parser.parse_args()
    repository = lexical_absolute(REPOSITORY)
    root = lexical_absolute(SKILL_ROOT)
    try:
        chain = validate_skill_root_chain(repository, root)
        changes = find_changes(repository, root, chain)
        if args.fix:
            for path, relative, status, payload, normalized in changes:
                atomic_replace_bytes(
                    repository,
                    root,
                    SKILL_DIRECTORY,
                    path,
                    relative,
                    normalized,
                    status,
                    chain,
                    expected_payload=payload,
                    mode=stat.S_IMODE(status.st_mode),
                    temporary_prefix=f".{path.name}.eol-",
                    context="Skill tree",
                )
    except (OSError, ValueError) as exc:
        print(f"Skill EOL check failed: {exc}")
        return 1

    if changes and not args.fix:
        print("non-LF Skill files:")
        for _, relative, _, _, _ in changes:
            print(f"- {relative}")
        return 1
    action = "normalized" if args.fix else "verified"
    print(f"{action} Skill line endings: {len(changes)} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
