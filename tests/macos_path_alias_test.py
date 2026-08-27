#!/usr/bin/env python3
"""Real macOS regressions for case and Unicode filesystem aliases."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

import extract_pdf as extract_module  # noqa: E402
import profile as profile_module  # noqa: E402
from adapters import mineru as mineru_module  # noqa: E402
from adapters.base import AdapterError  # noqa: E402
from safe_artifacts import artifact_paths_same_entry, lexical_paths_overlap  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin", "real APFS aliases require macOS")
class MacOSPathAliasTests(unittest.TestCase):
    def assert_same_entry(self, canonical: Path, alias: Path) -> None:
        try:
            canonical_status = os.lstat(canonical)
            alias_status = os.lstat(alias)
        except OSError as exc:
            self.fail(f"macOS alias must resolve on the hosted APFS volume: {exc}")
        self.assertTrue(
            os.path.samestat(canonical_status, alias_status),
            f"macOS alias must identify the same entry: {canonical} != {alias}",
        )

    def run_extract(
        self, source: Path, work: Path, *extra: object
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "extract_pdf.py"),
                str(source),
                "--work-dir",
                str(work),
                *map(str, extra),
                "--force",
            ],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env=environment,
        )

    def test_case_alias_rejects_source_and_mineru_input_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="macos-case-alias-", dir=REPOSITORY
        ) as temporary:
            root = Path(temporary)
            canonical_work = root / "CaseWork"
            source = canonical_work / "renders" / "source.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"immutable case-alias source\n")
            manifest = canonical_work / "manifest.json"
            manifest.write_bytes(b"old manifest\n")
            alias_work = root / "casework"
            self.assert_same_entry(canonical_work, alias_work)
            self.assertTrue(lexical_paths_overlap(source, alias_work))

            completed = self.run_extract(source, alias_work)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("filesystem aliases", completed.stdout + completed.stderr)
            self.assertEqual(source.read_bytes(), b"immutable case-alias source\n")
            self.assertEqual(manifest.read_bytes(), b"old manifest\n")

            outside_source = root / "outside.pdf"
            outside_source.write_bytes(b"outside source\n")
            with self.assertRaisesRegex(AdapterError, "filesystem-disjoint"):
                mineru_module._preflight_import_input_overlap(
                    outside_source,
                    canonical_work,
                    alias_work,
                    "academic-paper-en-zh",
                )

    def test_nfd_alias_rejects_custom_profile_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="macos-nfd-alias-", dir=REPOSITORY
        ) as temporary:
            root = Path(temporary)
            nfc_name = unicodedata.normalize("NFC", "caf\u00e9-work")
            nfd_name = unicodedata.normalize("NFD", nfc_name)
            self.assertNotEqual(nfc_name, nfd_name)
            canonical_work = root / nfc_name
            profile = canonical_work / "output" / "custom-profile.json"
            profile.parent.mkdir(parents=True)
            profile.write_bytes(b'{"profile":"immutable"}\n')
            manifest = canonical_work / "manifest.json"
            manifest.write_bytes(b"old unicode manifest\n")
            alias_work = root / nfd_name
            self.assert_same_entry(canonical_work, alias_work)
            self.assertTrue(lexical_paths_overlap(profile, alias_work))
            source = root / "outside.pdf"
            source.write_bytes(b"outside source\n")

            completed = self.run_extract(source, alias_work, "--profile", profile)

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("filesystem aliases", completed.stdout + completed.stderr)
            self.assertEqual(profile.read_bytes(), b'{"profile":"immutable"}\n')
            self.assertEqual(manifest.read_bytes(), b"old unicode manifest\n")

            with self.assertRaisesRegex(ValueError, "canonical WORK/profile.json"):
                profile_module._validate_work_profile_reference(alias_work, profile)
            canonical_profile = canonical_work / "profile.json"
            canonical_profile.write_bytes(b'{"profile":"canonical"}\n')
            canonical_alias = alias_work / "PROFILE.JSON"
            self.assert_same_entry(canonical_profile, canonical_alias)
            self.assertTrue(
                artifact_paths_same_entry(canonical_profile, canonical_alias)
            )
            profile_module._validate_work_profile_reference(
                alias_work, canonical_alias
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
