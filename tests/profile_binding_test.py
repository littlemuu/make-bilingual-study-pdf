#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import profile as profile_module
import safe_artifacts


class ProfileBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="profile-binding-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_work(self, name: str = "work") -> Path:
        work_dir = self.root / name
        work_dir.mkdir()
        return work_dir

    def test_new_task_binds_before_runtime_load_and_explicit_load_cannot_fallback(
        self,
    ) -> None:
        unbound = self.make_work("unbound")
        for reference in (None, "assignment-en-zh"):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(ValueError, "not bound to a Profile"):
                    profile_module.load_work_profile(unbound, reference)

        work_dir = self.make_work("bound")
        bound = profile_module.bind_profile(work_dir)
        self.assertEqual(bound["id"], "assignment-en-zh")
        self.assertEqual(
            profile_module.load_work_profile(work_dir)["id"],
            "assignment-en-zh",
        )
        self.assertEqual(
            profile_module.load_work_profile(work_dir, "assignment-en-zh")["id"],
            "assignment-en-zh",
        )
        self.assertEqual(list(work_dir.glob(".profile-*.tmp")), [])

    def test_each_frozen_artifact_requires_the_bound_copy(self) -> None:
        for index, artifact in enumerate(profile_module.FROZEN_WORK_ARTIFACTS):
            with self.subTest(artifact=artifact):
                work_dir = self.make_work(f"frozen-{index}")
                (work_dir / artifact).write_text("{}\n", encoding="utf-8")
                with mock.patch.object(
                    profile_module,
                    "load_profile",
                    side_effect=AssertionError("installed fallback must not be read"),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "frozen work directory is missing profile.json"
                    ):
                        profile_module.load_work_profile(
                            work_dir, "assignment-en-zh"
                        )

    def test_safe_force_recovery_is_atomic_and_no_force_preserves_bytes(self) -> None:
        work_dir = self.make_work()
        profile_module.bind_profile(work_dir, "assignment-en-zh")
        profile_path = work_dir / "profile.json"
        original = profile_path.read_bytes()

        with self.assertRaisesRegex(ValueError, "different profile"):
            profile_module.bind_profile(work_dir, "academic-paper-en-zh")
        self.assertEqual(profile_path.read_bytes(), original)

        real_replace = os.replace
        with mock.patch.object(
            profile_module.os, "replace", wraps=real_replace
        ) as replace_spy:
            recovered = profile_module.bind_profile(
                work_dir, "academic-paper-en-zh", force=True
            )
        replace_spy.assert_called_once()
        self.assertEqual(recovered["id"], "academic-paper-en-zh")
        published = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(published["id"], recovered["id"])
        self.assertTrue(stat.S_ISREG(os.lstat(profile_path).st_mode))
        self.assertEqual(os.lstat(profile_path).st_nlink, 1)
        self.assertEqual(list(work_dir.glob(".profile-*.tmp")), [])

        profile_path.write_bytes(b"not json\n")
        corrupted = profile_path.read_bytes()
        with self.assertRaises(ValueError):
            profile_module.bind_profile(work_dir, "assignment-en-zh")
        self.assertEqual(profile_path.read_bytes(), corrupted)
        profile_module.bind_profile(work_dir, "assignment-en-zh", force=True)
        self.assertEqual(
            profile_module.load_work_profile(work_dir)["id"], "assignment-en-zh"
        )

    def test_profile_symlink_never_reads_or_rewrites_external_target(self) -> None:
        outside = self.root / "outside-profile.json"
        outside.write_bytes(b"outside bytes must remain unchanged\n")
        work_dir = self.make_work()
        link = work_dir / "profile.json"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"symbolic links unavailable: {exc}")

        for operation in (
            lambda: profile_module.load_work_profile(work_dir),
            lambda: profile_module.bind_profile(
                work_dir, "assignment-en-zh", force=False
            ),
            lambda: profile_module.bind_profile(
                work_dir, "assignment-en-zh", force=True
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    operation()
                self.assertEqual(
                    outside.read_bytes(), b"outside bytes must remain unchanged\n"
                )
                self.assertTrue(link.is_symlink())

    def test_profile_hardlink_never_reads_or_rewrites_external_target(self) -> None:
        outside = self.root / "outside-hardlink.json"
        outside.write_bytes(b"outside hardlink bytes\n")
        work_dir = self.make_work()
        link = work_dir / "profile.json"
        try:
            os.link(outside, link)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")

        for operation in (
            lambda: profile_module.load_work_profile(work_dir),
            lambda: profile_module.bind_profile(
                work_dir, "assignment-en-zh", force=True
            ),
            lambda: profile_module.bind_profile(work_dir, link, force=True),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "multiply linked"):
                    operation()
                self.assertEqual(outside.read_bytes(), b"outside hardlink bytes\n")
                self.assertEqual(link.read_bytes(), b"outside hardlink bytes\n")

    def test_document_ir_force_cli_cannot_rewrite_linked_profile_target(self) -> None:
        work_dir = self.make_work()
        (work_dir / "manifest.json").write_text("{}\n", encoding="utf-8")
        (work_dir / "blocks.jsonl").write_bytes(b"")
        outside = self.root / "outside-cli-profile.json"
        outside.write_bytes(b"outside CLI bytes must remain unchanged\n")
        profile_path = work_dir / "profile.json"
        link_kind = "symbolic"
        try:
            os.symlink(outside, profile_path)
        except OSError:
            link_kind = "hard"
            try:
                os.link(outside, profile_path)
            except OSError as exc:
                self.skipTest(f"profile links unavailable: {exc}")

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "document_ir.py"),
                str(work_dir),
                "--profile",
                "assignment-en-zh",
                "--force",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        expected_error = (
            "symbolic links" if link_kind == "symbolic" else "multiply linked"
        )
        self.assertIn(expected_error, completed.stderr)
        self.assertEqual(
            outside.read_bytes(), b"outside CLI bytes must remain unchanged\n"
        )
        self.assertFalse((work_dir / "document-ir.json").exists())

    def test_document_ir_manifest_mismatch_fails_before_binding(self) -> None:
        work_dir = self.make_work()
        academic = profile_module.load_profile("academic-paper-en-zh")
        manifest = {
            "profile": {
                "id": academic["id"],
                "sha256": profile_module.canonical_profile_sha256(academic),
            }
        }
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (work_dir / "blocks.jsonl").write_bytes(b"")
        original_manifest = manifest_path.read_bytes()

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "document_ir.py"),
                str(work_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn(
            "manifest is bound to a different profile",
            completed.stdout + completed.stderr,
        )
        self.assertFalse(os.path.lexists(work_dir / "profile.json"))
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual(list(work_dir.glob(".profile-*.tmp")), [])

    def test_nonregular_profile_entry_is_rejected_without_blocking(self) -> None:
        directory_work = self.make_work("directory-target")
        (directory_work / "profile.json").mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            profile_module.bind_profile(
                directory_work, "assignment-en-zh", force=True
            )

        if not hasattr(os, "mkfifo"):
            return
        fifo_work = self.make_work("fifo-target")
        os.mkfifo(fifo_work / "profile.json")
        with self.assertRaisesRegex(ValueError, "regular file"):
            profile_module.bind_profile(fifo_work, "assignment-en-zh", force=True)

    def test_load_profile_rejects_linked_broken_and_nonregular_paths(self) -> None:
        payload = (
            json.dumps(
                profile_module.load_profile("assignment-en-zh"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        outside = self.root / "outside-profile.json"
        outside.write_bytes(payload)
        self.assertEqual(profile_module.load_profile(outside)["id"], "assignment-en-zh")

        hardlink = self.root / "hardlink-profile.json"
        os.link(outside, hardlink)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            profile_module.load_profile(hardlink)
        self.assertEqual(outside.read_bytes(), payload)

        directory = self.root / "directory-profile.json"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            profile_module.load_profile(directory)

        target = self.root / "symlink-target.json"
        target.write_bytes(payload)
        symlink = self.root / "symlink-profile.json"
        broken = self.root / "broken-profile.json"
        with self.subTest(kind="symlink-and-broken-symlink"):
            try:
                os.symlink(target, symlink)
                os.symlink(self.root / "missing-profile.json", broken)
            except OSError as exc:
                self.skipTest(f"file symbolic links are unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                profile_module.load_profile(symlink)
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                profile_module.load_profile(broken)
            self.assertEqual(target.read_bytes(), payload)

    def test_work_internal_profile_override_is_rejected_before_mutation(self) -> None:
        work_dir = self.make_work("profile-role-work")
        manifest_path = work_dir / "manifest.json"
        blocks_path = work_dir / "blocks.jsonl"
        manifest_path.write_bytes(b"{}\n")
        blocks_path.write_bytes(b"")
        output = work_dir / "output"
        output.mkdir()
        custom_profile = output / "custom-profile.json"
        custom_profile.write_text(
            json.dumps(
                profile_module.load_profile("assignment-en-zh"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        expected = {
            path.relative_to(work_dir).as_posix(): path.read_bytes()
            for path in work_dir.rglob("*")
            if path.is_file()
        }

        with self.assertRaisesRegex(ValueError, "canonical WORK/profile.json"):
            profile_module.bind_profile(work_dir, custom_profile, force=True)
        with self.assertRaisesRegex(ValueError, "canonical WORK/profile.json"):
            profile_module.load_work_profile(work_dir, custom_profile)

        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "document_ir.py"),
                str(work_dir),
                "--profile",
                str(custom_profile),
                "--force",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("canonical WORK/profile.json", completed.stdout + completed.stderr)
        actual = {
            path.relative_to(work_dir).as_posix(): path.read_bytes()
            for path in work_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual, expected)
        self.assertFalse((work_dir / "profile.json").exists())
        self.assertFalse((work_dir / "document-ir.json").exists())

    def test_physical_work_alias_enforces_canonical_profile_identity(self) -> None:
        work_dir = self.make_work("CaseWork")
        output = work_dir / "output"
        output.mkdir()
        custom_profile = output / "custom-profile.json"
        custom_profile.write_bytes(b"custom profile sentinel\n")
        canonical_profile = work_dir / "profile.json"
        canonical_profile.write_bytes(b"canonical profile sentinel\n")
        alias_work = self.root / "casework"
        canonical_alias = alias_work / "PROFILE.JSON"
        real_lstat = os.lstat
        aliases = (
            (canonical_alias, canonical_profile),
            (alias_work, work_dir),
        )

        def aliasing_lstat(value: object) -> os.stat_result:
            candidate = Path(os.path.abspath(os.fspath(value)))
            candidate_text = os.fspath(candidate)
            for alias, target in aliases:
                alias_text = os.fspath(Path(os.path.abspath(alias)))
                if candidate_text == alias_text:
                    candidate = target
                    break
                prefix = alias_text + os.sep
                if candidate_text.startswith(prefix):
                    candidate = target / candidate_text[len(prefix) :]
                    break
            return real_lstat(candidate)

        with (
            mock.patch.object(safe_artifacts.os, "lstat", side_effect=aliasing_lstat),
            mock.patch.object(
                safe_artifacts.os.path, "normcase", side_effect=lambda value: value
            ),
        ):
            with self.assertRaisesRegex(ValueError, "canonical WORK/profile.json"):
                profile_module._validate_work_profile_reference(
                    alias_work, custom_profile
                )
            profile_module._validate_work_profile_reference(
                alias_work, canonical_alias
            )

        self.assertEqual(custom_profile.read_bytes(), b"custom profile sentinel\n")
        self.assertEqual(canonical_profile.read_bytes(), b"canonical profile sentinel\n")

    def test_work_directory_symlink_ancestor_is_rejected_before_external_write(
        self,
    ) -> None:
        outside_parent = self.root / "outside-parent"
        outside_parent.mkdir()
        outside_work = outside_parent / "work"
        outside_work.mkdir()
        linked_parent = self.root / "linked-parent"
        try:
            os.symlink(outside_parent, linked_parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symbolic links unavailable: {exc}")

        missing_outside_work = outside_parent / "missing-work"
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            profile_module.prepare_profile_work_directory(
                linked_parent / "missing-work"
            )
        self.assertFalse(missing_outside_work.exists())

        with self.assertRaisesRegex(ValueError, "symbolic links"):
            profile_module.bind_profile(
                linked_parent / "work", "assignment-en-zh", force=True
            )
        self.assertFalse((outside_work / "profile.json").exists())

    @unittest.skipUnless(os.name == "nt", "junction regressions are Windows-specific")
    def test_windows_profile_and_ancestor_junctions_are_rejected(self) -> None:
        target = self.root / "junction-target"
        target.mkdir()
        outside = target / "sentinel.txt"
        outside.write_bytes(b"outside junction bytes\n")
        work_dir = self.make_work("profile-junction-work")
        profile_junction = work_dir / "profile.json"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(profile_junction), str(target)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if created.returncode != 0:
            self.fail(
                "Windows junction regression must execute: "
                f"{created.stdout}{created.stderr}"
            )
        try:
            with self.assertRaisesRegex(ValueError, "reparse points"):
                profile_module.bind_profile(
                    work_dir, "assignment-en-zh", force=True
                )
            self.assertEqual(outside.read_bytes(), b"outside junction bytes\n")
        finally:
            if os.path.lexists(profile_junction):
                os.rmdir(profile_junction)

        outside_parent = self.root / "outside-junction-parent"
        outside_parent.mkdir()
        parent_junction = self.root / "parent-junction"
        created = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(parent_junction),
                str(outside_parent),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if created.returncode != 0:
            self.fail(
                "Windows junction regression must execute: "
                f"{created.stdout}{created.stderr}"
            )
        try:
            missing_outside_work = outside_parent / "missing-work"
            with self.assertRaisesRegex(ValueError, "reparse points"):
                profile_module.prepare_profile_work_directory(
                    parent_junction / "missing-work"
                )
            self.assertFalse(missing_outside_work.exists())

            outside_work = outside_parent / "work"
            outside_work.mkdir()
            with self.assertRaisesRegex(ValueError, "reparse points"):
                profile_module.bind_profile(
                    parent_junction / "work", "assignment-en-zh", force=True
                )
            self.assertFalse((outside_work / "profile.json").exists())
        finally:
            if os.path.lexists(parent_junction):
                os.rmdir(parent_junction)


if __name__ == "__main__":
    unittest.main(verbosity=2)
