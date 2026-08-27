#!/usr/bin/env python3
"""Build the deterministic manifest for the complete installable Skill subtree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
MANIFEST_NAME = "release-manifest.json"
SKILL_NAME = "make-bilingual-study-pdf"
WINDOWS_RESERVED_STEMS = {
    "con",
    "conin$",
    "conout$",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(0, 10)),
    *(f"lpt{index}" for index in range(0, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}
WINDOWS_FORBIDDEN_CHARACTERS = set('<>:"|?*')
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        or getattr(status, "st_reparse_tag", 0)
    )


def unsafe_link_kind(status: os.stat_result) -> str | None:
    if stat.S_ISLNK(status.st_mode):
        return "symbolic links"
    if is_reparse_point(status):
        return "reparse points"
    return None


def reject_unsafe_status(
    status: os.stat_result, label: str, *, require_directory: bool = False
) -> None:
    link_kind = unsafe_link_kind(status)
    if link_kind:
        raise ValueError(f"{link_kind} are not allowed in payload: {label}")
    if require_directory:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"payload root must be a regular directory: {label}")
    elif not stat.S_ISREG(status.st_mode):
        raise ValueError(
            f"non-regular filesystem entries are not allowed in payload: {label}"
        )


def iter_safe_entries(
    root: Path,
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    """Walk without following symlinks, junctions, or other reparse points."""
    root_status = os.lstat(root)
    reject_unsafe_status(root_status, str(root), require_directory=True)
    observed: list[tuple[Path, PurePosixPath, os.stat_result]] = []

    def visit(
        directory: Path,
        relative_directory: PurePosixPath,
        expected_status: os.stat_result,
    ) -> None:
        current_status = os.lstat(directory)
        label = relative_directory.as_posix() or str(root)
        reject_unsafe_status(current_status, label, require_directory=True)
        if not metadata_matches(expected_status, current_status):
            raise ValueError(f"payload directory changed while traversing: {label}")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            status = os.lstat(path)
            observed.append((path, relative, status))
            if unsafe_link_kind(status) is None and stat.S_ISDIR(status.st_mode):
                visit(path, relative, status)

    visit(root, PurePosixPath(), root_status)
    return observed


def ensure_safe_parent_chain(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"payload path escapes root: {path}") from exc
    current = root
    reject_unsafe_status(os.lstat(current), str(current), require_directory=True)
    for part in relative.parts[:-1]:
        current /= part
        reject_unsafe_status(
            os.lstat(current),
            current.relative_to(root).as_posix(),
            require_directory=True,
        )


def metadata_matches(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def open_regular_fd(root: Path, path: Path, label: str) -> tuple[int, os.stat_result]:
    ensure_safe_parent_chain(root, path)
    before = os.lstat(path)
    reject_unsafe_status(before, label)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        reject_unsafe_status(opened, label)
        if not metadata_matches(before, opened):
            raise ValueError(f"payload entry changed while opening: {label}")
        return fd, opened
    except Exception:
        os.close(fd)
        raise


def read_regular_bytes(root: Path, path: Path, label: str) -> bytes:
    fd, opened = open_regular_fd(root, path, label)
    with os.fdopen(fd, "rb") as handle:
        payload = handle.read()
        finished = os.fstat(handle.fileno())
    if not metadata_matches(opened, finished) or len(payload) != finished.st_size:
        raise ValueError(f"payload entry changed while reading: {label}")
    return payload


def hash_regular_file(root: Path, path: Path, label: str) -> tuple[int, str]:
    fd, opened = open_regular_fd(root, path, label)
    digest = hashlib.sha256()
    total = 0
    with os.fdopen(fd, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
        finished = os.fstat(handle.fileno())
    if not metadata_matches(opened, finished) or total != finished.st_size:
        raise ValueError(f"payload entry changed while hashing: {label}")
    return finished.st_size, digest.hexdigest()


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
        windows_stem = part.split(".", 1)[0].rstrip(" ").casefold()
        if windows_stem in WINDOWS_RESERVED_STEMS:
            return False
    return True


def portable_path_key(value: str) -> str:
    path = PurePosixPath(value)
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def payload_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    casefolded: dict[str, str] = {}
    manifest_key = portable_path_key(MANIFEST_NAME)
    for path, relative, status in iter_safe_entries(root):
        link_kind = unsafe_link_kind(status)
        if link_kind:
            raise ValueError(f"{link_kind} are not allowed in payload: {relative}")
        if stat.S_ISDIR(status.st_mode):
            continue
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(
                f"non-regular filesystem entries are not allowed in payload: {relative}"
            )
        if relative.as_posix() == MANIFEST_NAME or is_generated_cache(relative):
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
        size, digest = hash_regular_file(root, path, name)
        records.append({"path": name, "size": size, "sha256": digest})
    records.sort(key=lambda record: record["path"])
    return records


def build_manifest(root: Path) -> dict[str, Any]:
    version_path = root / "VERSION"
    version = read_regular_bytes(root, version_path, "VERSION").decode("utf-8").strip()
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
    root = Path(os.path.abspath(args.skill_root))
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
            actual = read_regular_bytes(root, target, MANIFEST_NAME)
        except (OSError, ValueError) as exc:
            print(f"manifest check failed: {exc}")
            return 1
        if actual != expected:
            print(f"manifest check failed: regenerate {MANIFEST_NAME}")
            return 1
        print(f"manifest check passed: {len(manifest['files'])} files")
        return 0
    temporary_path: Path | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{MANIFEST_NAME}.", dir=root
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError as exc:
        print(f"manifest write failed: {exc}")
        return 1
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    print(f"wrote {MANIFEST_NAME}: {len(manifest['files'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
