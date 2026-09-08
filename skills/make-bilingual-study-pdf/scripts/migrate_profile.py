#!/usr/bin/env python3
"""Migrate an exact historical assignment V1 WORK to the installed V2 Profile."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from common import json_loads_strict
from document_ir import (
    build_document_ir,
    load_adapter_source_evidence,
    validate_ir_against_sources,
)
from profile import canonical_profile_sha256, load_profile, profile_contract, validate_profile
from safe_artifacts import (
    atomic_write_bytes,
    clear_artifact_directory,
    create_artifact_directory_exclusive,
    inspect_artifact_file,
    inspect_missing_artifact_file,
    lexical_absolute_path,
    lexical_paths_overlap,
    prepare_artifact_directory,
    read_artifact_bytes,
    recheck_artifact_file,
    remove_artifact_file,
    validate_artifact_tree,
)


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
V1_FILE_SHA256 = "58920601161479315f3673c2505f8d3b8e1915decf6c92f7931769b0b35b72e2"
V1_CANONICAL_SHA256 = "8ce2863ab72adc1ac11f415576060afbbdf39ab7d4f62fc7f25b88b31539c774"
V1_CONTRACT_SHA256 = "8510b893061ceb05d963595b51441e580b9ec4d79582702163589b43836191f5"
V2_CANONICAL_SHA256 = "7d608f86a741e770587a04d446af726f892b5fedf1df2de41819ffd6e34a315e"
UPSTREAM = ("manifest.json", "profile.json", "document-ir.json")
GATE_FILES = (
    "output/qa-report.json",
    "output/visual-review.json",
    "output/compile-audit.json",
    "output/docx-audit.json",
    "output/output-audit.json",
    "translation/translation-audit.json",
    "source-audit.json",
)


class MigrationRejected(ValueError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("migration input did not match the frozen historical V1")


class MigrationFailed(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        self.report = report
        super().__init__("migration stopped; follow the recovery report")


def _payload(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(value: Any) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


def _json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json_loads_strict(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: expected JSON object")
    return value


def _target_profile() -> dict[str, Any]:
    profile = load_profile("assignment-en-zh")
    if profile.get("schema_version") != 2 or canonical_profile_sha256(profile) != V2_CANONICAL_SHA256:
        raise ValueError("installed assignment-en-zh V2 Profile does not match the frozen target")
    return profile


def _match_v1(profile_bytes: bytes) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    profile = _json(profile_bytes, "profile.json")
    try:
        profile = validate_profile(profile)
    except ValueError as exc:
        return profile, [f"profile validation failed: {exc}"]
    if profile.get("id") != "assignment-en-zh":
        reasons.append("profile id is not assignment-en-zh")
    if profile.get("schema_version") != 1:
        reasons.append("profile schema_version is not 1")
    if _digest(profile_bytes.replace(b"\r\n", b"\n")) != V1_FILE_SHA256:
        reasons.append("profile bytes do not match the frozen historical V1")
    if canonical_profile_sha256(profile) != V1_CANONICAL_SHA256:
        reasons.append("profile canonical hash does not match the frozen historical V1")
    if _canonical_digest(profile_contract(profile)) != V1_CONTRACT_SHA256:
        reasons.append("profile contract does not match the frozen historical V1")
    return profile, reasons


def _invalidate(work_dir: Path) -> None:
    for name in GATE_FILES:
        path = work_dir / name
        if path.parent.exists():
            remove_artifact_file(path, boundary=work_dir)
    for name in ("output", "translation"):
        clear_artifact_directory(work_dir / name, boundary=work_dir, remove_directory=True)


def _upstream_state(
    work_dir: Path,
    before: dict[str, bytes],
    after: dict[str, bytes],
) -> dict[str, str]:
    state: dict[str, str] = {}
    for name in UPSTREAM:
        try:
            observed = read_artifact_bytes(work_dir / name, boundary=work_dir)
        except Exception:
            state[name] = "unknown"
            continue
        if observed == before[name]:
            state[name] = "old"
        elif observed == after[name]:
            state[name] = "new"
        else:
            state[name] = "unknown"
    return state


def _gate_state(work_dir: Path) -> dict[str, str]:
    roots: dict[str, str] = {}
    for name in ("output", "translation"):
        try:
            roots[f"{name}/"] = (
                "present"
                if validate_artifact_tree(work_dir / name, work_dir) is not None
                else "missing"
            )
        except Exception:
            roots[f"{name}/"] = "unknown"

    state: dict[str, str] = {}
    for name in GATE_FILES:
        root = name.split("/", 1)[0]
        if "/" in name and roots.get(f"{root}/") == "missing":
            state[name] = "missing"
            continue
        try:
            snapshot = inspect_artifact_file(
                work_dir / name, boundary=work_dir, allow_missing=True
            )
            state[name] = "present" if snapshot.exists else "missing"
        except Exception:
            state[name] = "unknown"
    state.update(roots)
    return state


def _failure_report(
    report: dict[str, Any],
    *,
    work_dir: Path,
    backup_dir: Path,
    before: dict[str, bytes],
    after: dict[str, bytes],
    failed_stage: str,
    completed_steps: list[str],
    backup_complete: bool,
    backup_snapshots: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    upstream = _upstream_state(work_dir, before, after)
    verified_backup = backup_complete and set(backup_snapshots) == set(UPSTREAM)
    if verified_backup:
        try:
            verified_backup = all(
                read_artifact_bytes(
                    backup_dir / name,
                    boundary=backup_dir,
                    expected=backup_snapshots[name],
                )
                == before[name]
                for name in UPSTREAM
            )
        except Exception:
            verified_backup = False
    changed_or_unknown = any(value != "old" for value in upstream.values())
    if verified_backup and changed_or_unknown:
        recovery_action = (
            "stop; restore manifest.json, profile.json, and document-ir.json from "
            "the backup, verify their source binding, then rebuild from source-audit"
        )
    elif verified_backup:
        recovery_action = (
            "WORK upstream files remain at V1; keep this backup, inspect the failure, "
            "and retry only with a new backup path"
        )
    else:
        recovery_action = (
            "WORK upstream files were safely reread above; do not use this incomplete "
            "backup, inspect the failure, and retry only with a new backup path"
        )
    failed = copy.deepcopy(report)
    failed.update(
        {
            "status": "failed",
            "failed_stage": failed_stage,
            "completed_steps": list(completed_steps),
            "error": {"type": type(error).__name__, "message": str(error)},
            "backup": {
                "path": str(backup_dir),
                "status": "complete" if verified_backup else "incomplete-or-unavailable",
                "files": {name: _digest(data) for name, data in before.items()}
                if verified_backup
                else {},
            },
            "upstream_state": upstream,
            "gate_state": _gate_state(work_dir),
            "next_action": recovery_action,
        }
    )
    return failed


def migrate_profile(work_dir: Path, backup_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    work_dir = lexical_absolute_path(work_dir)
    backup_dir = lexical_absolute_path(backup_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    snapshots = {
        name: inspect_artifact_file(work_dir / name, boundary=work_dir)
        for name in UPSTREAM
    }
    before = {
        name: read_artifact_bytes(work_dir / name, boundary=work_dir, expected=snapshots[name])
        for name in UPSTREAM
    }
    profile, reasons = _match_v1(before["profile.json"])
    target_profile = _target_profile()
    target_binding = {
        "id": target_profile["id"],
        "sha256": canonical_profile_sha256(target_profile),
    }
    manifest = _json(before["manifest.json"], "manifest.json")
    if (
        profile.get("schema_version") == 2
        and canonical_profile_sha256(profile) == V2_CANONICAL_SHA256
        and manifest.get("profile") == target_binding
        and not validate_ir_against_sources(work_dir, profile)
    ):
        return {
            "operation": "migrate-profile",
            "scope": "exact-historical-assignment-v1",
            "matched": True,
            "reason": ["already migrated"],
            "profile_before": {"id": profile["id"], "schema_version": 2, "canonical_sha256": V2_CANONICAL_SHA256},
            "profile_after": {"id": profile["id"], "schema_version": 2, "canonical_sha256": V2_CANONICAL_SHA256},
            "manifest_before_sha256": _digest(before["manifest.json"]),
            "manifest_after_sha256": _digest(before["manifest.json"]),
            "document_ir_before_sha256": _digest(before["document-ir.json"]),
            "document_ir_after_sha256": _digest(before["document-ir.json"]),
            "before": {name: _digest(data) for name, data in before.items()},
            "after": {name: _digest(data) for name, data in before.items()},
            "manifest_field_changes": {},
            "invalidate": [],
            "publish_order": [],
            "next_action": "source-audit" if not (work_dir / "source-audit.json").exists() else "status",
        }
    current_binding = {
        "id": profile.get("id"),
        "sha256": canonical_profile_sha256(profile),
    }
    if manifest.get("profile") != current_binding:
        reasons.append("manifest Profile binding does not match profile.json")
    if not reasons:
        failures = validate_ir_against_sources(work_dir, profile)
        reasons.extend(f"document IR binding is stale: {failure}" for failure in failures)
    if reasons:
        raise MigrationRejected({
            "operation": "migrate-profile",
            "scope": "exact-historical-assignment-v1",
            "matched": False,
            "reason": reasons,
            "profile_before": {
                "id": profile.get("id"),
                "schema_version": profile.get("schema_version"),
                "canonical_sha256": canonical_profile_sha256(profile),
            },
            "profile_after": {
                "id": target_profile["id"],
                "schema_version": 2,
                "canonical_sha256": V2_CANONICAL_SHA256,
            },
            "next_action": "leave WORK unchanged and inspect the rejected V1 binding",
        })
    if lexical_paths_overlap(backup_dir, work_dir) or lexical_paths_overlap(
        backup_dir, SKILL_DIR
    ):
        raise ValueError("backup must be outside WORK and the installed Skill root")

    target_manifest = copy.deepcopy(manifest)
    target_manifest["profile"] = target_binding
    manifest_bytes = _payload(target_manifest)
    blocks_path = work_dir / "blocks.jsonl"
    blocks_snapshot = inspect_artifact_file(blocks_path, boundary=work_dir)
    blocks_bytes = read_artifact_bytes(blocks_path, boundary=work_dir, expected=blocks_snapshot)
    _evidence, adapter_freeze = load_adapter_source_evidence(work_dir, manifest)
    target_ir = build_document_ir(
        target_manifest,
        [json.loads(line) for line in blocks_bytes.splitlines() if line.strip()],
        target_profile,
        manifest_sha256=_digest(manifest_bytes),
        blocks_sha256=_digest(blocks_bytes),
        adapter_freeze=adapter_freeze,
    )
    after = {
        "manifest.json": manifest_bytes,
        "profile.json": _payload(target_profile),
        "document-ir.json": _payload(target_ir),
    }
    report = {
        "operation": "migrate-profile",
        "scope": "exact-historical-assignment-v1",
        "matched": True,
        "reason": [],
        "profile_before": {"id": profile["id"], "schema_version": 1, "canonical_sha256": V1_CANONICAL_SHA256},
        "profile_after": {"id": target_profile["id"], "schema_version": 2, "canonical_sha256": V2_CANONICAL_SHA256},
        "manifest_before_sha256": _digest(before["manifest.json"]),
        "manifest_after_sha256": _digest(after["manifest.json"]),
        "document_ir_before_sha256": _digest(before["document-ir.json"]),
        "document_ir_after_sha256": _digest(after["document-ir.json"]),
        "before": {name: _digest(data) for name, data in before.items()},
        "after": {name: _digest(data) for name, data in after.items()},
        "manifest_field_changes": {"profile": {"before": manifest["profile"], "after": target_binding}},
        "invalidate": [name for name in GATE_FILES if (work_dir / name).exists()] + ["output/", "translation/"],
        "publish_order": list(UPSTREAM),
        "next_action": "source-audit",
    }
    if dry_run:
        return report

    failed_stage = "create-backup"
    completed_steps: list[str] = []
    backup_complete = False
    backup_snapshots: dict[str, Any] = {}
    try:
        prepare_artifact_directory(backup_dir.parent)
        if lexical_paths_overlap(backup_dir, work_dir) or lexical_paths_overlap(
            backup_dir, SKILL_DIR
        ):
            raise ValueError(
                "backup path began overlapping WORK or the installed Skill root"
            )
        backup_directory = create_artifact_directory_exclusive(backup_dir)
        completed_steps.append("backup-directory-created")
        backup_targets = {
            name: inspect_missing_artifact_file(
                backup_dir / name, parent=backup_directory
            )
            for name in UPSTREAM
        }
        for name, data in before.items():
            failed_stage = f"backup:{name}"
            atomic_write_bytes(
                backup_dir / name,
                data,
                boundary=backup_dir,
                expected=backup_targets[name],
            )
            backup_snapshots[name] = inspect_artifact_file(
                backup_dir / name, boundary=backup_dir
            )
            if read_artifact_bytes(
                backup_dir / name,
                boundary=backup_dir,
                expected=backup_snapshots[name],
            ) != data:
                raise ValueError(f"backup {name} failed byte verification")
            for verified_name, verified_snapshot in backup_snapshots.items():
                if read_artifact_bytes(
                    backup_dir / verified_name,
                    boundary=backup_dir,
                    expected=verified_snapshot,
                ) != before[verified_name]:
                    raise ValueError(
                        f"backup {verified_name} changed during backup publication"
                    )
            completed_steps.append(f"backup:{name}")
        backup_complete = True
        completed_steps.append("backup-complete")

        failed_stage = "prepare-publication"
        with tempfile.TemporaryDirectory(
            prefix="migration-prepared-", dir=backup_dir.parent
        ) as temporary:
            prepared = Path(temporary)
            for name, data in after.items():
                atomic_write_bytes(prepared / name, data, boundary=prepared)
                if read_artifact_bytes(prepared / name, boundary=prepared) != data:
                    raise ValueError(f"prepared {name} failed byte verification")
            completed_steps.append("prepared")

            failed_stage = "recheck-inputs"
            for snapshot in snapshots.values():
                recheck_artifact_file(snapshot)
            recheck_artifact_file(blocks_snapshot)
            if read_artifact_bytes(
                blocks_path, boundary=work_dir, expected=blocks_snapshot
            ) != blocks_bytes:
                raise ValueError("blocks.jsonl changed during migration")
            _current_evidence, current_freeze = load_adapter_source_evidence(
                work_dir, manifest
            )
            if current_freeze != adapter_freeze:
                raise ValueError("adapter source evidence changed during migration")
            validate_artifact_tree(work_dir, work_dir, allow_missing=False)
            completed_steps.append("inputs-rechecked")

            failed_stage = "invalidate"
            _invalidate(work_dir)
            completed_steps.append("invalidated")
            for index, name in enumerate(UPSTREAM):
                failed_stage = f"publish:{name}"
                for remaining in UPSTREAM[index:]:
                    recheck_artifact_file(snapshots[remaining])
                    if read_artifact_bytes(
                        work_dir / remaining,
                        boundary=work_dir,
                        expected=snapshots[remaining],
                    ) != before[remaining]:
                        raise ValueError("upstream binding changed during migration")
                atomic_write_bytes(
                    work_dir / name,
                    after[name],
                    boundary=work_dir,
                    expected=snapshots[name],
                )
                completed_steps.append(f"published:{name}")

        failed_stage = "verify-publication"
        for name, data in after.items():
            if read_artifact_bytes(work_dir / name, boundary=work_dir) != data:
                raise ValueError(f"published {name} failed byte verification")
        if validate_ir_against_sources(work_dir, target_profile):
            raise ValueError("published V2 document IR failed source binding verification")
        failed_stage = "verify-backup"
        for name, snapshot in backup_snapshots.items():
            if read_artifact_bytes(
                backup_dir / name, boundary=backup_dir, expected=snapshot
            ) != before[name]:
                raise ValueError(f"backup {name} changed during migration")
    except Exception as exc:
        if isinstance(exc, MigrationFailed):
            raise
        raise MigrationFailed(
            _failure_report(
                report,
                work_dir=work_dir,
                backup_dir=backup_dir,
                before=before,
                after=after,
                failed_stage=failed_stage,
                completed_steps=completed_steps,
                backup_complete=backup_complete,
                backup_snapshots=backup_snapshots,
                error=exc,
            )
        ) from exc
    return report
