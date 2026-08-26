#!/usr/bin/env python3
"""Validate the exact installable Skill payload and current release metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "release-manifest.json"
SKILL_NAME = "make-bilingual-study-pdf"
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

PROFILE_CONTRACTS = {
    "assignment-en-zh": (1, "native-text-pdf"),
    "academic-paper-en-zh": (2, "mineru-import"),
    "lecture-notes-en-zh": (2, "mineru-import"),
}
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
    return (
        "__pycache__" in relative.parts and relative.suffix in {".pyc", ".pyo"}
    )


def portable_path_key(value: str) -> str:
    path = PurePosixPath(value)
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in path.parts)


def collect_actual_files(
    *, ignore_generated_cache: bool, failures: list[str]
) -> dict[str, Path]:
    actual: dict[str, Path] = {}
    casefolded: dict[str, str] = {}
    manifest_key = portable_path_key(MANIFEST_NAME)
    for path in sorted(ROOT.rglob("*")):
        relative = PurePosixPath(path.relative_to(ROOT).as_posix())
        if path.is_symlink():
            failures.append(f"symbolic links are not allowed in payload: {relative}")
            continue
        if relative.as_posix() == MANIFEST_NAME:
            continue
        if ignore_generated_cache and is_generated_cache(relative):
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        folded = portable_path_key(name)
        if folded == manifest_key:
            failures.append(
                f"payload path conflicts with reserved manifest name: {name!r}"
            )
        if folded in casefolded and casefolded[folded] != name:
            failures.append(
                f"case-insensitive payload path collision: {casefolded[folded]!r}, {name!r}"
            )
        casefolded[folded] = name
        actual[name] = path
    return actual


def valid_manifest_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
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


def load_manifest(failures: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = ROOT / MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        failures.append(f"cannot read {MANIFEST_NAME}: {exc}")
        return {}, []
    if not isinstance(manifest, dict):
        failures.append(f"{MANIFEST_NAME} must contain a JSON object")
        return {}, []
    expected_keys = {
        "schema_version",
        "skill",
        "version",
        "hash_algorithm",
        "tree_sha256",
        "files",
    }
    if set(manifest) != expected_keys:
        failures.append(
            f"{MANIFEST_NAME} top-level keys mismatch: expected {sorted(expected_keys)!r}, "
            f"got {sorted(manifest)!r}"
        )
    if manifest.get("schema_version") != 1:
        failures.append(f"{MANIFEST_NAME} schema_version must be 1")
    if manifest.get("skill") != SKILL_NAME:
        failures.append(f"{MANIFEST_NAME} skill must be {SKILL_NAME!r}")
    if manifest.get("hash_algorithm") != "sha256":
        failures.append(f"{MANIFEST_NAME} hash_algorithm must be 'sha256'")
    records = manifest.get("files")
    if not isinstance(records, list):
        failures.append(f"{MANIFEST_NAME} files must be a list")
        return manifest, []
    return manifest, records


def validate_manifest_records(
    manifest: dict[str, Any], records: list[dict[str, Any]], failures: list[str]
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    observed_order: list[str] = []
    casefolded: dict[str, str] = {}
    manifest_key = portable_path_key(MANIFEST_NAME)
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            failures.append(
                f"{MANIFEST_NAME} files[{index}] must have only path, size, sha256"
            )
            continue
        name = record.get("path")
        size = record.get("size")
        digest = record.get("sha256")
        if not valid_manifest_path(name):
            failures.append(f"{MANIFEST_NAME} files[{index}] has invalid path {name!r}")
            continue
        assert isinstance(name, str)
        if portable_path_key(name) == manifest_key or is_generated_cache(
            PurePosixPath(name)
        ):
            failures.append(f"{MANIFEST_NAME} cannot list excluded path {name!r}")
        if name in indexed:
            failures.append(f"{MANIFEST_NAME} contains duplicate path {name!r}")
        folded = portable_path_key(name)
        if folded in casefolded and casefolded[folded] != name:
            failures.append(
                f"{MANIFEST_NAME} has case-insensitive collision: "
                f"{casefolded[folded]!r}, {name!r}"
            )
        casefolded[folded] = name
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            failures.append(f"{MANIFEST_NAME} path {name!r} has invalid size {size!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"{MANIFEST_NAME} path {name!r} has invalid sha256")
        indexed[name] = record
        observed_order.append(name)
    if observed_order != sorted(observed_order):
        failures.append(f"{MANIFEST_NAME} file records must be sorted by path")
    expected_tree = tree_sha256(records) if len(indexed) == len(records) else ""
    if manifest.get("tree_sha256") != expected_tree:
        failures.append(
            f"{MANIFEST_NAME} tree_sha256 mismatch: expected {expected_tree!r}, "
            f"got {manifest.get('tree_sha256')!r}"
        )
    return indexed


def validate_payload(
    records: dict[str, dict[str, Any]], *, ignore_generated_cache: bool, failures: list[str]
) -> None:
    actual = collect_actual_files(
        ignore_generated_cache=ignore_generated_cache, failures=failures
    )
    expected_names = set(records)
    actual_names = set(actual)
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    if missing:
        failures.append(f"missing payload files: {', '.join(missing)}")
    if extra:
        failures.append(f"unexpected payload files: {', '.join(extra)}")
    for name in sorted(expected_names & actual_names):
        path = actual[name]
        record = records[name]
        size = path.stat().st_size
        if size != record.get("size"):
            failures.append(
                f"payload size mismatch for {name}: expected {record.get('size')}, got {size}"
            )
            continue
        digest = sha256_file(path)
        if digest != record.get("sha256"):
            failures.append(
                f"payload sha256 mismatch for {name}: expected {record.get('sha256')}, "
                f"got {digest}"
            )


def read_release_version(failures: list[str]) -> str:
    path = ROOT / "VERSION"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"cannot read VERSION: {exc}")
        return ""
    version = raw.strip()
    if raw not in {version, f"{version}\n", f"{version}\r\n"}:
        failures.append("VERSION must contain one UTF-8 semantic version line")
    if not SEMVER_RE.fullmatch(version):
        failures.append(f"VERSION is not semantic version text: {version!r}")
    return version


def read_skill_name(failures: list[str]) -> str:
    try:
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"cannot read SKILL.md: {exc}")
        return ""
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n", text, flags=re.DOTALL)
    if not match:
        failures.append("SKILL.md has no valid leading YAML frontmatter block")
        return ""
    name_match = re.search(r"(?m)^name:\s*([^#\r\n]+?)\s*$", match.group(1))
    if not name_match:
        failures.append("SKILL.md frontmatter has no name")
        return ""
    return name_match.group(1).strip(" '\"")


def github_tag_from_environment() -> str | None:
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME") or ""
    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref.startswith("refs/tags/"):
        return github_ref.removeprefix("refs/tags/")
    return None


def read_text(relative: str, failures: list[str]) -> str:
    try:
        return (ROOT / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"cannot read {relative}: {exc}")
        return ""


def fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[^\r\n]*\r?\n(.*?)\r?\n```", text, flags=re.DOTALL)


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


def validate_readme(version: str, failures: list[str]) -> None:
    text = read_text("README.md", failures)
    install_blocks = [
        block for block in fenced_blocks(text) if "install-skill-from-github.py" in block
    ]
    if len(install_blocks) != 1:
        failures.append(
            "README.md must contain exactly one fenced install-skill-from-github.py command"
        )
    else:
        try:
            argv = shlex.split(install_blocks[0], posix=True)
        except ValueError as exc:
            failures.append(f"cannot parse README installer command: {exc}")
        else:
            require_exact_option(argv, "--repo", REPOSITORY, failures)
            require_exact_option(argv, "--path", INSTALL_PATH, failures)
            require_exact_option(argv, "--ref", f"v{version}", failures)
            if option_values(argv, "--name", failures):
                failures.append("README installer command must derive the name from --path")
            expected_argv = [
                "python",
                "<SKILL_INSTALLER_DIR>/scripts/install-skill-from-github.py",
                "--repo",
                REPOSITORY,
                "--path",
                INSTALL_PATH,
                "--ref",
                f"v{version}",
            ]
            if argv != expected_argv:
                failures.append(
                    "README installer command must contain only the documented exact argv"
                )

    verification_lines = [
        line
        for block in fenced_blocks(text)
        for line in block.splitlines()
        if "release_check.py" in line and "--expected-version" in line
    ]
    if len(verification_lines) != 1:
        failures.append(
            "README.md must contain exactly one fenced release_check.py verification command"
        )
    else:
        try:
            argv = shlex.split(verification_lines[0], posix=True)
        except ValueError as exc:
            failures.append(f"cannot parse README verification command: {exc}")
        else:
            values = option_values(argv, "--expected-version", failures)
            if values != [version]:
                failures.append(
                    "README verification --expected-version must occur once with exact value "
                    f"{version!r}; found {values!r}"
                )
            if argv != [
                "python",
                "scripts/release_check.py",
                "--expected-version",
                version,
            ]:
                failures.append(
                    "README verification command must contain only the documented exact argv"
                )


def validate_profiles(failures: list[str]) -> dict[str, dict[str, object]]:
    observed: dict[str, dict[str, object]] = {}
    for profile_id, (schema_version, adapter) in PROFILE_CONTRACTS.items():
        relative = f"profiles/{profile_id}.json"
        try:
            profile = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            failures.append(f"cannot read {relative}: {exc}")
            continue
        if not isinstance(profile, dict):
            failures.append(f"{relative} must contain a JSON object")
            continue
        profile_input = profile.get("input")
        if not isinstance(profile_input, dict):
            failures.append(f"{relative} input must be a JSON object")
            profile_input = {}
        actual = {
            "id": profile.get("id"),
            "schema_version": profile.get("schema_version"),
            "adapter": profile_input.get("adapter"),
        }
        observed[profile_id] = actual
        expected = {
            "id": profile_id,
            "schema_version": schema_version,
            "adapter": adapter,
        }
        if actual != expected:
            failures.append(
                f"{relative} release identity mismatch: expected {expected}, got {actual}"
            )
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate exact Skill payload bytes and current release metadata."
    )
    parser.add_argument(
        "--expected-version",
        help="Fail unless VERSION exactly matches this semantic version.",
    )
    parser.add_argument(
        "--tag",
        help="Validate an explicit tag; GitHub tag environments are detected automatically.",
    )
    parser.add_argument(
        "--ignore-generated-cache",
        action="store_true",
        help="Ignore only __pycache__ and .pyc/.pyo files created after installation.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    manifest, raw_records = load_manifest(failures)
    records = validate_manifest_records(manifest, raw_records, failures)
    validate_payload(
        records,
        ignore_generated_cache=args.ignore_generated_cache,
        failures=failures,
    )

    version = read_release_version(failures)
    if manifest.get("version") != version:
        failures.append(
            f"{MANIFEST_NAME} version must equal VERSION {version!r}; "
            f"got {manifest.get('version')!r}"
        )
    if args.expected_version and version != args.expected_version:
        failures.append(f"expected version {args.expected_version!r}, got {version!r}")

    skill_name = read_skill_name(failures)
    if skill_name and skill_name != SKILL_NAME:
        failures.append(
            f"SKILL.md name mismatch: expected {SKILL_NAME!r}, got {skill_name!r}"
        )

    expected_tag = f"v{version}" if version else ""
    tag = args.tag if args.tag is not None else github_tag_from_environment()
    if tag is not None and tag != expected_tag:
        failures.append(f"release tag mismatch: expected {expected_tag!r}, got {tag!r}")

    if version:
        validate_readme(version, failures)
    profiles = validate_profiles(failures)
    report = {
        "status": "failed" if failures else "passed",
        "skill": skill_name or SKILL_NAME,
        "version": version,
        "expected_tag": expected_tag,
        "checked_tag": tag,
        "manifest_file_count": len(records),
        "manifest_tree_sha256": manifest.get("tree_sha256"),
        "profiles": profiles,
        "failures": failures,
    }
    # ASCII escapes keep failure reports printable even on non-UTF-8 Windows consoles.
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
