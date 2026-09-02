#!/usr/bin/env python3
"""Validate repository-only release documentation against the Skill version."""
from __future__ import annotations

import argparse
import json
import re
import shlex
from pathlib import Path

from _payload_fs import (
    lexical_absolute,
    read_regular_utf8,
    validate_directory_components,
)


DEFAULT_REPOSITORY_ROOT = lexical_absolute(Path(__file__)).parent.parent
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
    root = lexical_absolute(args.repository_root)
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
