#!/usr/bin/env python3
"""Fail-closed filesystem operations for generated work artifacts.

Paths are normalized lexically with ``abspath`` and never resolved through a
filesystem link. Every existing ancestor is inspected with ``lstat`` before an
artifact is read, published, or removed. Destructive operations additionally
require an explicit lexical boundary.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable


WINDOWS_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
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


class ArtifactSafetyError(ValueError):
    """Raised when an artifact path or filesystem entry is unsafe."""


@dataclass(frozen=True)
class _EntrySnapshot:
    path: Path
    status: os.stat_result


@dataclass(frozen=True)
class ArtifactFileSnapshot:
    """Opaque file and ancestor identities captured for a later safe operation."""

    path: Path
    status: os.stat_result | None
    parents: tuple[_EntrySnapshot, ...]

    @property
    def exists(self) -> bool:
        return self.status is not None


def lexical_absolute_path(path: str | os.PathLike[str]) -> Path:
    """Return an expanded absolute path without following filesystem links."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def lexical_paths_overlap(
    left: str | os.PathLike[str], right: str | os.PathLike[str]
) -> bool:
    """Return whether either path lexically or physically contains the other.

    The physical fallback compares no-follow ``lstat`` snapshots. This catches
    alternate spellings that the filesystem maps to the same directory, such as
    case or NFC/NFD aliases on a default macOS APFS volume, without guessing that
    those spellings are aliases on case-sensitive filesystems.
    """
    absolute_left = lexical_absolute_path(left)
    absolute_right = lexical_absolute_path(right)
    left_parts = absolute_left.parts
    right_parts = absolute_right.parts
    if (
        left_parts == right_parts[: len(left_parts)]
        or right_parts == left_parts[: len(right_parts)]
    ):
        return True
    return _physical_paths_overlap(absolute_left, absolute_right)


def artifact_paths_same_entry(
    left: str | os.PathLike[str], right: str | os.PathLike[str]
) -> bool:
    """Compare exact path identity without following any filesystem link."""
    absolute_left = lexical_absolute_path(left)
    absolute_right = lexical_absolute_path(right)
    left_snapshots, left_complete = _inspect_existing_path_components(absolute_left)
    if absolute_left == absolute_right:
        _recheck_entry_snapshots(left_snapshots)
        return bool(left_complete and left_snapshots)
    right_snapshots, right_complete = _inspect_existing_path_components(absolute_right)
    same = bool(
        left_complete
        and right_complete
        and left_snapshots
        and right_snapshots
        and _same_entry_identity(
            left_snapshots[-1].status, right_snapshots[-1].status
        )
    )
    _recheck_entry_snapshots(left_snapshots)
    _recheck_entry_snapshots(right_snapshots)
    return same


