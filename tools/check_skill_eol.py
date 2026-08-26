#!/usr/bin/env python3
"""Check or normalize installable Skill text files to repository LF bytes."""
from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path, PurePosixPath


REPOSITORY = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY / "skills" / "make-bilingual-study-pdf"
BINARY_SUFFIXES = {".pdf", ".png", ".pyc", ".pyo"}
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        or getattr(status, "st_reparse_tag", 0)
    )


def reject_unsafe_status(
    status: os.stat_result, label: str, *, require_directory: bool = False
) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise ValueError(f"symbolic links are not allowed in Skill tree: {label}")
    if is_reparse_point(status):
        raise ValueError(f"reparse points are not allowed in Skill tree: {label}")
    if require_directory:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"Skill root must be a regular directory: {label}")
    else:
        if not stat.S_ISREG(status.st_mode):
            raise ValueError(
                f"non-regular filesystem entries are not allowed in Skill tree: {label}"
            )
        if status.st_nlink != 1:
            raise ValueError(
                f"multiply linked files are not allowed in Skill tree: {label}"
            )


def metadata_matches(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and left.st_nlink == right.st_nlink
    )


def iter_safe_files(
    root: Path,
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    """Inventory the complete tree without following links or reparse points."""
    root_status = os.lstat(root)
    reject_unsafe_status(root_status, str(root), require_directory=True)
    files: list[tuple[Path, PurePosixPath, os.stat_result]] = []

    def visit(
        directory: Path,
        relative_directory: PurePosixPath,
        expected_status: os.stat_result,
    ) -> None:
        current_status = os.lstat(directory)
        label = relative_directory.as_posix() or str(root)
        reject_unsafe_status(current_status, label, require_directory=True)
        if not metadata_matches(expected_status, current_status):
            raise ValueError(f"Skill directory changed while traversing: {label}")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            status = os.lstat(path)
            label = relative.as_posix()
            if stat.S_ISDIR(status.st_mode):
                reject_unsafe_status(status, label, require_directory=True)
                visit(path, relative, status)
            else:
                reject_unsafe_status(status, label)
                files.append((path, relative, status))

    visit(root, PurePosixPath(), root_status)
    return files


def ensure_safe_parent_chain(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Skill path escapes root: {path}") from exc
    current = root
    reject_unsafe_status(os.lstat(current), str(current), require_directory=True)
    for part in relative.parts[:-1]:
        current /= part
        reject_unsafe_status(
            os.lstat(current),
            current.relative_to(root).as_posix(),
            require_directory=True,
        )


def open_descriptor(path: Path, *, writable: bool) -> int:
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        desired_access = 0x80000000 | (0x40000000 if writable else 0)
        handle = create_file(
            str(path),
            desired_access,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        flags = os.O_BINARY | (os.O_RDWR if writable else os.O_RDONLY)
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDWR if writable else os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def open_regular_fd(
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    *,
    writable: bool,
) -> tuple[int, os.stat_result]:
    ensure_safe_parent_chain(root, path)
    before = os.lstat(path)
    reject_unsafe_status(before, label)
    if not metadata_matches(expected_status, before):
        raise ValueError(f"Skill file changed after inventory: {label}")
    descriptor = open_descriptor(path, writable=writable)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        reject_unsafe_status(opened, label)
        reject_unsafe_status(after_open, label)
        if not (
            metadata_matches(before, opened)
            and metadata_matches(opened, after_open)
        ):
            raise ValueError(f"Skill file changed while opening: {label}")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def read_regular_bytes(
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
) -> bytes:
    descriptor, opened = open_regular_fd(
        root, path, label, expected_status, writable=False
    )
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        finished = os.fstat(descriptor)
        final_path = os.lstat(path)
        reject_unsafe_status(finished, label)
        reject_unsafe_status(final_path, label)
        if not (
            metadata_matches(opened, finished)
            and metadata_matches(finished, final_path)
            and len(payload) == finished.st_size
        ):
            raise ValueError(f"Skill file changed while reading: {label}")
        return payload
    finally:
        os.close(descriptor)


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("failed to write normalized Skill file")
        view = view[written:]


def normalize_regular_file(
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_payload: bytes,
    normalized: bytes,
) -> None:
    descriptor, opened = open_regular_fd(
        root, path, label, expected_status, writable=True
    )
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        current_payload = b"".join(chunks)
        before_write = os.fstat(descriptor)
        current_path = os.lstat(path)
        reject_unsafe_status(before_write, label)
        reject_unsafe_status(current_path, label)
        if (
            current_payload != expected_payload
            or not metadata_matches(opened, before_write)
            or not metadata_matches(before_write, current_path)
        ):
            raise ValueError(f"Skill file changed before normalization: {label}")

        os.lseek(descriptor, 0, os.SEEK_SET)
        write_all(descriptor, normalized)
        os.ftruncate(descriptor, len(normalized))
        os.fsync(descriptor)

        finished = os.fstat(descriptor)
        final_path = os.lstat(path)
        reject_unsafe_status(finished, label)
        reject_unsafe_status(final_path, label)
        if (
            not os.path.samestat(opened, finished)
            or not os.path.samestat(finished, final_path)
            or finished.st_size != len(normalized)
        ):
            raise ValueError(f"Skill file changed while normalizing: {label}")
    finally:
        os.close(descriptor)


def display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(REPOSITORY).as_posix()
    except ValueError:
        return path.relative_to(root).as_posix()


def find_changes(
    root: Path,
) -> list[tuple[Path, str, os.stat_result, bytes, bytes]]:
    files = iter_safe_files(root)
    changes: list[tuple[Path, str, os.stat_result, bytes, bytes]] = []
    for path, relative, status in files:
        if "__pycache__" in relative.parts or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        label = relative.as_posix()
        payload = read_regular_bytes(root, path, label, status)
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
    root = Path(os.path.abspath(SKILL_ROOT))
    try:
        changes = find_changes(root)
        if args.fix:
            for path, relative, status, payload, normalized in changes:
                normalize_regular_file(
                    root, path, relative, status, payload, normalized
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
