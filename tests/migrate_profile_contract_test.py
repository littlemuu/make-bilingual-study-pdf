#!/usr/bin/env python3
"""Exercise the public work-package-B profile migration command."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path[:0] = [str(SCRIPTS), str(REPOSITORY / "tests")]

import v2_assignment_chain_diff_test as chain
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


if __name__ == "__main__":
    unittest.main()