def work_relative_artifact_path(
    work_dir: str | os.PathLike[str], value: object, *, label: str = "artifact path"
) -> Path:
    """Build a lexical WORK-relative path without resolving filesystem links."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ArtifactSafetyError(f"{label} must be a nonempty work-relative path")
    if "\\" in value or value.startswith("/") or PureWindowsPath(value).drive:
        raise ArtifactSafetyError(f"{label} must use a portable work-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactSafetyError(f"{label} contains an unsafe path component")
    for part in parts:
        if part.endswith((".", " ")):
            raise ArtifactSafetyError(
                f"{label} contains a Windows-trimmed path component"
            )
        if any(ord(character) < 32 for character in part) or any(
            character in WINDOWS_FORBIDDEN_CHARACTERS for character in part
        ):
            raise ArtifactSafetyError(
                f"{label} contains a Windows-forbidden path component"
            )
        windows_stem = part.split(".", 1)[0].rstrip(" ").casefold()
        if windows_stem in WINDOWS_RESERVED_STEMS:
            raise ArtifactSafetyError(
                f"{label} contains a Windows-reserved path component"
            )
    boundary = lexical_absolute_path(work_dir)
    candidate, _ = _bounded_path(boundary.joinpath(*parts), boundary)
    return candidate


def portable_artifact_basename(value: object, *, label: str = "basename") -> str:
    """Validate one portable filename component without touching the filesystem."""
    boundary = lexical_absolute_path(os.curdir)
    candidate = work_relative_artifact_path(boundary, value, label=label)
    if candidate.parent != boundary:
        raise ArtifactSafetyError(f"{label} must be a single filename component")
    assert isinstance(value, str)
    return value


def _path_components(path: Path) -> tuple[Path, ...]:
    parts = path.parts
    return tuple(Path(*parts[:index]) for index in range(1, len(parts) + 1))


def _is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & WINDOWS_REPARSE_ATTRIBUTE
        or getattr(status, "st_reparse_tag", 0)
    )


def _reject_link_or_reparse(status: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(status.st_mode):
        raise ArtifactSafetyError(f"symbolic links are not allowed: {path}")
    if _is_reparse_point(status):
        raise ArtifactSafetyError(f"reparse points are not allowed: {path}")


def _require_directory(status: os.stat_result, path: Path) -> None:
    _reject_link_or_reparse(status, path)
    if not stat.S_ISDIR(status.st_mode):
        raise ArtifactSafetyError(f"artifact path must be a directory: {path}")


def _require_regular_file(status: os.stat_result, path: Path) -> None:
    _reject_link_or_reparse(status, path)
    if not stat.S_ISREG(status.st_mode):
        raise ArtifactSafetyError(f"artifact path must be a regular file: {path}")
    if status.st_nlink != 1:
        raise ArtifactSafetyError(f"hard-linked artifact files are not allowed: {path}")


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(
        right.st_mode
    )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_nlink == right.st_nlink
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and left.st_nlink == right.st_nlink
    )


def _same_entry_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(
        right.st_mode
    )


def _inspect_existing_path_components(
    path: Path,
) -> tuple[tuple[_EntrySnapshot, ...], bool]:
    components = _path_components(path)
    snapshots: list[_EntrySnapshot] = []
    for index, component in enumerate(components):
        try:
            status = os.lstat(component)
        except FileNotFoundError:
            return tuple(snapshots), False
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot inspect artifact path identity: {exc}"
            ) from exc
        _reject_link_or_reparse(status, component)
        if index < len(components) - 1:
            _require_directory(status, component)
        snapshots.append(_EntrySnapshot(component, status))
    return tuple(snapshots), True


def _recheck_entry_snapshots(snapshots: tuple[_EntrySnapshot, ...]) -> None:
    for snapshot in snapshots:
        try:
            current = os.lstat(snapshot.path)
            _reject_link_or_reparse(current, snapshot.path)
        except (OSError, ArtifactSafetyError) as exc:
            if isinstance(exc, ArtifactSafetyError):
                raise
            raise ArtifactSafetyError(
                f"artifact path identity changed during overlap check: {exc}"
            ) from exc
        if not _same_entry_identity(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact path identity changed during overlap check: {snapshot.path}"
            )


def _physical_paths_overlap(left: Path, right: Path) -> bool:
    left_snapshots, left_complete = _inspect_existing_path_components(left)
    right_snapshots, right_complete = _inspect_existing_path_components(right)
    overlap = False

    left_endpoint = left_snapshots[-1] if left_complete and left_snapshots else None
    right_endpoint = right_snapshots[-1] if right_complete and right_snapshots else None
    if left_endpoint is not None and right_endpoint is not None:
        overlap = _same_entry_identity(
            left_endpoint.status, right_endpoint.status
        )
    if (
        not overlap
        and left_endpoint is not None
        and stat.S_ISDIR(left_endpoint.status.st_mode)
    ):
        overlap = any(
            stat.S_ISDIR(snapshot.status.st_mode)
            and _same_directory(left_endpoint.status, snapshot.status)
            for snapshot in right_snapshots
        )
    if (
        not overlap
        and right_endpoint is not None
        and stat.S_ISDIR(right_endpoint.status.st_mode)
    ):
        overlap = any(
            stat.S_ISDIR(snapshot.status.st_mode)
            and _same_directory(right_endpoint.status, snapshot.status)
            for snapshot in left_snapshots
        )

    _recheck_entry_snapshots(left_snapshots)
    _recheck_entry_snapshots(right_snapshots)
    return overlap


def _bounded_path(
    path: str | os.PathLike[str],
    boundary: str | os.PathLike[str] | None,
    *,
    allow_boundary: bool = True,
) -> tuple[Path, Path | None]:
    absolute = lexical_absolute_path(path)
    if boundary is None:
        return absolute, None
    absolute_boundary = lexical_absolute_path(boundary)
    try:
        common = Path(
            os.path.commonpath((os.fspath(absolute_boundary), os.fspath(absolute)))
        )
    except ValueError as exc:
        raise ArtifactSafetyError(
            f"artifact path is outside its boundary: {absolute}"
        ) from exc
    if os.path.normcase(os.fspath(common)) != os.path.normcase(
        os.fspath(absolute_boundary)
    ):
        raise ArtifactSafetyError(
            f"artifact path is outside its boundary: {absolute}"
        )
    if not allow_boundary and os.path.normcase(os.fspath(absolute)) == os.path.normcase(
        os.fspath(absolute_boundary)
    ):
        raise ArtifactSafetyError("the artifact boundary itself may not be removed")
    return absolute, absolute_boundary


def _inspect_directory(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
) -> tuple[Path, tuple[_EntrySnapshot, ...]]:
    absolute, _ = _bounded_path(path, boundary)
    identities: list[_EntrySnapshot] = []
    try:
        for component in _path_components(absolute):
            status = os.lstat(component)
            _require_directory(status, component)
            identities.append(_EntrySnapshot(component, status))
    except (OSError, ArtifactSafetyError) as exc:
        if isinstance(exc, ArtifactSafetyError):
            raise
        raise ArtifactSafetyError(f"cannot inspect artifact directory: {exc}") from exc
    return absolute, tuple(identities)


def _recheck_directories(identities: tuple[_EntrySnapshot, ...]) -> None:
    for snapshot in identities:
        try:
            current = os.lstat(snapshot.path)
            _require_directory(current, snapshot.path)
        except (OSError, ArtifactSafetyError) as exc:
            if isinstance(exc, ArtifactSafetyError):
                raise
            raise ArtifactSafetyError(
                f"artifact directory changed during operation: {exc}"
            ) from exc
        if not _same_directory(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact directory identity changed: {snapshot.path}"
            )


def validate_artifact_directory(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
) -> Path:
    """Validate an existing directory and every lexical ancestor."""
    absolute, identities = _inspect_directory(path, boundary=boundary)
    _recheck_directories(identities)
    return absolute


def prepare_artifact_directory(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    mode: int = 0o700,
) -> Path:
    """Create a directory path without following links in any component."""
    absolute, _ = _bounded_path(path, boundary)
    identities: list[_EntrySnapshot] = []
    for component in _path_components(absolute):
        try:
            status = os.lstat(component)
        except FileNotFoundError:
            _recheck_directories(tuple(identities))
            try:
                os.mkdir(component, mode)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ArtifactSafetyError(
                    f"cannot create artifact directory safely: {exc}"
                ) from exc
            try:
                status = os.lstat(component)
            except OSError as exc:
                raise ArtifactSafetyError(
                    f"cannot inspect created artifact directory: {exc}"
                ) from exc
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot inspect artifact directory safely: {exc}"
            ) from exc
        _require_directory(status, component)
        identities.append(_EntrySnapshot(component, status))
        _recheck_directories(tuple(identities))
    return absolute


def _artifact_file_status(path: Path) -> os.stat_result | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot inspect artifact file: {exc}") from exc
    _require_regular_file(status, path)
    return status


def _inspect_file(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None,
    allow_missing: bool,
) -> tuple[Path, os.stat_result | None, tuple[_EntrySnapshot, ...]]:
    absolute, _ = _bounded_path(path, boundary)
    _, parent_identities = _inspect_directory(absolute.parent, boundary=boundary)
    status = _artifact_file_status(absolute)
    if status is None and not allow_missing:
        raise ArtifactSafetyError(f"artifact file does not exist: {absolute}")
    _recheck_directories(parent_identities)
    return absolute, status, parent_identities


def validate_artifact_file(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    allow_missing: bool = False,
) -> Path:
    """Validate a regular, singly linked file and all parent directories."""
    absolute, _, _ = _inspect_file(
        path, boundary=boundary, allow_missing=allow_missing
    )
    return absolute


def inspect_artifact_file(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    allow_missing: bool = False,
) -> ArtifactFileSnapshot:
    """Capture a regular file and its ancestors for a later read or publication."""
    absolute, status, parents = _inspect_file(
        path, boundary=boundary, allow_missing=allow_missing
    )
    return ArtifactFileSnapshot(absolute, status, parents)


def _expected_file_snapshot(
    path: str | os.PathLike[str],
    boundary: str | os.PathLike[str] | None,
    expected: ArtifactFileSnapshot,
) -> tuple[Path, os.stat_result | None, tuple[_EntrySnapshot, ...]]:
    if not isinstance(expected, ArtifactFileSnapshot):
        raise TypeError("expected artifact version must be an ArtifactFileSnapshot")
    absolute, _ = _bounded_path(path, boundary, allow_boundary=False)
    if absolute != expected.path:
        raise ArtifactSafetyError("artifact snapshot belongs to a different path")
    _recheck_directories(expected.parents)
    current = _artifact_file_status(absolute)
    if expected.status is None:
        if current is not None:
            raise ArtifactSafetyError("artifact target appeared after inspection")
    elif current is None or not _same_file(expected.status, current):
        raise ArtifactSafetyError("artifact target changed after inspection")
    _recheck_directories(expected.parents)
    return absolute, expected.status, expected.parents


def _open_readonly_no_follow(path: Path) -> int:
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
        handle = create_file(
            str(path),
            0x80000000,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def _consume_artifact_chunks(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    expected: ArtifactFileSnapshot | None = None,
    consume: Callable[[bytes], None],
) -> int:
    """Feed stable file chunks to an internal consumer and return total bytes."""
    if expected is None:
        absolute, expected_status, parent_identities = _inspect_file(
            path, boundary=boundary, allow_missing=False
        )
    else:
        absolute, expected_status, parent_identities = _expected_file_snapshot(
            path, boundary, expected
        )
        if expected_status is None:
            raise ArtifactSafetyError(f"artifact file does not exist: {absolute}")
    assert expected_status is not None
    try:
        descriptor = _open_readonly_no_follow(absolute)
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot open artifact file safely: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        _require_regular_file(opened, absolute)
        after_open = _artifact_file_status(absolute)
        if after_open is None or not (
            _same_file(expected_status, opened) and _same_file(opened, after_open)
        ):
            raise ArtifactSafetyError("artifact file changed while opening")
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            consume(chunk)
            size += len(chunk)
        finished = os.fstat(descriptor)
        after_read = _artifact_file_status(absolute)
        if after_read is None or not (
            _same_file(opened, finished) and _same_file(finished, after_read)
        ):
            raise ArtifactSafetyError("artifact file changed while reading")
        _recheck_directories(parent_identities)
        return size
    finally:
        os.close(descriptor)


def read_artifact_bytes(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    expected: ArtifactFileSnapshot | None = None,
) -> bytes:
    """Read a validated file while detecting entry replacement during the read."""
    chunks: list[bytes] = []
    _consume_artifact_chunks(
        path, boundary=boundary, expected=expected, consume=chunks.append
    )
    return b"".join(chunks)


def read_artifact_text(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
    encoding: str = "utf-8",
    expected: ArtifactFileSnapshot | None = None,
) -> str:
    """Read and decode a validated artifact file."""
    try:
        return read_artifact_bytes(
            path, boundary=boundary, expected=expected
        ).decode(encoding)
    except UnicodeError as exc:
        raise ArtifactSafetyError(
            f"artifact file is not valid {encoding}: {exc}"
        ) from exc


def artifact_size(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
) -> int:
    """Return the byte size observed through a validated descriptor read."""
    return _consume_artifact_chunks(
        path, boundary=boundary, consume=lambda _chunk: None
    )


def sha256_artifact(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str] | None = None,
) -> str:
    """Hash a validated artifact without loading the complete file in memory."""
    digest = hashlib.sha256()
    _consume_artifact_chunks(path, boundary=boundary, consume=digest.update)
    return digest.hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("artifact write made no progress")
        remaining = remaining[written:]


def _fsync_directory(path: Path, expected: os.stat_result) -> None:
    if os.name == "nt":
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        _require_directory(opened, path)
        if not _same_directory(expected, opened):
            raise ArtifactSafetyError(
                f"artifact directory changed before fsync: {path}"
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _prepare_publication(
    path: str | os.PathLike[str],
    boundary: str | os.PathLike[str],
    expected: ArtifactFileSnapshot | None = None,
) -> tuple[Path, os.stat_result | None, tuple[_EntrySnapshot, ...]]:
    if expected is not None:
        return _expected_file_snapshot(path, boundary, expected)
    absolute, _ = _bounded_path(path, boundary, allow_boundary=False)
    _, parent_identities = _inspect_directory(absolute.parent, boundary=boundary)
    expected_target = _artifact_file_status(absolute)
    _recheck_directories(parent_identities)
    return absolute, expected_target, parent_identities


def _create_temporary(
    target: Path, *, preserve_suffix: bool
) -> tuple[int, Path, os.stat_result]:
    suffix = "".join(target.suffixes) if preserve_suffix else ".tmp"
    if not suffix:
        suffix = ".tmp"
    base_name = (
        target.name[: -len(suffix)]
        if target.name.endswith(suffix)
        else target.name
    )
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{base_name or 'artifact'}-", suffix=suffix, dir=target.parent
    )
    temporary = Path(raw_path)
    initial: os.stat_result | None = None
    try:
        initial = os.fstat(descriptor)
        _require_regular_file(initial, temporary)
        return descriptor, temporary, initial
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if initial is not None:
            try:
                observed = os.lstat(temporary)
                if os.path.samestat(initial, observed):
                    os.unlink(temporary)
            except OSError:
                pass
        raise


def _finish_temporary(
    descriptor: int,
    temporary: Path,
    initial: os.stat_result,
    *,
    expected_size: int | None,
) -> os.stat_result:
    os.fsync(descriptor)
    finished = os.fstat(descriptor)
    _require_regular_file(finished, temporary)
    if not _same_file_identity(initial, finished):
        raise ArtifactSafetyError("temporary artifact changed while writing")
    if expected_size is not None and finished.st_size != expected_size:
        raise ArtifactSafetyError("temporary artifact size changed while writing")
    return finished


def _publish_temporary(
    temporary: Path,
    finished: os.stat_result,
    target: Path,
    expected_target: os.stat_result | None,
    parent_identities: tuple[_EntrySnapshot, ...],
) -> None:
    current_temporary = _artifact_file_status(temporary)
    if current_temporary is None or not _same_file(finished, current_temporary):
        raise ArtifactSafetyError("temporary artifact changed before publication")
    _recheck_directories(parent_identities)
    current_target = _artifact_file_status(target)
    if expected_target is None:
        if current_target is not None:
            raise ArtifactSafetyError("artifact target appeared during publication")
    elif current_target is None or not _same_file(expected_target, current_target):
        raise ArtifactSafetyError("artifact target changed before publication")

    os.replace(temporary, target)
    published = _artifact_file_status(target)
    if (
        published is None
        or published.st_size != finished.st_size
        or not _same_file_identity(finished, published)
    ):
        raise ArtifactSafetyError(
            "published artifact identity does not match temporary file"
        )
    _fsync_directory(target.parent, parent_identities[-1].status)
    _recheck_directories(parent_identities)


def _cleanup_temporary(temporary: Path | None, initial: os.stat_result | None) -> None:
    if temporary is None or initial is None:
        return
    try:
        current = os.lstat(temporary)
        if _same_file_identity(initial, current):
            os.unlink(temporary)
    except OSError:
        pass


def atomic_write_bytes(
    path: str | os.PathLike[str],
    payload: bytes | bytearray | memoryview,
    *,
    boundary: str | os.PathLike[str],
    expected: ArtifactFileSnapshot | None = None,
) -> Path:
    """Fsync a same-directory temporary file and atomically replace a target."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("artifact payload must be bytes-like")
    data = bytes(payload)
    absolute, expected_target, parent_identities = _prepare_publication(
        path, boundary, expected
    )

    descriptor = -1
    temporary: Path | None = None
    temporary_status: os.stat_result | None = None
    try:
        descriptor, temporary, temporary_status = _create_temporary(
            absolute, preserve_suffix=False
        )
        _write_all(descriptor, data)
        finished = _finish_temporary(
            descriptor,
            temporary,
            temporary_status,
            expected_size=len(data),
        )
        os.close(descriptor)
        descriptor = -1
        _publish_temporary(
            temporary,
            finished,
            absolute,
            expected_target,
            parent_identities,
        )
        temporary = None
        return absolute
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot publish artifact safely: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _cleanup_temporary(temporary, temporary_status)


