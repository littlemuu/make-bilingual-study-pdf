#!/usr/bin/env python3
"""Require two installed Skill directories to have identical byte-level payloads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


FORBIDDEN_TOP_LEVEL = {
    ".git",
    ".github",
    ".python-version",
    "docs",
    "requirements-dev.txt",
    "tests",
    "tools",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory(root: Path) -> dict[str, dict[str, object]]:
    if not root.is_dir():
        raise ValueError(f"not an installed Skill directory: {root}")
    observed: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"installed Skill contains symbolic link: {relative}")
        if not path.is_file():
            continue
        if relative.split("/", 1)[0] in FORBIDDEN_TOP_LEVEL:
            raise ValueError(f"installed Skill contains repository-only path: {relative}")
        observed[relative] = {
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    if "release-manifest.json" not in observed:
        raise ValueError("installed Skill has no release-manifest.json")
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("download_install", type=Path)
    parser.add_argument("git_install", type=Path)
    args = parser.parse_args()
    try:
        download = inventory(args.download_install.resolve())
        git = inventory(args.git_install.resolve())
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    missing_from_git = sorted(set(download) - set(git))
    missing_from_download = sorted(set(git) - set(download))
    changed = sorted(
        path for path in set(download) & set(git) if download[path] != git[path]
    )
    report = {
        "status": (
            "passed"
            if not missing_from_git and not missing_from_download and not changed
            else "failed"
        ),
        "file_count": len(download),
        "missing_from_git": missing_from_git,
        "missing_from_download": missing_from_download,
        "changed": changed,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
