#!/usr/bin/env python3
"""Validate repository-only release documentation against the Skill version."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
from pathlib import Path


DEFAULT_REPOSITORY_ROOT = Path(os.path.abspath(__file__)).parent.parent
REPOSITORY = "littlemuu/make-bilingual-study-pdf"
INSTALL_PATH = "skills/make-bilingual-study-pdf"

NUMERIC_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
NON_NUMERIC_IDENTIFIER = r"(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
PRERELEASE_IDENTIFIER = rf"(?:{NUMERIC_IDENTIFIER}|{NON_NUMERIC_IDENTIFIER})"
SEMVER_PATTERN = (
    rf"{NUMERIC_IDENTIFIER}\.{NUMERIC_IDENTIFIER}\.{NUMERIC_IDENTIFIER}"
    rf"(?:-{PRERELEASE_IDENTIFIER}(?:\.{PRERELEASE_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
SEMVER_RE = re.compile(rf"^{SEMVER_PATTERN}$")
FENCE_OPEN_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$"
)


def is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_attribute)


def file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def file_snapshot(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def open_read_descriptor(path: Path) -> int:
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
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise

    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NONBLOCK", "O_NOFOLLOW"):
        flags |= getattr(os, optional_flag, 0)
    return os.open(path, flags)


def validate_directory_components(
    path: Path, label: str, failures: list[str]
) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            current /= part
            component = current.lstat()
            if stat.S_ISLNK(component.st_mode) or is_reparse_point(component):
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


def read_regular_utf8(
    path: Path, label: str, failures: list[str]
) -> str | None:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or is_reparse_point(before):
            failures.append(f"{label} must not be a symbolic link or reparse point")
            return None
        if not stat.S_ISREG(before.st_mode):
            failures.append(f"{label} must be a regular file")
            return None

        descriptor = open_read_descriptor(path)
        opened = os.fstat(descriptor)
        after_open = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or is_reparse_point(opened)
            or stat.S_ISLNK(after_open.st_mode)
            or is_reparse_point(after_open)
            or not stat.S_ISREG(after_open.st_mode)
        ):
            failures.append(f"{label} must remain a regular non-reparse file")
            return None
        if not (
            file_identity(before)
            == file_identity(opened)
            == file_identity(after_open)
        ):
            failures.append(f"{label} changed while it was being opened")
            return None
        if file_snapshot(before) != file_snapshot(opened):
            failures.append(f"{label} changed while it was being opened")
            return None

        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        payload = b"".join(chunks)
        os.lseek(descriptor, 0, os.SEEK_SET)
        second_chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            second_chunks.append(chunk)
        if payload != b"".join(second_chunks):
            failures.append(f"{label} changed while it was being read")
            return None

        after_read = os.fstat(descriptor)
        final_path = path.lstat()
        if (
            stat.S_ISLNK(final_path.st_mode)
            or is_reparse_point(final_path)
            or not stat.S_ISREG(final_path.st_mode)
            or not (
                file_identity(opened)
                == file_identity(after_read)
                == file_identity(final_path)
            )
            or not (
                file_snapshot(opened)
                == file_snapshot(after_read)
                == file_snapshot(final_path)
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


def opening_fence(line: str) -> tuple[str, int] | None:
    match = FENCE_OPEN_RE.fullmatch(line)
    if match is None:
        return None
    fence = match.group("fence")
    if fence.startswith("`") and "`" in match.group("info"):
        return None
    return fence[0], len(fence)


def is_closing_fence(line: str, marker: str, minimum_length: int) -> bool:
    match = re.fullmatch(r" {0,3}(?P<fence>`+|~+)[ \t]*", line)
    if match is None:
        return False
    fence = match.group("fence")
    return fence[0] == marker and len(fence) >= minimum_length


def is_backslash_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def find_unescaped(text: str, needle: str, start: int) -> int:
    while True:
        index = text.find(needle, start)
        if index < 0 or not is_backslash_escaped(text, index):
            return index
        start = index + len(needle)


def strip_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    cursor = 0
    visible: list[str] = []
    while cursor < len(line):
        if in_comment:
            end = line.find("-->", cursor)
            if end < 0:
                return "".join(visible), True
            in_comment = False
            cursor = end + 3
            continue
        start = find_unescaped(line, "<!--", cursor)
        if start < 0:
            visible.append(line[cursor:])
            break
        visible.append(line[cursor:start])
        in_comment = True
        cursor = start + 4
    return "".join(visible), in_comment


def fenced_blocks(text: str, failures: list[str]) -> list[str]:
    """Extract visible CommonMark-style fenced blocks, conservatively."""
    blocks: list[str] = []
    block_lines: list[str] = []
    fence_marker = ""
    fence_length = 0
    fence_start = 0
    in_comment = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        if fence_marker:
            if is_closing_fence(line, fence_marker, fence_length):
                blocks.append("\n".join(block_lines))
                block_lines = []
                fence_marker = ""
                fence_length = 0
                fence_start = 0
            else:
                block_lines.append(line)
            continue

        if not in_comment:
            opened = opening_fence(line)
            if opened is not None:
                fence_marker, fence_length = opened
                fence_start = line_number
                continue

        _, in_comment = strip_html_comments(line, in_comment)

    if fence_marker:
        failures.append(
            f"README.md contains an unclosed fenced code block starting at line {fence_start}"
        )
    if in_comment:
        failures.append("README.md contains an unclosed HTML comment")
    return blocks


def option_values(argv: list[str], option: str, failures: list[str]) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(argv):
        if token == option:
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                failures.append(f"README installer command has no value for {option}")
            else:
                values.append(argv[index + 1])
        elif token.startswith(f"{option}="):
            values.append(token.split("=", 1)[1])
    return values


def require_exact_option(
    argv: list[str], option: str, expected: str, failures: list[str]
) -> None:
    values = option_values(argv, option, failures)
    if values != [expected]:
        failures.append(
            f"README installer {option} must occur once with exact value {expected!r}; "
            f"found {values!r}"
        )


def validate_readme(text: str, version: str, failures: list[str]) -> None:
    blocks = fenced_blocks(text, failures)
    installer_name = "install-skill-from-github.py"
    expected_installer_line = (
        'python "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py" '
        f"--repo {REPOSITORY} --path {INSTALL_PATH} --ref v{version}"
    )
    if text.count(installer_name) != 1:
        failures.append(
            "README.md must contain exactly one raw install-skill-from-github.py occurrence"
        )
    if sum(line == expected_installer_line for line in text.splitlines()) != 1:
        failures.append(
            "README installer canonical physical line must occur exactly once"
        )
    install_blocks = [
        block for block in blocks if installer_name in block
    ]
    if len(install_blocks) != 1:
        failures.append(
            "README.md must contain exactly one fenced install-skill-from-github.py command"
        )
    else:
        physical_lines = install_blocks[0].splitlines()
        if len(physical_lines) != 1:
            failures.append("README installer command must be one physical line")
            installer_command = ""
        else:
            installer_command = physical_lines[0]
        if installer_command != expected_installer_line:
            failures.append("README installer command must match the canonical line exactly")
        try:
            argv = shlex.split(installer_command, posix=True)
        except ValueError as exc:
            failures.append(f"cannot parse README installer command: {exc}")
        else:
            require_exact_option(argv, "--repo", REPOSITORY, failures)
            require_exact_option(argv, "--path", INSTALL_PATH, failures)
            require_exact_option(argv, "--ref", f"v{version}", failures)
            if option_values(argv, "--name", failures):
                failures.append("README installer command must derive the name from --path")
            expected = [
                "python",
                "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py",
                "--repo",
                REPOSITORY,
                "--path",
                INSTALL_PATH,
                "--ref",
                f"v{version}",
            ]
            if argv != expected:
                failures.append(
                    "README installer command must contain only the documented exact argv"
                )

    expected_verification_line = (
        f"python scripts/release_check.py --expected-version {version}"
    )
    verification_blocks = [
        block
        for block in blocks
        if "--expected-version" in block and "release_check.py" in block
    ]
    if text.count("--expected-version") != 1:
        failures.append(
            "README.md must contain exactly one raw --expected-version occurrence"
        )
    if sum(line == expected_verification_line for line in text.splitlines()) != 1:
        failures.append(
            "README installed verification canonical physical line must occur exactly once"
        )
    if len(verification_blocks) != 1:
        failures.append(
            "README.md must contain exactly one fenced installed release_check.py command"
        )
    else:
        physical_lines = verification_blocks[0].splitlines()
        if len(physical_lines) != 1:
            failures.append("README installed verification command must be one physical line")
            verification_command = ""
        else:
            verification_command = physical_lines[0]
        if verification_command != expected_verification_line:
            failures.append(
                "README installed verification command must match the canonical line exactly"
            )
        try:
            argv = shlex.split(verification_command, posix=True)
        except ValueError as exc:
            failures.append(f"cannot parse README verification command: {exc}")
        else:
            expected = [
                "python",
                "scripts/release_check.py",
                "--expected-version",
                version,
            ]
            if argv != expected:
                failures.append(
                    "README installed verification command must contain only the exact argv"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=DEFAULT_REPOSITORY_ROOT,
        help="Repository root (defaults to the parent of this tools directory).",
    )
    parser.add_argument(
        "--expected-version",
        help="Fail unless the repository Skill VERSION exactly matches this value.",
    )
    args = parser.parse_args()
    root = Path(os.path.abspath(args.repository_root))
    failures: list[str] = []
    root_is_safe = validate_directory_components(
        root, "repository root", failures
    )
    skill_root_is_safe = root_is_safe and validate_directory_components(
        root / INSTALL_PATH, "repository Skill path", failures
    )
    raw_version = (
        read_regular_utf8(
            root / INSTALL_PATH / "VERSION", "Skill VERSION", failures
        )
        if skill_root_is_safe
        else None
    )
    version = raw_version.strip() if raw_version is not None else ""
    if raw_version is not None:
        if raw_version not in {version, f"{version}\n", f"{version}\r\n"}:
            failures.append("Skill VERSION must contain one UTF-8 semantic version line")
        if not SEMVER_RE.fullmatch(version):
            failures.append(f"Skill VERSION is not semantic version text: {version!r}")
        if args.expected_version is not None and version != args.expected_version:
            failures.append(f"expected version {args.expected_version!r}, got {version!r}")
    readme = (
        read_regular_utf8(root / "README.md", "repository README.md", failures)
        if root_is_safe
        else None
    )
    if readme is not None:
        if not readme.strip():
            failures.append("repository README.md must not be empty")
        elif version:
            validate_readme(readme, version, failures)
    report = {
        "status": "failed" if failures else "passed",
        "version": version,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