def _open_readwrite_no_follow(path: Path) -> int:
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
        handle = create_file(
            str(path),
            0xC0000000,
            0x00000001,
            None,
            3,
            0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDWR
    for optional_flag in ("O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def atomic_publish_with_writer(
    path: str | os.PathLike[str],
    writer: Callable[[Path], object],
    *,
    boundary: str | os.PathLike[str],
) -> Path:
    """Publish callback output through a temp path retaining the target suffix."""
    if not callable(writer):
        raise TypeError("artifact writer must be callable")
    absolute, expected_target, parent_identities = _prepare_publication(
        path, boundary
    )
    descriptor = -1
    temporary: Path | None = None
    temporary_status: os.stat_result | None = None
    try:
        descriptor, temporary, temporary_status = _create_temporary(
            absolute, preserve_suffix=True
        )
        os.close(descriptor)
        descriptor = -1
        writer(temporary)

        current = _artifact_file_status(temporary)
        if current is None or not _same_file_identity(temporary_status, current):
            raise ArtifactSafetyError("artifact writer replaced its temporary file")
        descriptor = _open_readwrite_no_follow(temporary)
        opened = os.fstat(descriptor)
        _require_regular_file(opened, temporary)
        if not _same_file(current, opened):
            raise ArtifactSafetyError("temporary artifact changed while reopening")
        finished = _finish_temporary(
            descriptor, temporary, temporary_status, expected_size=None
        )
        after_fsync = _artifact_file_status(temporary)
        if after_fsync is None or not _same_file(finished, after_fsync):
            raise ArtifactSafetyError("temporary artifact changed after fsync")
        os.close(descriptor)
        descriptor = -1
        _publish_temporary(
            temporary,
            finished,
            absolute,
            expected_target,
            parent_identities,
        )
        temporary = None
        return absolute
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot publish artifact safely: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _cleanup_temporary(temporary, temporary_status)


def atomic_copy_file(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str],
    source_boundary: str | os.PathLike[str] | None = None,
) -> Path:
    """Stream a validated source into a same-directory temp and publish it."""
    source_path, expected_source, source_parents = _inspect_file(
        source, boundary=source_boundary, allow_missing=False
    )
    assert expected_source is not None
    target_path, expected_target, target_parents = _prepare_publication(
        target, boundary
    )
    source_descriptor = -1
    target_descriptor = -1
    temporary: Path | None = None
    temporary_status: os.stat_result | None = None
    try:
        source_descriptor = _open_readonly_no_follow(source_path)
        opened_source = os.fstat(source_descriptor)
        _require_regular_file(opened_source, source_path)
        after_open = _artifact_file_status(source_path)
        if after_open is None or not (
            _same_file(expected_source, opened_source)
            and _same_file(opened_source, after_open)
        ):
            raise ArtifactSafetyError("source artifact changed while opening")

        target_descriptor, temporary, temporary_status = _create_temporary(
            target_path, preserve_suffix=False
        )
        copied = 0
        while chunk := os.read(source_descriptor, 1024 * 1024):
            _write_all(target_descriptor, chunk)
            copied += len(chunk)
        finished_source = os.fstat(source_descriptor)
        after_read = _artifact_file_status(source_path)
        if after_read is None or not (
            _same_file(opened_source, finished_source)
            and _same_file(finished_source, after_read)
        ):
            raise ArtifactSafetyError("source artifact changed while copying")
        _recheck_directories(source_parents)
        os.close(source_descriptor)
        source_descriptor = -1

        finished_target = _finish_temporary(
            target_descriptor,
            temporary,
            temporary_status,
            expected_size=copied,
        )
        os.close(target_descriptor)
        target_descriptor = -1
        _publish_temporary(
            temporary,
            finished_target,
            target_path,
            expected_target,
            target_parents,
        )
        temporary = None
        return target_path
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot copy artifact safely: {exc}") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if target_descriptor >= 0:
            os.close(target_descriptor)
        _cleanup_temporary(temporary, temporary_status)


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    boundary: str | os.PathLike[str],
    encoding: str = "utf-8",
) -> Path:
    """Encode text and atomically publish it as an artifact."""
    if not isinstance(text, str):
        raise TypeError("artifact text must be a string")
    return atomic_write_bytes(path, text.encode(encoding), boundary=boundary)


