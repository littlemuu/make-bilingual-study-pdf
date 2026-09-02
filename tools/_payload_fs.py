"""Shared fail-closed filesystem operations for repository payload tools."""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath


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
DirectoryChain = tuple[tuple[Path, os.stat_result], ...]


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
    status: os.stat_result,
    label: str,
    *,
    require_directory: bool = False,
    context: str = "payload",
    require_single_link: bool = True,
) -> None:
    link_kind = unsafe_link_kind(status)
    if link_kind:
        raise ValueError(f"{link_kind} are not allowed in {context}: {label}")
    if require_directory:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"{context} root must be a regular directory: {label}")
        return
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(
            f"non-regular filesystem entries are not allowed in {context}: {label}"
        )
    if require_single_link and status.st_nlink != 1:
        raise ValueError(f"multiply linked files are not allowed in {context}: {label}")


def metadata_matches(
    left: os.stat_result,
    right: os.stat_result,
    *,
    include_link_count: bool = True,
) -> bool:
    return (
        os.path.samestat(left, right)
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and (not include_link_count or left.st_nlink == right.st_nlink)
    )


def directory_identity_matches(left: os.stat_result, right: os.stat_result) -> bool:
    return os.path.samestat(left, right) and stat.S_IFMT(left.st_mode) == stat.S_IFMT(
        right.st_mode
    )


def lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving links or reparse points."""
    return Path(os.path.abspath(os.fspath(path)))


def repository_directory_paths(repository: Path) -> tuple[tuple[Path, str], ...]:
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


def repository_for_skill_root(
    root: Path,
    *,
    default_root: Path,
    default_repository: Path,
    skill_directory: str,
) -> Path:
    root = lexical_absolute(root)
    if root == lexical_absolute(default_root):
        return lexical_absolute(default_repository)
    if root.name != skill_directory or root.parent.name != "skills":
        raise ValueError(
            "custom Skill root must use the repository/skills/"
            f"{skill_directory} layout: {root}"
        )
    return root.parent.parent


def validate_skill_root_chain(
    repository: Path,
    root: Path,
    skill_directory: str,
    expected: DirectoryChain | None = None,
    *,
    context: str = "payload",
) -> DirectoryChain:
    repository = lexical_absolute(repository)
    root = lexical_absolute(root)
    skills = repository / "skills"
    expected_root = skills / skill_directory
    if root != expected_root:
        raise ValueError(f"Skill root must be the canonical subtree: {expected_root}")
    paths = repository_directory_paths(repository) + (
        (skills, "skills directory"),
        (root, "Skill root"),
    )
    if expected is not None and tuple(path for path, _ in expected) != tuple(
        path for path, _ in paths
    ):
        raise ValueError(f"{context} directory chain changed after validation")

    observed: list[tuple[Path, os.stat_result]] = []
    for index, (path, label) in enumerate(paths):
        status = os.lstat(path)
        reject_unsafe_status(
            status, label, require_directory=True, context=context
        )
        if expected is not None and not directory_identity_matches(
            expected[index][1], status
        ):
            raise ValueError(f"{context} directory changed after validation: {label}")
        observed.append((path, status))
    return tuple(observed)


def iter_safe_files(
    repository: Path,
    root: Path,
    skill_directory: str,
    expected_chain: DirectoryChain,
    *,
    context: str = "payload",
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    """Inventory the complete tree without following unsafe entries."""
    chain = validate_skill_root_chain(
        repository, root, skill_directory, expected_chain, context=context
    )
    files: list[tuple[Path, PurePosixPath, os.stat_result]] = []

    def visit(
        directory: Path,
        relative_directory: PurePosixPath,
        expected_status: os.stat_result,
    ) -> None:
        current_status = os.lstat(directory)
        label = relative_directory.as_posix() or str(root)
        reject_unsafe_status(
            current_status, label, require_directory=True, context=context
        )
        if not metadata_matches(expected_status, current_status):
            raise ValueError(f"{context} directory changed while traversing: {label}")
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            status = os.lstat(path)
            label = relative.as_posix()
            if stat.S_ISDIR(status.st_mode):
                reject_unsafe_status(
                    status, label, require_directory=True, context=context
                )
                visit(path, relative, status)
            else:
                reject_unsafe_status(status, label, context=context)
                files.append((path, relative, status))

    visit(root, PurePosixPath(), chain[-1][1])
    return files


def ensure_safe_parent_chain(
    repository: Path,
    root: Path,
    skill_directory: str,
    path: Path,
    expected_chain: DirectoryChain,
    expected_parents: DirectoryChain | None = None,
    *,
    context: str = "payload",
) -> DirectoryChain:
    chain = validate_skill_root_chain(
        repository, root, skill_directory, expected_chain, context=context
    )
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{context} path escapes root: {path}") from exc
    observed: list[tuple[Path, os.stat_result]] = list(chain)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        status = os.lstat(current)
        reject_unsafe_status(
            status,
            current.relative_to(root).as_posix(),
            require_directory=True,
            context=context,
        )
        observed.append((current, status))
    result = tuple(observed)
    if expected_parents is not None:
        if tuple(path for path, _ in expected_parents) != tuple(
            path for path, _ in result
        ):
            raise ValueError(f"{context} parent chain changed: {path}")
        for (parent, expected_status), (_, current_status) in zip(
            expected_parents, result
        ):
            if not directory_identity_matches(expected_status, current_status):
                raise ValueError(f"{context} parent directory changed: {parent}")
    return result


def open_read_descriptor(path: Path, *, writable: bool = False) -> int:
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
        access = 0x80000000 | (0x40000000 if writable else 0)
        handle = create_file(
            str(path), access, 0x00000001, None, 3, 0x00200000, None
        )
        if handle == ctypes.c_void_p(-1).value:
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
    skill_directory: str,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
    *,
    writable: bool = False,
    context: str = "payload",
) -> tuple[int, os.stat_result]:
    ensure_safe_parent_chain(
        repository, root, skill_directory, path, expected_chain, context=context
    )
    before = os.lstat(path)
    reject_unsafe_status(before, label, context=context)
    if not metadata_matches(expected_status, before):
        raise ValueError(f"{context} file changed after inventory: {label}")
    descriptor = open_read_descriptor(path, writable=writable)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        reject_unsafe_status(opened, label, context=context)
        reject_unsafe_status(after_open, label, context=context)
        if not (
            metadata_matches(before, opened)
            and metadata_matches(opened, after_open)
        ):
            raise ValueError(f"{context} file changed while opening: {label}")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def inspect_regular_file(
    repository: Path,
    root: Path,
    skill_directory: str,
    path: Path,
    label: str,
    expected_chain: DirectoryChain,
    *,
    allow_missing: bool = False,
    context: str = "payload",
) -> os.stat_result | None:
    ensure_safe_parent_chain(
        repository, root, skill_directory, path, expected_chain, context=context
    )
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    reject_unsafe_status(status, label, context=context)
    ensure_safe_parent_chain(
        repository, root, skill_directory, path, expected_chain, context=context
    )
    return status


def read_regular_bytes(
    repository: Path,
    root: Path,
    skill_directory: str,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
    *,
    context: str = "payload",
) -> bytes:
    descriptor, opened = open_regular_fd(
        repository,
        root,
        skill_directory,
        path,
        label,
        expected_status,
        expected_chain,
        context=context,
    )
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        finished = os.fstat(descriptor)
        final_path = os.lstat(path)
        reject_unsafe_status(finished, label, context=context)
        reject_unsafe_status(final_path, label, context=context)
        if not (
            metadata_matches(opened, finished)
            and metadata_matches(finished, final_path)
            and len(payload) == finished.st_size
        ):
            raise ValueError(f"{context} file changed while reading: {label}")
        return payload
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def hash_regular_file(
    repository: Path,
    root: Path,
    skill_directory: str,
    path: Path,
    label: str,
    expected_status: os.stat_result,
    expected_chain: DirectoryChain,
    *,
    context: str = "payload",
) -> tuple[int, str]:
    descriptor, opened = open_regular_fd(
        repository,
        root,
        skill_directory,
        path,
        label,
        expected_status,
        expected_chain,
        context=context,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
        finished = os.fstat(descriptor)
        final_path = os.lstat(path)
        reject_unsafe_status(finished, label, context=context)
        reject_unsafe_status(final_path, label, context=context)
        if not (
            metadata_matches(opened, finished)
            and metadata_matches(finished, final_path)
            and total == finished.st_size
        ):
            raise ValueError(f"{context} file changed while hashing: {label}")
        return total, digest.hexdigest()
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
            raise OSError("failed to write payload file")
        view = view[written:]


def best_effort_fsync_directory(directory: Path) -> None:
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


def atomic_replace_bytes(
    repository: Path,
    root: Path,
    skill_directory: str,
    path: Path,
    label: str,
    payload: bytes,
    expected_status: os.stat_result | None,
    expected_chain: DirectoryChain,
    *,
    expected_payload: bytes | None = None,
    mode: int | None = None,
    temporary_prefix: str | None = None,
    context: str = "payload",
) -> None:
    def verify_target_version() -> None:
        if expected_status is None:
            current = inspect_regular_file(
                repository,
                root,
                skill_directory,
                path,
                label,
                expected_chain,
                allow_missing=True,
                context=context,
            )
            if current is not None:
                raise ValueError(
                    f"{context} target appeared before replacement: {label}"
                )
            return
        current_payload = read_regular_bytes(
            repository,
            root,
            skill_directory,
            path,
            label,
            expected_status,
            expected_chain,
            context=context,
        )
        if expected_payload is not None and current_payload != expected_payload:
            raise ValueError(f"{context} file changed before replacement: {label}")

    parent_chain = ensure_safe_parent_chain(
        repository, root, skill_directory, path, expected_chain, context=context
    )
    temporary_path: Path | None = None
    temporary_identity: os.stat_result | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=temporary_prefix or f".{path.name}-", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        ensure_safe_parent_chain(
            repository,
            root,
            skill_directory,
            path,
            expected_chain,
            expected_parents=parent_chain,
            context=context,
        )
        temporary_identity = os.fstat(descriptor)
        temporary_path_status = os.lstat(temporary_path)
        reject_unsafe_status(
            temporary_identity, f"temporary file for {label}", context=context
        )
        reject_unsafe_status(
            temporary_path_status, f"temporary file for {label}", context=context
        )
        if not directory_identity_matches(temporary_identity, temporary_path_status):
            raise ValueError(f"temporary {context} file changed after creation: {label}")

        write_all(descriptor, payload)
        os.fsync(descriptor)
        if mode is not None:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            else:
                os.chmod(temporary_path, mode)
            os.fsync(descriptor)
        finished = os.fstat(descriptor)
        reject_unsafe_status(
            finished, f"temporary file for {label}", context=context
        )
        if (
            not directory_identity_matches(temporary_identity, finished)
            or finished.st_size != len(payload)
        ):
            raise ValueError(f"temporary {context} file changed while writing: {label}")
        temporary_identity = finished
        os.close(descriptor)
        descriptor = None

        verify_target_version()

        ensure_safe_parent_chain(
            repository,
            root,
            skill_directory,
            path,
            expected_chain,
            expected_parents=parent_chain,
            context=context,
        )
        temporary_payload = read_regular_bytes(
            repository,
            root,
            skill_directory,
            temporary_path,
            f"temporary file for {label}",
            temporary_identity,
            expected_chain,
            context=context,
        )
        if temporary_payload != payload:
            raise ValueError(f"temporary {context} file changed before replacement: {label}")
        ensure_safe_parent_chain(
            repository,
            root,
            skill_directory,
            path,
            expected_chain,
            expected_parents=parent_chain,
            context=context,
        )
        verify_target_version()
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
                    skill_directory,
                    path,
                    expected_chain,
                    expected_parents=parent_chain,
                    context=context,
                )
                try:
                    current_temporary = os.lstat(temporary_path)
                except FileNotFoundError:
                    current_temporary = None
                if current_temporary is not None:
                    reject_unsafe_status(
                        current_temporary,
                        f"temporary file for {label}",
                        context=context,
                    )
                    if temporary_identity is None or not directory_identity_matches(
                        temporary_identity, current_temporary
                    ):
                        raise ValueError(
                            f"temporary {context} file changed before cleanup: {label}"
                        )
                    try:
                        temporary_path.unlink()
                    except PermissionError:
                        os.chmod(temporary_path, stat.S_IREAD | stat.S_IWRITE)
                        temporary_path.unlink()
            except (OSError, ValueError):
                pass


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
    return "/".join(
        unicodedata.normalize("NFC", part).casefold() for part in path.parts
    )


def validate_directory_components(path: Path, label: str, failures: list[str]) -> bool:
    absolute = lexical_absolute(path)
    current = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            current /= part
            component = os.lstat(current)
            if unsafe_link_kind(component):
                failures.append(
                    f"{label} contains a symbolic link or reparse component: {current}"
                )
                return False
            if not stat.S_ISDIR(component.st_mode):
                failures.append(f"{label} component is not a directory: {current}")
                return False
    except OSError as exc:
        failures.append(f"cannot safely inspect {label}: {exc}")
        return False
    return True


def read_regular_utf8(path: Path, label: str, failures: list[str]) -> str | None:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if unsafe_link_kind(before):
            failures.append(f"{label} must not be a symbolic link or reparse point")
            return None
        if not stat.S_ISREG(before.st_mode):
            failures.append(f"{label} must be a regular file")
            return None
        descriptor = open_read_descriptor(path)
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or unsafe_link_kind(opened)
            or unsafe_link_kind(after_open)
            or not stat.S_ISREG(after_open.st_mode)
        ):
            failures.append(f"{label} must remain a regular non-reparse file")
            return None
        if not (
            os.path.samestat(before, opened)
            and os.path.samestat(opened, after_open)
        ) or not metadata_matches(before, opened, include_link_count=False):
            failures.append(f"{label} changed while it was being opened")
            return None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        os.lseek(descriptor, 0, os.SEEK_SET)
        second = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            second.extend(chunk)
        after_read = os.fstat(descriptor)
        final_path = os.lstat(path)
        if (
            payload != bytes(second)
            or unsafe_link_kind(final_path)
            or not stat.S_ISREG(final_path.st_mode)
            or not (
                os.path.samestat(opened, after_read)
                and os.path.samestat(after_read, final_path)
            )
            or not (
                metadata_matches(opened, after_read, include_link_count=False)
                and metadata_matches(after_read, final_path, include_link_count=False)
            )
        ):
            failures.append(f"{label} changed while it was being read")
            return None
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{label} is not valid UTF-8: {exc}")
            return None
    except OSError as exc:
        failures.append(f"cannot safely read {label}: {exc}")
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
