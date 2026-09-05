#!/usr/bin/env python3
"""Executable work-package-A reference contract on disposable real V1 WORKs.

This is not an installed migrate-profile command. Work package B must implement
the frozen transaction and run these same assertions against its public CLI.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import v2_assignment_chain_diff_test as chain
from v2_migration_contract_test import build_candidate_v2, FROZEN_CANONICAL_SHA256
from document_ir import build_document_ir, validate_ir_against_sources
from profile import canonical_profile_sha256
from safe_artifacts import (
    atomic_write_bytes, clear_artifact_directory, inspect_artifact_file,
    read_artifact_bytes, recheck_artifact_file, remove_artifact_file,
    validate_artifact_tree,
)

UPSTREAM = ("manifest.json", "profile.json", "document-ir.json")
GATES = (
    "output/qa-report.json", "output/visual-review.json",
    "output/compile-audit.json", "output/docx-audit.json",
    "output/output-audit.json", "translation/translation-audit.json",
    "source-audit.json",
)


def payload(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def tree_bytes(work: Path) -> dict[str, str]:
    return {p.relative_to(work).as_posix(): digest(p.read_bytes())
            for p in work.rglob("*") if p.is_file()}


def invalidate(work: Path) -> None:
    # Invalidate from the final gate upwards before changing any upstream bytes.
    for name in GATES:
        if (work / name).parent.exists():
            remove_artifact_file(work / name, boundary=work)
    for name in ("output", "translation"):
        clear_artifact_directory(work / name, boundary=work, remove_directory=True)


def migrate_profile_reference(
    work: Path, backup: Path, *, dry_run: bool = False, interrupt_after: str | None = None,
) -> dict:
    """Execute the proposed transaction using real runtime builders/safe writers."""
    if backup == work or work in backup.parents:
        raise ValueError("backup must be outside WORK")
    validate_artifact_tree(work, work)
    snapshots = {name: inspect_artifact_file(work / name, boundary=work) for name in UPSTREAM}
    before = {name: read_artifact_bytes(work / name, boundary=work) for name in UPSTREAM}
    profile = json.loads(before["profile.json"])
    if canonical_profile_sha256(profile) != FROZEN_CANONICAL_SHA256:
        raise ValueError("reference contract accepts only the frozen assignment V1")
    if validate_ir_against_sources(work, profile):
        raise ValueError("V1 source binding is stale")
    target_profile = build_candidate_v2(profile)
    manifest = json.loads(before["manifest.json"])
    if manifest.get("profile") != {"id": profile["id"], "sha256": canonical_profile_sha256(profile)}:
        raise ValueError("V1 manifest Profile binding is stale")
    target_manifest = copy.deepcopy(manifest)
    target_manifest["profile"] = {
        "id": target_profile["id"], "sha256": canonical_profile_sha256(target_profile),
    }
    manifest_bytes = payload(target_manifest)
    blocks_path = work / "blocks.jsonl"
    blocks_bytes = read_artifact_bytes(blocks_path, boundary=work)
    ir = build_document_ir(
        target_manifest, [json.loads(line) for line in blocks_bytes.splitlines() if line.strip()],
        target_profile, manifest_sha256=digest(manifest_bytes), blocks_sha256=digest(blocks_bytes),
    )
    after = {"manifest.json": manifest_bytes, "profile.json": payload(target_profile),
             "document-ir.json": payload(ir)}
    report = {
        "operation": "migrate-profile", "scope": "test-reference-contract",
        "matched": True, "reason": [],
        "profile_before": {"id": profile["id"], "schema_version": profile["schema_version"],
                           "canonical_sha256": canonical_profile_sha256(profile)},
        "profile_after": {"id": target_profile["id"], "schema_version": target_profile["schema_version"],
                          "canonical_sha256": canonical_profile_sha256(target_profile)},
        "manifest_before_sha256": digest(before["manifest.json"]),
        "manifest_after_sha256": digest(after["manifest.json"]),
        "document_ir_before_sha256": digest(before["document-ir.json"]),
        "document_ir_after_sha256": digest(after["document-ir.json"]),
        "before": {name: digest(data) for name, data in before.items()},
        "after": {name: digest(data) for name, data in after.items()},
        "manifest_field_changes": {"profile": {"before": manifest["profile"], "after": target_manifest["profile"]}},
        "invalidate": [name for name in GATES if (work / name).exists()] + ["output/", "translation/"],
        "publish_order": list(UPSTREAM), "next_action": "source-audit",
    }
    if dry_run:
        return report
    backup.mkdir(parents=True, exist_ok=False)
    for name, data in before.items():
        atomic_write_bytes(backup / name, data, boundary=backup)
    with tempfile.TemporaryDirectory(prefix="migration-prepared-", dir=backup.parent) as temporary:
        prepared = Path(temporary)
        for name, data in after.items():
            atomic_write_bytes(prepared / name, data, boundary=prepared)
            assert read_artifact_bytes(prepared / name, boundary=prepared) == data
        if interrupt_after == "prepared":
            raise RuntimeError("interrupted after prepared")
        # Every fixed path/source is rechecked before the first invalidation.
        for snapshot in snapshots.values():
            recheck_artifact_file(snapshot)
        assert read_artifact_bytes(blocks_path, boundary=work) == blocks_bytes
        invalidate(work)
        if interrupt_after == "invalidated":
            raise RuntimeError("interrupted after invalidated")
        for index, name in enumerate(UPSTREAM):
            for remaining in UPSTREAM[index:]:
                recheck_artifact_file(snapshots[remaining])
                if read_artifact_bytes(work / remaining, boundary=work) != before[remaining]:
                    raise ValueError("upstream changed during publication")
            atomic_write_bytes(work / name, after[name], boundary=work, expected=snapshots[name])
            if interrupt_after == name:
                raise RuntimeError(f"interrupted after {name}")
    assert all(read_artifact_bytes(work / name, boundary=work) == after[name] for name in UPSTREAM)
    assert not validate_ir_against_sources(work, target_profile)
    return report


def rollback_source_binding(work: Path, backup: Path) -> None:
    validate_artifact_tree(work, work)
    restored = {name: read_artifact_bytes(backup / name, boundary=backup) for name in UPSTREAM}
    snapshots = {name: inspect_artifact_file(work / name, boundary=work) for name in UPSTREAM}
    invalidate(work)
    for name in UPSTREAM:
        recheck_artifact_file(snapshots[name])
        atomic_write_bytes(work / name, restored[name], boundary=work, expected=snapshots[name])
    assert not validate_ir_against_sources(work, json.loads(restored["profile.json"]))


class ExistingWorkMigrationTests(unittest.TestCase):
    def test_existing_work_dry_run_migration_interruptions_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="existing-v1-migration-") as temporary:
            root = Path(temporary)
            chain.run_assignment_chain(root / "original")
            original = root / "original/work"
            before = tree_bytes(original)
            invocation = [sys.executable, "-B", str(Path(__file__).resolve()), "migrate-profile",
                          str(original), "--backup", str(root / "unused"), "--dry-run"]
            first = chain._last_pipeline_json("migrate-profile", subprocess.check_output(invocation, env=chain._subprocess_env(), text=True))
            self.assertEqual(first, migrate_profile_reference(original, root / "unused", dry_run=True))
            self.assertEqual(tree_bytes(original), before)
            self.assertFalse((root / "unused").exists())
            for stop in ("prepared", "invalidated", *UPSTREAM, None):
                with self.subTest(stop=stop):
                    case = root / str(stop)
                    work = case / "work"
                    shutil.copytree(original, work)
                    backup = case / "backup"
                    if stop:
                        with self.assertRaisesRegex(RuntimeError, "interrupted"):
                            migrate_profile_reference(work, backup, interrupt_after=stop)
                    else:
                        report = chain._last_pipeline_json("migrate-profile", subprocess.check_output(
                            [sys.executable, "-B", str(Path(__file__).resolve()), "migrate-profile",
                             str(work), "--backup", str(backup)], env=chain._subprocess_env(), text=True,
                        ))
                        self.assertEqual(report, first)
                        self.assertEqual(chain.read_json(work / "document-ir.json")["source"]["manifest_sha256"],
                                         digest((work / "manifest.json").read_bytes()))
                        chain._run_pipeline_stage("source-audit", work)
                        self.assertEqual(chain.read_json(work / "source-audit.json")["status"], "passed")
                        chain._run_script("init_glossary.py", work)
                        chain._run_pipeline_stage("prepare", work, "--max-source-chars", "1000")
                        self.assertTrue((work / "translation/plan.json").is_file())
                    if stop == "prepared":
                        self.assertEqual(tree_bytes(work), before)
                    elif stop is not None:
                        self.assertFalse((work / "translation").exists())
                        self.assertFalse((work / "output").exists())
                        self.assertTrue(all(not (work / name).exists() for name in GATES if name != "source-audit.json"))
                    rollback_source_binding(work, backup)
                    for name in UPSTREAM:
                        self.assertEqual((work / name).read_bytes(), (original / name).read_bytes())
                    self.assertFalse((work / "source-audit.json").exists())
                    chain._run_pipeline_stage("source-audit", work)
                    self.assertEqual(chain.read_json(work / "source-audit.json")["status"], "passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "migrate-profile":
        import argparse
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("work", type=Path)
        parser.add_argument("--backup", required=True, type=Path)
        parser.add_argument("--dry-run", action="store_true")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(migrate_profile_reference(args.work, args.backup, dry_run=args.dry_run)))
    else:
        unittest.main()