def remove_artifact_file(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str],
    missing_ok: bool = True,
) -> None:
    """Remove one validated regular file without following directory entries."""
    absolute, _ = _bounded_path(path, boundary, allow_boundary=False)
    _, parent_identities = _inspect_directory(absolute.parent, boundary=boundary)
    expected = _artifact_file_status(absolute)
    if expected is None:
        if missing_ok:
            return
        raise ArtifactSafetyError(f"artifact file does not exist: {absolute}")
    _recheck_directories(parent_identities)
    current = _artifact_file_status(absolute)
    if current is None or not _same_file(expected, current):
        raise ArtifactSafetyError("artifact file changed before removal")
    try:
        os.unlink(absolute)
        _fsync_directory(absolute.parent, parent_identities[-1].status)
    except OSError as exc:
        raise ArtifactSafetyError(f"cannot remove artifact file safely: {exc}") from exc
    _recheck_directories(parent_identities)


def _snapshot_tree(
    root: Path,
    root_identities: tuple[_EntrySnapshot, ...],
) -> tuple[
    list[_EntrySnapshot],
    list[_EntrySnapshot],
    dict[Path, os.stat_result],
]:
    files: list[_EntrySnapshot] = []
    directories: list[_EntrySnapshot] = []
    directory_identities = {root: root_identities[-1].status}
    root_device = root_identities[-1].status.st_dev

    def visit(directory: Path, expected: os.stat_result) -> None:
        before = os.lstat(directory)
        _require_directory(before, directory)
        if not _same_directory(expected, before):
            raise ArtifactSafetyError(
                f"artifact directory changed while scanning: {directory}"
            )
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot scan artifact directory safely: {exc}"
            ) from exc
        after_scan = os.lstat(directory)
        _require_directory(after_scan, directory)
        if not _same_directory(before, after_scan):
            raise ArtifactSafetyError(
                f"artifact directory changed while scanning: {directory}"
            )

        for entry in entries:
            path = Path(entry.path)
            try:
                status = os.lstat(path)
            except OSError as exc:
                raise ArtifactSafetyError(
                    f"cannot inspect artifact tree entry: {exc}"
                ) from exc
            _reject_link_or_reparse(status, path)
            if stat.S_ISDIR(status.st_mode):
                if status.st_dev != root_device:
                    raise ArtifactSafetyError(
                        f"cross-device artifact directories are not allowed: {path}"
                    )
                directory_identities[path] = status
                visit(path, status)
                directories.append(_EntrySnapshot(path, status))
            else:
                _require_regular_file(status, path)
                files.append(_EntrySnapshot(path, status))

        finished = os.lstat(directory)
        _require_directory(finished, directory)
        if not _same_directory(before, finished):
            raise ArtifactSafetyError(
                f"artifact directory changed while scanning: {directory}"
            )

    visit(root, root_identities[-1].status)
    _recheck_directories(root_identities)
    return files, directories, directory_identities


