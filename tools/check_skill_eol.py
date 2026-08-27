#!/usr/bin/env python3
"""Check or normalize installable Skill text files to repository LF bytes."""
from __future__ import annotations

import argparse
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath


REPOSITORY = Path(os.path.abspath(__file__)).parent.parent
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
WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
DirectoryChain = tuple[tuple[Path, os.stat_result], ...]


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


def directory_identity_matches(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(
        right.st_mode
    )


def lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving links or reparse points."""
    return Path(os.path.abspath(os.fspath(path)))


def repository_directory_paths(repository: Path) -> tuple[tuple[Path, str], ...]:
    """Return every lexical directory from the filesystem anchor to the repo."""
    repository = lexical_absolute(repository)
    if not repository.anchor:
        raise ValueError(f"repository root must be absolute: {repository}")
    anchor = Path(repository.anchor)
    paths: list[tuple[Path, str]] = [(anchor, "filesystem anchor")]
    current = anchor
    for part in repository.relative_to(anchor).parts:
        current /= part
        label = (
            "repository root"
            if current == repository
            else f"repository ancestor: {current}"
        )
        paths.append((current, label))
    return tuple(paths)


def validate_skill_root_chain(
    repository: Path,
    root: Path,
    expected: DirectoryChain | None = None,
) -> DirectoryChain:
    """Validate the repository, skills ancestor, and Skill root in that order."""
    repository = lexical_absolute(repository)
    root = lexical_absolute(root)
    skills = repository / "skills"
    expected_root = skills / SKILL_DIRECTORY
    if root != expected_root:
        raise ValueError(
            f"Skill root must be the canonical repository subtree: {expected_root}"
        )
    paths = repository_directory_paths(repository) + (
        (skills, "skills directory"),
        (root, "Skill root"),
    )
    if expected is not None and tuple(path for path, _ in expected) != tuple(
        path for path, _ in paths
    ):
        raise ValueError("Skill directory chain changed after validation")

    observed: list[tuple[Path, os.stat_result]] = []
    for index, (path, label) in enumerate(paths):
        status = os.lstat(path)
        reject_unsafe_status(status, label, require_directory=True)
        if expected is not None and not directory_identity_matches(
            expected[index][1], status
        ):
            raise ValueError(f"Skill directory changed after validation: {label}")
        observed.append((path, status))
    return tuple(observed)


def iter_safe_files(
    repository: Path,
    root: Path,
    expected_chain: DirectoryChain,
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    """Inventory the complete tree without following links or reparse points."""
    chain = validate_skill_root_chain(repository, root, expected_chain)
    root_status = chain[-1][1]
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


def ensure_safe_parent_chain(
    repository: Path,
    root: Path,
    path: Path,
    expected_chain: DirectoryChain,
    expected_parents: DirectoryChain | None = None,
) -> DirectoryChain:
    chain = validate_skill_root_chain(repository, root, expected_chain)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Skill path escapes root: {path}") from exc
    observed: list[tuple[Path, os.stat_result]] = list(chain)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        status = os.lstat(current)
        reject_unsafe_status(
            status, current.relative_to(root).as_posix(), require_directory=True
        )
        observed.append((current, status))
    result = tuple(observed)
    if expected_parents is not None:
        if tuple(path for path, _ in expected_parents) != tuple(
            path for path, _ in result
        ):
            raise ValueError(f"Skill parent chain changed: {path}")
        for (parent, expected_status), (_, current_status) in zip(
            expected_parents, result
        ):
            if not directory_identity_matches(expected_status, current_status):
                raise ValueError(f"Skill parent directory changed: {parent}")
    return result


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
    repository: Path,
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
    *,
    writable: bool,
) -> tuple[int, os.stat_result]:
    ensure_safe_parent_chain(repository, root, path, expected_chain)
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
    repository: Path,
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
) -> bytes:
    descriptor, opened = open_regular_fd(
        repository,
        root,
        path,
        label,
        expected_status,
        expected_chain,
        writable=False,
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
        try:
            os.close(descriptor)
        except OSError:
            pass


def write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("failed to write normalized Skill file")
        view = view[written:]


def best_effort_fsync_directory(directory: Path) -> None:
    """Persist a completed rename where directory fsync is supported."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def normalize_regular_file(
    repository: Path,
    root: Path,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_payload: bytes,
    normalized: bytes,
    expected_chain: DirectoryChain,
) -> None:
    current_payload = read_regular_bytes(
        repository,
        root,
        path,
        label,
        expected_status,
        expected_chain,
    )
    if current_payload != expected_payload:
        raise ValueError(f"Skill file changed before normalization: {label}")

    parent_chain = ensure_safe_parent_chain(
        repository, root, path, expected_chain
    )
    temporary_path: Path | None = None
    temporary_identity: os.stat_result | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.eol-", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        ensure_safe_parent_chain(
            repository,
            root,
            path,
            expected_chain,
            expected_parents=parent_chain,
        )
        temporary_identity = os.fstat(descriptor)
        temporary_path_status = os.lstat(temporary_path)
        reject_unsafe_status(temporary_identity, f"temporary file for {label}")
        reject_unsafe_status(temporary_path_status, f"temporary file for {label}")
        if not directory_identity_matches(temporary_identity, temporary_path_status):
            raise ValueError(f"temporary Skill file changed after creation: {label}")

        write_all(descriptor, normalized)
        os.fsync(descriptor)
        mode = stat.S_IMODE(expected_status.st_mode)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary_path, mode)
        os.fsync(descriptor)

        finished = os.fstat(descriptor)
        reject_unsafe_status(finished, f"temporary file for {label}")
        if (
            not directory_identity_matches(temporary_identity, finished)
            or finished.st_size != len(normalized)
        ):
            raise ValueError(f"temporary Skill file changed while writing: {label}")
        temporary_identity = finished
        os.close(descriptor)
        descriptor = None

        current_payload = read_regular_bytes(
            repository,
            root,
            path,
            label,
            expected_status,
            expected_chain,
        )
        if current_payload != expected_payload:
            raise ValueError(f"Skill file changed before atomic replacement: {label}")
        ensure_safe_parent_chain(
            repository,
            root,
            path,
            expected_chain,
            expected_parents=parent_chain,
        )
        temporary_payload = read_regular_bytes(
            repository,
            root,
            temporary_path,
            f"temporary file for {label}",
            temporary_identity,
            expected_chain,
        )
        if temporary_payload != normalized:
            raise ValueError(f"temporary Skill file changed before replacement: {label}")

        os.replace(temporary_path, path)
        temporary_path = None
        best_effort_fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                ensure_safe_parent_chain(
                    repository,
                    root,
                    path,
                    expected_chain,
                    expected_parents=parent_chain,
                )
                try:
                    current_temporary = os.lstat(temporary_path)
                except FileNotFoundError:
                    current_temporary = None
                if current_temporary is not None:
                    reject_unsafe_status(
                        current_temporary, f"temporary file for {label}"
                    )
                    if temporary_identity is None or not directory_identity_matches(
                        temporary_identity, current_temporary
                    ):
                        raise ValueError(
                            f"temporary Skill file changed before cleanup: {label}"
                        )
                    try:
                        temporary_path.unlink()
                    except PermissionError:
                        os.chmod(temporary_path, stat.S_IREAD | stat.S_IWRITE)
                        temporary_path.unlink()
            except (OSError, ValueError):
                # Preserve the primary failure; never follow or delete a replaced path.
                pass


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
    files = iter_safe_files(repository, root, expected_chain)
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
                normalize_regular_file(
                    repository,
                    root,
                    path,
                    relative,
                    status,
                    payload,
                    normalized,
                    chain,
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
