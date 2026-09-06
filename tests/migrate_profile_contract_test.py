#!/usr/bin/env python3
"""Exercise the public work-package-B profile migration command."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(REPOSITORY / "tests")]

import v2_assignment_chain_diff_test as chain
import migrate_profile as migration_module
import pipeline as pipeline_module
from migrate_profile import MigrationFailed
from safe_artifacts import ArtifactSafetyError
from profile import canonical_profile_sha256


V1_FIXTURE = REPOSITORY / "tests" / "fixtures" / "profiles" / "assignment-en-zh-v1.json"
V2_HASH = "7d608f86a741e770587a04d446af726f892b5fedf1df2de41819ffd6e34a315e"
UPSTREAM = ("manifest.json", "profile.json", "document-ir.json")


def tree_bytes(work: Path) -> dict[str, str]:
    return {
        path.relative_to(work).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in work.rglob("*")
        if path.is_file()
    }


def run_migration(work: Path, backup: Path, *extra: str, check: bool = True) -> tuple[subprocess.CompletedProcess[str], dict]:
    process = subprocess.run(
        [sys.executable, "-B", str(SCRIPTS / "pipeline.py"), "migrate-profile", str(work), "--backup", str(backup), *extra],
        check=False,
        capture_output=True,
        text=True,
        env=chain._subprocess_env(),
    )
    if check and process.returncode:
        raise AssertionError(process.stdout + process.stderr)
    return process, json.loads(process.stdout)


class ExistingWorkMigrationTests(unittest.TestCase):
    def seed_v1_work(self, root: Path) -> Path:
        v1 = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        chain.run_assignment_chain(root / "seed", v1)
        return root / "seed" / "work"

    def test_public_cli_migrates_blank_early_and_full_v1_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="existing-v1-migration-") as temporary:
            root = Path(temporary)
            v1 = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
            chain.run_assignment_chain(root / "seed", v1)
            seed = root / "seed" / "work"
            status = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS / "pipeline.py"), "status", str(seed)],
                check=True,
                capture_output=True,
                text=True,
                env=chain._subprocess_env(),
            )
            instruction = json.loads(status.stdout)["migration_instruction"]
            self.assertTrue(instruction["dry_run_first"])
            self.assertIn("migrate-profile", instruction["command"])
            cases = {}
            for state in ("blank", "early", "full"):
                work = root / state / "work"
                shutil.copytree(seed, work)
                if state == "blank":
                    shutil.rmtree(work / "translation")
                    shutil.rmtree(work / "output")
                    (work / "source-audit.json").unlink()
                    (work / "glossary.json").unlink(missing_ok=True)
                elif state == "early":
                    shutil.rmtree(work / "translation")
                    shutil.rmtree(work / "output")
                cases[state] = work

            for state, work in cases.items():
                with self.subTest(state=state):
                    before = tree_bytes(work)
                    backup = root / f"{state}-backup"
                    _, dry_run = run_migration(work, backup, "--dry-run")
                    self.assertEqual(tree_bytes(work), before)
                    self.assertFalse(backup.exists())
                    _, actual = run_migration(work, backup)
                    self.assertEqual(actual, dry_run)
                    self.assertEqual(set(path.name for path in backup.iterdir()), set(UPSTREAM))
                    profile = json.loads((work / "profile.json").read_text(encoding="utf-8"))
                    self.assertEqual(canonical_profile_sha256(profile), V2_HASH)
                    self.assertFalse((work / "translation").exists())
                    self.assertFalse((work / "output").exists())
                    self.assertFalse((work / "source-audit.json").exists())
                    chain._run_pipeline_stage("source-audit", work)
                    self.assertEqual(chain.read_json(work / "source-audit.json")["status"], "passed")
                    after_first = tree_bytes(work)
                    _, repeated = run_migration(work, backup)
                    self.assertEqual(repeated["reason"], ["already migrated"])
                    self.assertEqual(tree_bytes(work), after_first)

    def test_modified_v1_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="modified-v1-migration-") as temporary:
            root = Path(temporary)
            v1 = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
            chain.run_assignment_chain(root / "seed", v1)
            work = root / "seed" / "work"
            profile = json.loads((work / "profile.json").read_text(encoding="utf-8"))
            profile["label"] += " customized"
            chain.write_json(work / "profile.json", profile)
            before = tree_bytes(work)
            process, report = run_migration(work, root / "backup", "--dry-run", check=False)
            self.assertEqual(process.returncode, 2)
            self.assertFalse(report["matched"])
            self.assertTrue(any("frozen historical V1" in reason for reason in report["reason"]))
            self.assertEqual(tree_bytes(work), before)
            self.assertFalse((root / "backup").exists())

    def test_backup_creation_race_preserves_competing_backup_and_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="migration-backup-race-") as temporary:
            root = Path(temporary)
            work = self.seed_v1_work(root)
            before = tree_bytes(work)
            backup = root / "backup"
            sentinel = backup / "manifest.json"
            original = migration_module.load_adapter_source_evidence
            injected = False

            def create_competing_backup(*args: object, **kwargs: object) -> object:
                nonlocal injected
                result = original(*args, **kwargs)
                if not injected:
                    backup.mkdir()
                    sentinel.write_bytes(b"competing migration\n")
                    injected = True
                return result

            with mock.patch.object(
                migration_module,
                "load_adapter_source_evidence",
                side_effect=create_competing_backup,
            ):
                with self.assertRaises(MigrationFailed) as raised:
                    migration_module.migrate_profile(work, backup)

            report = raised.exception.report
            self.assertEqual(report["failed_stage"], "create-backup")
            self.assertEqual(report["backup"]["status"], "incomplete-or-unavailable")
            self.assertEqual(set(report["upstream_state"].values()), {"old"})
            self.assertEqual(sentinel.read_bytes(), b"competing migration\n")
            self.assertEqual(tree_bytes(work), before)

    def test_post_create_backup_file_and_directory_races_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="migration-post-create-race-") as temporary:
            root = Path(temporary)
            seed = self.seed_v1_work(root)
            real_create = migration_module.create_artifact_directory_exclusive

            file_work = root / "file-race" / "work"
            shutil.copytree(seed, file_work)
            file_before = tree_bytes(file_work)
            file_backup = root / "file-race-backup"
            file_sentinel = file_backup / "manifest.json"

            def create_then_add_file(*args: object, **kwargs: object) -> object:
                created = real_create(*args, **kwargs)
                (created.path / "manifest.json").write_bytes(b"competing backup\n")
                return created

            with mock.patch.object(
                migration_module,
                "create_artifact_directory_exclusive",
                side_effect=create_then_add_file,
            ):
                with self.assertRaises(MigrationFailed):
                    migration_module.migrate_profile(file_work, file_backup)

            self.assertEqual(file_sentinel.read_bytes(), b"competing backup\n")
            self.assertEqual(tree_bytes(file_work), file_before)

            directory_work = root / "directory-race" / "work"
            shutil.copytree(seed, directory_work)
            directory_before = tree_bytes(directory_work)
            directory_backup = root / "directory-race-backup"
            displaced_backup = root / "directory-race-created"
            replacement_sentinel = directory_backup / "intruder.txt"

            def create_then_replace_directory(
                *args: object, **kwargs: object
            ) -> object:
                created = real_create(*args, **kwargs)
                created.path.rename(displaced_backup)
                created.path.mkdir()
                (created.path / "intruder.txt").write_bytes(b"replacement directory\n")
                return created

            with mock.patch.object(
                migration_module,
                "create_artifact_directory_exclusive",
                side_effect=create_then_replace_directory,
            ):
                with self.assertRaises(MigrationFailed):
                    migration_module.migrate_profile(directory_work, directory_backup)

            self.assertEqual(replacement_sentinel.read_bytes(), b"replacement directory\n")
            self.assertEqual(
                sorted(path.name for path in directory_backup.iterdir()),
                ["intruder.txt"],
            )
            self.assertFalse(any(displaced_backup.iterdir()))
            self.assertEqual(tree_bytes(directory_work), directory_before)

    def test_real_migration_reports_each_write_phase_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="migration-failures-") as temporary:
            root = Path(temporary)
            seed = self.seed_v1_work(root)
            cases = (
                ("prepared", "prepare-publication", None, {name: "old" for name in UPSTREAM}),
                ("invalidated", "invalidate", None, {name: "old" for name in UPSTREAM}),
                (
                    "manifest-replace",
                    "publish:manifest.json",
                    "manifest.json",
                    {name: "old" for name in UPSTREAM},
                ),
                (
                    "manifest",
                    "publish:manifest.json",
                    "manifest.json",
                    {"manifest.json": "new", "profile.json": "old", "document-ir.json": "old"},
                ),
                (
                    "profile",
                    "publish:profile.json",
                    "profile.json",
                    {"manifest.json": "new", "profile.json": "new", "document-ir.json": "old"},
                ),
                (
                    "document-ir",
                    "publish:document-ir.json",
                    "document-ir.json",
                    {name: "new" for name in UPSTREAM},
                ),
            )

            for label, failed_stage, publish_name, expected_state in cases:
                with self.subTest(stage=label):
                    work = root / label / "work"
                    shutil.copytree(seed, work)
                    original_bytes = {
                        name: (work / name).read_bytes() for name in UPSTREAM
                    }
                    backup = root / f"{label}-backup"
                    original_write = migration_module.atomic_write_bytes
                    original_invalidate = migration_module._invalidate

                    def interrupted_write(
                        path: object, payload: object, **kwargs: object
                    ) -> Path:
                        target = Path(path)
                        if (
                            label == "prepared"
                            and target.name == "manifest.json"
                            and target.parent.name.startswith("migration-prepared-")
                        ):
                            raise ArtifactSafetyError("simulated prepared write failure")
                        if (
                            label == "manifest-replace"
                            and target == work / "manifest.json"
                        ):
                            raise ArtifactSafetyError("simulated replace failure")
                        published = original_write(path, payload, **kwargs)
                        if publish_name and target == work / publish_name:
                            raise ArtifactSafetyError(
                                "simulated fsync failure after replace"
                            )
                        return published

                    def interrupted_invalidation(value: Path) -> None:
                        original_invalidate(value)
                        raise ArtifactSafetyError("simulated invalidation interruption")

                    patches = [
                        mock.patch.object(
                            migration_module,
                            "atomic_write_bytes",
                            side_effect=interrupted_write,
                        )
                    ]
                    if label == "invalidated":
                        patches.append(
                            mock.patch.object(
                                migration_module,
                                "_invalidate",
                                side_effect=interrupted_invalidation,
                            )
                        )
                    for patcher in patches:
                        patcher.start()
                    try:
                        with self.assertRaises(MigrationFailed) as raised:
                            migration_module.migrate_profile(work, backup)
                    finally:
                        for patcher in reversed(patches):
                            patcher.stop()

                    report = raised.exception.report
                    self.assertEqual(report["failed_stage"], failed_stage)
                    self.assertEqual(report["backup"]["status"], "complete")
                    self.assertEqual(report["upstream_state"], expected_state)
                    for name, data in original_bytes.items():
                        self.assertEqual((backup / name).read_bytes(), data)

                    if label == "prepared":
                        self.assertEqual(report["gate_state"]["source-audit.json"], "present")
                        self.assertEqual(report["gate_state"]["output/"], "present")
                        self.assertEqual(report["gate_state"]["translation/"], "present")
                    else:
                        self.assertEqual(report["gate_state"]["source-audit.json"], "missing")
                        self.assertEqual(report["gate_state"]["output/"], "missing")
                        self.assertEqual(report["gate_state"]["translation/"], "missing")

                    if label == "profile":
                        for name in UPSTREAM:
                            (work / name).write_bytes((backup / name).read_bytes())
                        chain._run_pipeline_stage("source-audit", work)
                        self.assertEqual(
                            chain.read_json(work / "source-audit.json")["status"],
                            "passed",
                        )

    def test_cli_prints_structured_recovery_report(self) -> None:
        expected = {"status": "failed", "failed_stage": "publish:profile.json"}
        arguments = [
            "pipeline.py",
            "migrate-profile",
            "work",
            "--backup",
            "backup",
        ]
        with mock.patch.object(sys, "argv", arguments), mock.patch.object(
            pipeline_module,
            "migrate_profile",
            side_effect=MigrationFailed(expected),
        ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as raised:
                pipeline_module.main()
        self.assertEqual(raised.exception.code, 3)
        self.assertEqual(json.loads(output.getvalue()), expected)


if __name__ == "__main__":
    unittest.main()