def _recheck_tree_parent(
    root: Path,
    parent: Path,
    directory_identities: dict[Path, os.stat_result],
) -> None:
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise ArtifactSafetyError(
            f"artifact tree path escaped its root: {parent}"
        ) from exc
    current = root
    components = (current,)
    for part in relative.parts:
        current = current / part
        components += (current,)
    for component in components:
        expected = directory_identities.get(component)
        if expected is None:
            raise ArtifactSafetyError(
                f"artifact directory was not included in preflight: {component}"
            )
        try:
            observed = os.lstat(component)
            _require_directory(observed, component)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"artifact directory changed before removal: {exc}"
            ) from exc
        if not _same_directory(expected, observed):
            raise ArtifactSafetyError(
                f"artifact directory identity changed before removal: {component}"
            )


def _recheck_tree_snapshot(
    root: Path,
    root_identities: tuple[_EntrySnapshot, ...],
    files: list[_EntrySnapshot],
    directories: list[_EntrySnapshot],
    directory_identities: dict[Path, os.stat_result],
) -> None:
    for snapshot in files:
        _recheck_tree_parent(root, snapshot.path.parent, directory_identities)
        current = _artifact_file_status(snapshot.path)
        if current is None or not _same_file(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact file changed after preflight: {snapshot.path}"
            )

    for snapshot in directories:
        _recheck_tree_parent(root, snapshot.path.parent, directory_identities)
        try:
            current = os.lstat(snapshot.path)
            _require_directory(current, snapshot.path)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"artifact directory changed after preflight: {exc}"
            ) from exc
        if not _same_directory(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact directory changed after preflight: {snapshot.path}"
            )

    _recheck_directories(root_identities)


