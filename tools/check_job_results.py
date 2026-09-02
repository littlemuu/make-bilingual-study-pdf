#!/usr/bin/env python3
"""Require every declared GitHub Actions dependency result to be exactly success."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable


class ResultError(RuntimeError):
    pass


def require_all_success(raw_needs: str, required_jobs: Iterable[str]) -> None:
    required = tuple(required_jobs)
    if not required or len(required) != len(set(required)):
        raise ResultError("required job names must be non-empty and unique")
    try:
        needs = json.loads(raw_needs)
    except json.JSONDecodeError as exc:
        raise ResultError(f"CI_NEEDS_JSON is not valid JSON: {exc.msg}") from exc
    if not isinstance(needs, dict):
        raise ResultError("CI_NEEDS_JSON must be an object")

    unexpected = set(needs).difference(required)
    if unexpected:
        raise ResultError(f"unexpected dependency results: {sorted(unexpected)}")

    failures: list[str] = []
    for job in required:
        record: Any = needs.get(job)
        if not isinstance(record, dict) or "result" not in record:
            failures.append(f"{job}=missing")
            continue
        result = record["result"]
        if result != "success":
            rendered = result if isinstance(result, str) else "invalid"
            failures.append(f"{job}={rendered}")
    if failures:
        raise ResultError("dependencies did not all succeed: " + ", ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require", nargs="+", required=True, metavar="JOB")
    args = parser.parse_args()
    raw_needs = os.environ.get("CI_NEEDS_JSON")
    if raw_needs is None:
        print("job result check failed: CI_NEEDS_JSON is missing", file=sys.stderr)
        return 1
    try:
        require_all_success(raw_needs, args.require)
    except ResultError as exc:
        print(f"job result check failed: {exc}", file=sys.stderr)
        return 1
    print("all required dependency jobs succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
