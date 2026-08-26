#!/usr/bin/env python3
"""Validate release metadata and the installable V2.3 payload."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NUMERIC_IDENTIFIER = r"(?:0|[1-9][0-9]*)"
NON_NUMERIC_IDENTIFIER = r"(?:[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
PRERELEASE_IDENTIFIER = rf"(?:{NUMERIC_IDENTIFIER}|{NON_NUMERIC_IDENTIFIER})"
SEMVER_PATTERN = (
    rf"{NUMERIC_IDENTIFIER}\.{NUMERIC_IDENTIFIER}\.{NUMERIC_IDENTIFIER}"
    rf"(?:-{PRERELEASE_IDENTIFIER}(?:\.{PRERELEASE_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
SEMVER_RE = re.compile(rf"^{SEMVER_PATTERN}$")
TAG_LITERAL_RE = re.compile(
    rf"(?<![0-9A-Za-z])(v{SEMVER_PATTERN})(?![0-9A-Za-z])"
)
SKILL_NAME = "make-bilingual-study-pdf"

PROFILE_CONTRACTS = {
    "assignment-en-zh": (1, "native-text-pdf"),
    "academic-paper-en-zh": (2, "mineru-import"),
    "lecture-notes-en-zh": (2, "mineru-import"),
}

REQUIRED_FILES = (
    "VERSION",
    "README.md",
    "SKILL.md",
    "agents/openai.yaml",
    ".github/V2.3_ACCEPTANCE.md",
    "references/development.md",
    "references/profile-ir.md",
    "scripts/release_check.py",
    "scripts/self_test.py",
    "scripts/pipeline.py",
    "scripts/import_mineru.py",
    "scripts/document_ir.py",
    "scripts/build_docx.py",
    "scripts/audit_docx.py",
    "scripts/compile_docx_pdf.py",
    "scripts/adapters/native_pdf.py",
    "scripts/adapters/mineru.py",
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
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
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


def read_utf8(relative: str, failures: list[str]) -> str:
    try:
        return (ROOT / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"cannot read {relative}: {exc}")
        return ""


def require_only_value(
    *,
    relative: str,
    text: str,
    pattern: re.Pattern[str],
    expected: str,
    label: str,
    failures: list[str],
) -> None:
    observed = pattern.findall(text)
    if observed != [expected]:
        failures.append(
            f"{relative} must contain exactly one {label} {expected!r}; "
            f"found {observed!r}"
        )


def require_current_tag_literals(
    *,
    relative: str,
    text: str,
    expected: str,
    failures: list[str],
) -> None:
    observed = TAG_LITERAL_RE.findall(text)
    if not observed or set(observed) != {expected}:
        failures.append(
            f"{relative} release tag literals must all equal {expected!r}; "
            f"found {observed!r}"
        )


def validate_profiles(failures: list[str]) -> dict[str, dict[str, object]]:
    observed: dict[str, dict[str, object]] = {}
    for profile_id, (schema_version, adapter) in PROFILE_CONTRACTS.items():
        relative = f"profiles/{profile_id}.json"
        path = ROOT / relative
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            failures.append(f"cannot read {relative}: {exc}")
            continue
        actual = {
            "id": profile.get("id"),
            "schema_version": profile.get("schema_version"),
            "adapter": profile.get("input", {}).get("adapter"),
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
        description="Validate the public version, pinned tag, and installable payload."
    )
    parser.add_argument(
        "--expected-version",
        help="Fail unless VERSION exactly matches this semantic version.",
    )
    parser.add_argument(
        "--tag",
        help="Validate an explicit tag; GitHub tag workflows are detected automatically.",
    )
    args = parser.parse_args()

    failures: list[str] = []
    version = read_release_version(failures)

    missing = [relative for relative in REQUIRED_FILES if not (ROOT / relative).is_file()]
    if missing:
        failures.append(f"missing release files: {', '.join(missing)}")

    skill_name = read_skill_name(failures)
    if skill_name and skill_name != SKILL_NAME:
        failures.append(
            f"SKILL.md name mismatch: expected {SKILL_NAME!r}, got {skill_name!r}"
        )

    if args.expected_version and version != args.expected_version:
        failures.append(
            f"expected version {args.expected_version!r}, got {version!r}"
        )

    expected_tag = f"v{version}" if version else ""
    tag = args.tag if args.tag is not None else github_tag_from_environment()
    if tag is not None and tag != expected_tag:
        failures.append(f"release tag mismatch: expected {expected_tag!r}, got {tag!r}")

    readme = read_utf8("README.md", failures)
    development = read_utf8("references/development.md", failures)
    acceptance = read_utf8(".github/V2.3_ACCEPTANCE.md", failures)
    if expected_tag:
        for relative, text in (
            ("README.md", readme),
            ("references/development.md", development),
            (".github/V2.3_ACCEPTANCE.md", acceptance),
        ):
            require_current_tag_literals(
                relative=relative,
                text=text,
                expected=expected_tag,
                failures=failures,
            )
        require_only_value(
            relative="README.md",
            text=readme,
            pattern=re.compile(r"--ref\s+(v[0-9A-Za-z.+-]+)"),
            expected=expected_tag,
            label="installer ref",
            failures=failures,
        )
        for relative, text in (
            ("references/development.md", development),
            (".github/V2.3_ACCEPTANCE.md", acceptance),
        ):
            require_only_value(
                relative=relative,
                text=text,
                pattern=re.compile(r"--tag\s+(v[0-9A-Za-z.+-]+)"),
                expected=expected_tag,
                label="release-check tag",
                failures=failures,
            )
    if version:
        require_only_value(
            relative="README.md",
            text=readme,
            pattern=re.compile(r"--expected-version\s+([0-9A-Za-z.+-]+)"),
            expected=version,
            label="verification version",
            failures=failures,
        )

    profiles = validate_profiles(failures)
    report = {
        "status": "failed" if failures else "passed",
        "skill": skill_name or SKILL_NAME,
        "version": version,
        "expected_tag": expected_tag,
        "checked_tag": tag,
        "required_file_count": len(REQUIRED_FILES),
        "profiles": profiles,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