def validate_artifact_tree(
    path: str | os.PathLike[str],
    boundary: str | os.PathLike[str],
    allow_missing: bool = True,
) -> Path | None:
    """Preflight an entire generated tree without mutating any entry."""
    absolute, absolute_boundary = _bounded_path(path, boundary)
    assert absolute_boundary is not None
    _, parent_identities = _inspect_directory(absolute.parent)
    try:
        root_status = os.lstat(absolute)
    except FileNotFoundError:
        _recheck_directories(parent_identities)
        if allow_missing:
            return None
        raise ArtifactSafetyError(f"artifact directory does not exist: {absolute}")
    except OSError as exc:
        raise ArtifactSafetyError(
            f"cannot inspect artifact directory safely: {exc}"
        ) from exc
    _require_directory(root_status, absolute)
    _recheck_directories(parent_identities)

    absolute, root_identities = _inspect_directory(
        absolute, boundary=absolute_boundary
    )
    files, directories, directory_identities = _snapshot_tree(
        absolute, root_identities
    )
    _recheck_tree_snapshot(
        absolute,
        root_identities,
        files,
        directories,
        directory_identities,
    )
    return absolute


def clear_artifact_directory(
    path: str | os.PathLike[str],
    *,
    boundary: str | os.PathLike[str],
    remove_directory: bool = False,
    missing_ok: bool = True,
) -> None:
    """Preflight and clear a generated tree without crossing unsafe entries."""
    absolute, absolute_boundary = _bounded_path(
        path, boundary, allow_boundary=not remove_directory
    )
    assert absolute_boundary is not None
    _, parent_identities = _inspect_directory(absolute.parent)
    try:
        root_status = os.lstat(absolute)
    except FileNotFoundError:
        if missing_ok:
            return
        raise ArtifactSafetyError(f"artifact directory does not exist: {absolute}")
    except OSError as exc:
        raise ArtifactSafetyError(
            f"cannot inspect artifact directory safely: {exc}"
        ) from exc
    _require_directory(root_status, absolute)
    _recheck_directories(parent_identities)
    absolute, identities = _inspect_directory(absolute, boundary=absolute_boundary)

    files, directories, directory_identities = _snapshot_tree(absolute, identities)
    _recheck_tree_snapshot(
        absolute,
        identities,
        files,
        directories,
        directory_identities,
    )
    for snapshot in files:
        _recheck_tree_parent(
            absolute, snapshot.path.parent, directory_identities
        )
        current = _artifact_file_status(snapshot.path)
        if current is None or not _same_file(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact file changed before removal: {snapshot.path}"
            )
        _recheck_tree_parent(
            absolute, snapshot.path.parent, directory_identities
        )
        _recheck_directories(identities)
        try:
            os.unlink(snapshot.path)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot remove artifact file safely: {exc}"
            ) from exc

    for snapshot in directories:
        _recheck_tree_parent(
            absolute, snapshot.path.parent, directory_identities
        )
        current = os.lstat(snapshot.path)
        _require_directory(current, snapshot.path)
        if not _same_directory(snapshot.status, current):
            raise ArtifactSafetyError(
                f"artifact directory changed before removal: {snapshot.path}"
            )
        _recheck_tree_parent(
            absolute, snapshot.path.parent, directory_identities
        )
        _recheck_directories(identities)
        try:
            os.rmdir(snapshot.path)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot remove artifact directory safely: {exc}"
            ) from exc

    if remove_directory:
        root_status = os.lstat(absolute)
        _require_directory(root_status, absolute)
        if not _same_directory(identities[-1].status, root_status):
            raise ArtifactSafetyError("artifact root changed before removal")
        _recheck_directories(identities)
        try:
            os.rmdir(absolute)
            _fsync_directory(absolute.parent, identities[-2].status)
        except OSError as exc:
            raise ArtifactSafetyError(
                f"cannot remove artifact root safely: {exc}"
            ) from exc
        _recheck_directories(identities[:-1])
    else:
        _fsync_directory(absolute, identities[-1].status)
        _recheck_directories(identities)


__all__ = [
    "ArtifactSafetyError",
    "artifact_paths_same_entry",
    "artifact_size",
    "atomic_copy_file",
    "atomic_publish_with_writer",
    "atomic_write_bytes",
    "atomic_write_text",
    "clear_artifact_directory",
    "lexical_absolute_path",
    "lexical_paths_overlap",
    "prepare_artifact_directory",
    "portable_artifact_basename",
    "read_artifact_bytes",
    "read_artifact_text",
    "remove_artifact_file",
    "sha256_artifact",
    "validate_artifact_directory",
    "validate_artifact_file",
    "validate_artifact_tree",
    "work_relative_artifact_path",
]
