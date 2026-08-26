#!/usr/bin/env python3
"""Validate the exact installable Skill payload and current release metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(os.path.abspath(Path(__file__).parent.parent))
MANIFEST_NAME = "release-manifest.json"
SKILL_NAME = "make-bilingual-study-pdf"
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        value[key] = item
    return value


def json_loads_strict(payload: str) -> Any:
    """Parse JSON without importing payload modules or accepting duplicate keys."""
    return json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)


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
    root: Path, failures: list[str]
) -> list[tuple[Path, PurePosixPath, os.stat_result]]:
    """Walk without following symlinks, junctions, or other reparse points."""
    observed: list[tuple[Path, PurePosixPath, os.stat_result]] = []
    try:
        root_status = os.lstat(root)
        reject_unsafe_status(root_status, str(root), require_directory=True)
    except (OSError, ValueError) as exc:
        failures.append(f"cannot inspect payload root: {exc}")
        return observed

    def visit(
        directory: Path,
        relative_directory: PurePosixPath,
        expected_status: os.stat_result,
    ) -> None:
        try:
            current_status = os.lstat(directory)
            label = relative_directory.as_posix() or str(root)
            reject_unsafe_status(current_status, label, require_directory=True)
            if not metadata_matches(expected_status, current_status):
                raise ValueError(f"payload directory changed while traversing: {label}")
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except (OSError, ValueError) as exc:
            label = relative_directory.as_posix() or "."
            failures.append(f"cannot scan payload directory {label}: {exc}")
            return
        for entry in entries:
            relative = relative_directory / entry.name
            path = directory / entry.name
            try:
                status = os.lstat(path)
            except OSError as exc:
                failures.append(f"cannot inspect payload entry {relative}: {exc}")
                continue
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


def open_regular_fd(path: Path, label: str) -> tuple[int, os.stat_result]:
    ensure_safe_parent_chain(ROOT, path)
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


def read_regular_bytes(path: Path, label: str) -> bytes:
    fd, opened = open_regular_fd(path, label)
    with os.fdopen(fd, "rb") as handle:
        payload = handle.read()
        finished = os.fstat(handle.fileno())
    if not metadata_matches(opened, finished) or len(payload) != finished.st_size:
        raise ValueError(f"payload entry changed while reading: {label}")
    return payload


def hash_regular_file(path: Path, label: str) -> tuple[int, str]:
    fd, opened = open_regular_fd(path, label)
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
    for path, relative, status in iter_safe_entries(ROOT, failures):
        link_kind = unsafe_link_kind(status)
        if link_kind:
            failures.append(f"{link_kind} are not allowed in payload: {relative}")
            continue
        if stat.S_ISDIR(status.st_mode):
            continue
        if not stat.S_ISREG(status.st_mode):
            failures.append(
                f"non-regular filesystem entries are not allowed in payload: {relative}"
            )
            continue
        if relative.as_posix() == MANIFEST_NAME:
            continue
        if ignore_generated_cache and is_generated_cache(relative):
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
        manifest = json_loads_strict(
            read_regular_bytes(path, MANIFEST_NAME).decode("utf-8")
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
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
    if type(manifest.get("schema_version")) is not int or manifest.get(
        "schema_version"
    ) != 1:
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
        try:
            size, digest = hash_regular_file(path, name)
        except (OSError, ValueError) as exc:
            failures.append(f"cannot verify payload file {name}: {exc}")
            continue
        if size != record.get("size"):
            failures.append(
                f"payload size mismatch for {name}: expected {record.get('size')}, got {size}"
            )
            continue
        if digest != record.get("sha256"):
            failures.append(
                f"payload sha256 mismatch for {name}: expected {record.get('sha256')}, "
                f"got {digest}"
            )


def read_regular_utf8(relative: str, failures: list[str]) -> str:
    path = ROOT / relative
    try:
        return read_regular_bytes(path, relative).decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        failures.append(f"cannot read {relative}: {exc}")
        return ""


def read_release_version(failures: list[str]) -> str:
    raw = read_regular_utf8("VERSION", failures)
    version = raw.strip()
    if raw not in {version, f"{version}\n", f"{version}\r\n"}:
        failures.append("VERSION must contain one UTF-8 semantic version line")
    if not SEMVER_RE.fullmatch(version):
        failures.append(f"VERSION is not semantic version text: {version!r}")
    return version


def parse_string_scalar(raw: str, *, key: str, failures: list[str]) -> str:
    value = raw.strip()
    if not value:
        failures.append(f"SKILL.md frontmatter {key} must be a non-empty string")
        return ""
    if value.startswith(('"', "'")):
        if value[0] == "'":
            if len(value) < 2 or not value.endswith("'"):
                failures.append(f"SKILL.md frontmatter {key} has an unterminated string")
                return ""
            inner = value[1:-1]
            if "'" in inner.replace("''", ""):
                failures.append(f"SKILL.md frontmatter {key} has an invalid string")
                return ""
            parsed = inner.replace("''", "'")
        else:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                failures.append(f"SKILL.md frontmatter {key} has an invalid string")
                return ""
        if not isinstance(parsed, str) or not parsed:
            failures.append(f"SKILL.md frontmatter {key} must be a non-empty string")
            return ""
        return parsed
    lowered = value.casefold()
    non_string_tokens = {
        "null",
        "~",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        ".nan",
        ".inf",
        "+.inf",
        "-.inf",
    }
    if (
        lowered in non_string_tokens
        or value in {"#", "?", ":", "-"}
        or value.startswith("#")
        or re.fullmatch(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value)
        or value[0] in "[{&*!|>@`"
        or value.startswith(("- ", "? ", ": "))
        or re.search(r":\s", value)
        or re.search(r"\s#", value)
    ):
        failures.append(f"SKILL.md frontmatter {key} must be a string scalar")
        return ""
    return value


def read_skill_metadata(failures: list[str]) -> tuple[str, str]:
    """Check required fields with a deliberately narrow, fail-closed YAML subset.

    This standard-library check protects installed payloads. Repository CI separately
    runs the pinned upstream Skill validator for complete contract validation.
    """
    failure_count = len(failures)
    text = read_regular_utf8("SKILL.md", failures)
    if text == "":
        if len(failures) == failure_count:
            failures.append("SKILL.md must not be empty")
        return "", ""
    match = re.match(
        r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n",
        text,
        flags=re.DOTALL,
    )
    if not match:
        failures.append("SKILL.md has no valid leading YAML frontmatter block")
        return "", ""
    entries: dict[str, list[str]] = {"name": [], "description": []}
    for line_number, line in enumerate(match.group(1).splitlines(), start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(?:[ \t]*(.*))?", line)
        if not entry:
            failures.append(
                "SKILL.md frontmatter uses syntax outside the release checker's "
                f"supported flat string mapping on line {line_number}"
            )
            continue
        key, raw = entry.group(1), entry.group(2) or ""
        if key not in entries:
            failures.append(f"SKILL.md frontmatter has unexpected key {key!r}")
            continue
        entries[key].append(raw)
    parsed: dict[str, str] = {}
    for key in ("name", "description"):
        values = entries[key]
        if not values:
            failures.append(f"SKILL.md frontmatter has no {key}")
            parsed[key] = ""
        elif len(values) != 1:
            failures.append(f"SKILL.md frontmatter contains duplicate {key} keys")
            parsed[key] = ""
        else:
            parsed[key] = parse_string_scalar(values[0], key=key, failures=failures)
    return parsed["name"], parsed["description"]


def github_tag_from_environment() -> str | None:
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME") or ""
    github_ref = os.environ.get("GITHUB_REF", "")
    if github_ref.startswith("refs/tags/"):
        return github_ref.removeprefix("refs/tags/")
    return None


def validate_profiles(failures: list[str]) -> dict[str, dict[str, object]]:
    observed: dict[str, dict[str, object]] = {}
    for profile_id, (schema_version, adapter) in PROFILE_CONTRACTS.items():
        relative = f"profiles/{profile_id}.json"
        try:
            profile = json_loads_strict(read_regular_utf8(relative, failures))
        except (json.JSONDecodeError, ValueError) as exc:
            failures.append(f"cannot read {relative}: {exc}")
            continue
        if not isinstance(profile, dict):
            failures.append(f"{relative} must contain a JSON object")
            continue
        profile_input = profile.get("input")
        if not isinstance(profile_input, dict):
            failures.append(f"{relative} input must be a JSON object")
            profile_input = {}
        actual_schema_version = profile.get("schema_version")
        if type(actual_schema_version) is not int:
            failures.append(f"{relative} schema_version must be an integer")
        actual = {
            "id": profile.get("id"),
            "schema_version": actual_schema_version,
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
    if args.expected_version is not None and version != args.expected_version:
        failures.append(f"expected version {args.expected_version!r}, got {version!r}")

    skill_name, _skill_description = read_skill_metadata(failures)
    if skill_name and skill_name != SKILL_NAME:
        failures.append(
            f"SKILL.md name mismatch: expected {SKILL_NAME!r}, got {skill_name!r}"
        )

    expected_tag = f"v{version}" if version else ""
    tag = args.tag if args.tag is not None else github_tag_from_environment()
    if tag is not None and tag != expected_tag:
        failures.append(f"release tag mismatch: expected {expected_tag!r}, got {tag!r}")

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
