#!/usr/bin/env python3
"""Focused cross-platform regressions for safe generated-artifact I/O."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

import safe_artifacts  # noqa: E402
from safe_artifacts import (  # noqa: E402
    ArtifactSafetyError,
    artifact_paths_same_entry,
    artifact_size,
    atomic_copy_file,
    atomic_publish_with_writer,
    atomic_write_bytes,
    atomic_write_text,
    clear_artifact_directory,
    inspect_artifact_file,
    lexical_absolute_path,
    lexical_paths_overlap,
    portable_artifact_basename,
    prepare_artifact_directory,
    read_artifact_bytes,
    read_artifact_text,
    recheck_artifact_file,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)


class SafeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="safe-artifacts-test-")
        self.temp = Path(self.temporary.name)
        self.boundary = self.temp / "work"
        prepare_artifact_directory(self.boundary)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lexical_absolute_paths_and_boundary_are_fail_closed(self) -> None:
        candidate = self.boundary / "missing" / ".." / "artifact.txt"
        self.assertEqual(
            lexical_absolute_path(candidate), self.boundary / "artifact.txt"
        )
        outside = self.temp / "outside.txt"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(ArtifactSafetyError, "outside its boundary"):
            atomic_write_bytes(outside, b"changed", boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "outside its boundary"):
            remove_artifact_file(outside, boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "outside its boundary"):
            prepare_artifact_directory(
                self.temp / "outside-directory", boundary=self.boundary
            )
        with self.assertRaisesRegex(ArtifactSafetyError, "outside its boundary"):
            clear_artifact_directory(self.temp, boundary=self.boundary)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_work_relative_paths_reject_windows_aliases_and_devices(self) -> None:
        rejected = (
            "artifact.json ",
            "artifact.json.",
            "nested/NUL",
            "nested/nul .txt",
            "CONIN$",
            "CONOUT$.json",
            "COM0",
            "LPT0.txt",
            "COM¹.bin",
            "LPT³",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ArtifactSafetyError, "Windows-(?:trimmed|reserved)"
                ):
                    work_relative_artifact_path(self.boundary, value)
        self.assertEqual(
            work_relative_artifact_path(
                self.boundary, "adapter-inputs/content.json"
            ),
            self.boundary / "adapter-inputs" / "content.json",
        )

    def test_portable_basename_and_lexical_overlap_helpers(self) -> None:
        self.assertEqual(portable_artifact_basename("study-v2.3"), "study-v2.3")
        for value in ("", ".", "..", "nested/name", r"nested\name", "NUL", "COM1.pdf"):
            with self.subTest(value=value):
                with self.assertRaises(ArtifactSafetyError):
                    portable_artifact_basename(value)

        child = self.boundary / "output" / "document.pdf"
        sibling = self.temp / "outside" / "document.pdf"
        self.assertTrue(lexical_paths_overlap(self.boundary, child))
        self.assertTrue(lexical_paths_overlap(child, self.boundary))
        self.assertFalse(lexical_paths_overlap(self.boundary, sibling))

    def test_physical_overlap_detects_case_and_unicode_directory_aliases(self) -> None:
        case_directory = self.temp / "CaseWork"
        case_source = case_directory / "renders" / "source.pdf"
        case_source.parent.mkdir(parents=True)
        case_source.write_bytes(b"case-alias-source\n")
        case_alias = self.temp / "casework"

        nfc_name = unicodedata.normalize("NFC", "caf\u00e9-work")
        nfd_name = unicodedata.normalize("NFD", nfc_name)
        self.assertNotEqual(nfc_name, nfd_name)
        unicode_directory = self.temp / nfc_name
        unicode_directory.mkdir()
        unicode_profile = unicode_directory / "profile.json"
        unicode_profile.write_bytes(b"unicode-alias-profile\n")
        unicode_alias = self.temp / nfd_name

        sensitive_directory = self.temp / "SensitiveWork"
        sensitive_directory.mkdir()
        sensitive_backing = self.temp / "sensitive-backing"
        sensitive_backing.mkdir()
        sensitive_alias = self.temp / "sensitivework"

        real_lstat = os.lstat
        aliases = (
            (lexical_absolute_path(case_alias), lexical_absolute_path(case_directory)),
            (
                lexical_absolute_path(unicode_alias),
                lexical_absolute_path(unicode_directory),
            ),
            (
                lexical_absolute_path(sensitive_alias),
                lexical_absolute_path(sensitive_backing),
            ),
        )

        def aliasing_lstat(value: object) -> os.stat_result:
            candidate = lexical_absolute_path(os.fspath(value))
            for alias, target in aliases:
                candidate_text = os.fspath(candidate)
                alias_text = os.fspath(alias)
                if candidate_text == alias_text:
                    candidate = target
                    break
                prefix = alias_text + os.sep
                if candidate_text.startswith(prefix):
                    candidate = target / candidate_text[len(prefix) :]
                    break
            return real_lstat(candidate)

        with mock.patch.object(
            safe_artifacts.os, "lstat", side_effect=aliasing_lstat
        ):
            self.assertTrue(lexical_paths_overlap(case_source, case_alias))
            self.assertTrue(
                artifact_paths_same_entry(case_directory, case_alias)
            )
            self.assertTrue(
                lexical_paths_overlap(unicode_profile, unicode_alias)
            )
            self.assertTrue(
                artifact_paths_same_entry(unicode_directory, unicode_alias)
            )
            self.assertFalse(
                lexical_paths_overlap(sensitive_directory, sensitive_alias)
            )

        missing = self.temp / "missing-entry"
        self.assertFalse(artifact_paths_same_entry(missing, missing))

    def test_directory_validation_rejects_a_regular_file_component(self) -> None:
        file_path = self.boundary / "not-a-directory"
        file_path.write_bytes(b"file")
        with self.assertRaisesRegex(ArtifactSafetyError, "must be a directory"):
            validate_artifact_directory(file_path, boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "must be a directory"):
            prepare_artifact_directory(
                file_path / "child", boundary=self.boundary
            )

    def test_tree_validation_is_read_only_and_supports_missing_roots(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated" / "nested", boundary=self.boundary
        ).parent
        artifact = generated / "nested" / "artifact.txt"
        artifact.write_bytes(b"preserve")
        self.assertEqual(
            validate_artifact_tree(generated, self.boundary), generated
        )
        self.assertEqual(artifact.read_bytes(), b"preserve")

        missing = self.boundary / "missing"
        self.assertIsNone(validate_artifact_tree(missing, self.boundary))
        with self.assertRaisesRegex(ArtifactSafetyError, "does not exist"):
            validate_artifact_tree(
                missing, self.boundary, allow_missing=False
            )

    def test_prepare_validate_atomic_read_replace_and_remove(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated" / "nested", boundary=self.boundary
        )
        self.assertEqual(
            validate_artifact_directory(generated, boundary=self.boundary), generated
        )
        target = generated / "artifact.txt"
        atomic_write_text(target, "first\n", boundary=self.boundary)
        self.assertEqual(
            read_artifact_text(target, boundary=self.boundary), "first\n"
        )
        atomic_write_bytes(target, b"second\n", boundary=self.boundary)
        self.assertEqual(
            read_artifact_bytes(target, boundary=self.boundary), b"second\n"
        )
        self.assertEqual(validate_artifact_file(target, boundary=self.boundary), target)
        self.assertFalse(list(generated.glob(f".{target.name}-*.tmp")))
        remove_artifact_file(target, boundary=self.boundary, missing_ok=False)
        self.assertFalse(target.exists())
        remove_artifact_file(target, boundary=self.boundary)

    def test_atomic_replace_failure_preserves_original_and_cleans_temp(self) -> None:
        target = self.boundary / "artifact.bin"
        atomic_write_bytes(target, b"original", boundary=self.boundary)
        with mock.patch.object(
            safe_artifacts.os, "replace", side_effect=OSError("injected failure")
        ):
            with self.assertRaisesRegex(ArtifactSafetyError, "cannot publish"):
                atomic_write_bytes(target, b"replacement", boundary=self.boundary)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(list(self.boundary.glob(f".{target.name}-*.tmp")))

    def test_snapshot_read_and_publish_reject_target_version_changes(self) -> None:
        target = self.boundary / "versioned.bin"
        target.write_bytes(b"first")
        snapshot = inspect_artifact_file(target, boundary=self.boundary)
        recheck_artifact_file(snapshot)
        self.assertEqual(
            read_artifact_bytes(
                target, boundary=self.boundary, expected=snapshot
            ),
            b"first",
        )

        preserved = self.temp / "preserved.bin"
        target.replace(preserved)
        target.write_bytes(b"intruder")
        for operation in (
            lambda: recheck_artifact_file(snapshot),
            lambda: read_artifact_bytes(
                target, boundary=self.boundary, expected=snapshot
            ),
            lambda: atomic_write_bytes(
                target, b"replacement", boundary=self.boundary, expected=snapshot
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ArtifactSafetyError, "changed after inspection"):
                    operation()
        self.assertEqual(preserved.read_bytes(), b"first")
        self.assertEqual(target.read_bytes(), b"intruder")
        self.assertFalse(list(self.boundary.glob(".versioned.bin-*.tmp")))

    def test_missing_snapshot_rejects_target_appearance(self) -> None:
        target = self.boundary / "appeared.bin"
        snapshot = inspect_artifact_file(
            target, boundary=self.boundary, allow_missing=True
        )
        self.assertFalse(snapshot.exists)
        target.write_bytes(b"intruder")
        with self.assertRaisesRegex(ArtifactSafetyError, "appeared after inspection"):
            atomic_write_bytes(
                target, b"replacement", boundary=self.boundary, expected=snapshot
            )
        self.assertEqual(target.read_bytes(), b"intruder")
        self.assertFalse(list(self.boundary.glob(".appeared.bin-*.tmp")))

    def test_existing_snapshot_rejects_target_disappearance(self) -> None:
        target = self.boundary / "disappeared.bin"
        target.write_bytes(b"original")
        snapshot = inspect_artifact_file(target, boundary=self.boundary)
        preserved = self.temp / "preserved-disappeared.bin"
        target.replace(preserved)

        with self.assertRaisesRegex(ArtifactSafetyError, "changed after inspection"):
            atomic_write_bytes(
                target, b"replacement", boundary=self.boundary, expected=snapshot
            )
        self.assertFalse(target.exists())
        self.assertEqual(preserved.read_bytes(), b"original")
        self.assertFalse(list(self.boundary.glob(".disappeared.bin-*.tmp")))

    def test_publish_rechecks_target_after_temporary_fsync(self) -> None:
        target = self.boundary / "late-change.bin"
        target.write_bytes(b"original")
        preserved = self.temp / "preserved-late-change.bin"
        real_finish = safe_artifacts._finish_temporary

        def change_after_finish(*args: object, **kwargs: object) -> os.stat_result:
            finished = real_finish(*args, **kwargs)
            target.replace(preserved)
            target.write_bytes(b"intruder")
            return finished

        with mock.patch.object(
            safe_artifacts, "_finish_temporary", side_effect=change_after_finish
        ):
            with self.assertRaisesRegex(ArtifactSafetyError, "changed before publication"):
                atomic_write_bytes(target, b"replacement", boundary=self.boundary)
        self.assertEqual(preserved.read_bytes(), b"original")
        self.assertEqual(target.read_bytes(), b"intruder")
        self.assertFalse(list(self.boundary.glob(".late-change.bin-*.tmp")))

    def test_publish_rejects_temporary_identity_or_hardlink_change(self) -> None:
        target = self.boundary / "temporary-change.bin"
        target.write_bytes(b"original")
        outside = self.temp / "outside.bin"
        outside.write_bytes(b"outside")
        outside_link = self.temp / "outside-link.bin"
        os.link(outside, outside_link)
        outside_status = os.lstat(outside)
        real_status = safe_artifacts._artifact_file_status

        def spoof_temporary(path: Path) -> os.stat_result | None:
            if path.parent == self.boundary and path.name.startswith(
                ".temporary-change.bin-"
            ):
                return outside_status
            return real_status(path)

        with mock.patch.object(
            safe_artifacts, "_artifact_file_status", side_effect=spoof_temporary
        ):
            with self.assertRaisesRegex(ArtifactSafetyError, "temporary artifact changed"):
                atomic_write_bytes(target, b"replacement", boundary=self.boundary)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(outside_link.read_bytes(), b"outside")
        self.assertFalse(list(self.boundary.glob(".temporary-change.bin-*.tmp")))

    def test_snapshot_rejects_parent_identity_change(self) -> None:
        nested = self.boundary / "nested"
        nested.mkdir()
        target = nested / "artifact.bin"
        target.write_bytes(b"original")
        snapshot = inspect_artifact_file(target, boundary=self.boundary)
        moved = self.temp / "moved-nested"
        nested.replace(moved)
        nested.mkdir()
        intruder = nested / "artifact.bin"
        intruder.write_bytes(b"intruder")

        with self.assertRaisesRegex(ArtifactSafetyError, "directory identity changed"):
            atomic_write_bytes(
                target, b"replacement", boundary=self.boundary, expected=snapshot
            )
        self.assertEqual((moved / "artifact.bin").read_bytes(), b"original")
        self.assertEqual(intruder.read_bytes(), b"intruder")
        self.assertFalse(list(nested.glob(".artifact.bin-*.tmp")))

    def test_stable_read_rejects_disappearance_while_opening_or_reading(self) -> None:
        target = self.boundary / "read-race.bin"
        target.write_bytes(b"stable bytes")
        real_status = safe_artifacts._artifact_file_status

        for missing_call, phrase in ((2, "while opening"), (3, "while reading")):
            with self.subTest(phase=phrase):
                calls = 0

                def disappear(path: Path) -> os.stat_result | None:
                    nonlocal calls
                    if path == target:
                        calls += 1
                        if calls == missing_call:
                            return None
                    return real_status(path)

                with mock.patch.object(
                    safe_artifacts,
                    "_artifact_file_status",
                    side_effect=disappear,
                ):
                    with self.assertRaisesRegex(ArtifactSafetyError, phrase):
                        read_artifact_bytes(target, boundary=self.boundary)
                self.assertEqual(target.read_bytes(), b"stable bytes")

    def test_short_writes_complete_and_no_progress_preserves_original(self) -> None:
        target = self.boundary / "short-write.bin"
        target.write_bytes(b"original")
        payload = b"replacement-payload"
        real_write = os.write

        def short_write(descriptor: int, data: object) -> int:
            view = memoryview(data)
            return real_write(descriptor, view[: max(1, len(view) // 2)])

        with mock.patch.object(safe_artifacts.os, "write", side_effect=short_write):
            atomic_write_bytes(target, payload, boundary=self.boundary)
        self.assertEqual(target.read_bytes(), payload)

        with mock.patch.object(safe_artifacts.os, "write", return_value=0):
            with self.assertRaisesRegex(ArtifactSafetyError, "made no progress"):
                atomic_write_bytes(target, b"never-published", boundary=self.boundary)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(list(self.boundary.glob(".short-write.bin-*.tmp")))

    def test_fsync_failure_preserves_original_and_cleans_temp(self) -> None:
        target = self.boundary / "fsync.bin"
        target.write_bytes(b"original")
        with mock.patch.object(
            safe_artifacts.os, "fsync", side_effect=OSError("injected fsync failure")
        ):
            with self.assertRaisesRegex(ArtifactSafetyError, "cannot publish"):
                atomic_write_bytes(target, b"replacement", boundary=self.boundary)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertFalse(list(self.boundary.glob(".fsync.bin-*.tmp")))

    def test_atomic_copy_hash_and_size_use_validated_streams(self) -> None:
        source = self.temp / "source.bin"
        payload = (b"streamed-artifact\x00" * 140_000) + b"tail"
        source.write_bytes(payload)
        target = self.boundary / "copied.bin"
        atomic_copy_file(source, target, boundary=self.boundary)
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(artifact_size(target, boundary=self.boundary), len(payload))
        self.assertEqual(
            sha256_artifact(target, boundary=self.boundary),
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertFalse(list(self.boundary.glob(".copied.bin-*.tmp")))

    def test_writer_preserves_suffix_and_failure_preserves_original(self) -> None:
        target = self.boundary / "image.png"
        atomic_write_bytes(target, b"original", boundary=self.boundary)
        observed: list[Path] = []

        def successful_writer(temporary: Path) -> None:
            observed.append(temporary)
            self.assertEqual(temporary.parent, target.parent)
            self.assertEqual(temporary.suffix, ".png")
            temporary.write_bytes(b"generated-image")

        atomic_publish_with_writer(
            target, successful_writer, boundary=self.boundary
        )
        self.assertEqual(target.read_bytes(), b"generated-image")
        self.assertEqual(len(observed), 1)

        def failing_writer(temporary: Path) -> None:
            self.assertEqual(temporary.suffix, ".png")
            temporary.write_bytes(b"partial")
            raise RuntimeError("injected writer failure")

        with self.assertRaisesRegex(RuntimeError, "injected writer failure"):
            atomic_publish_with_writer(
                target, failing_writer, boundary=self.boundary
            )
        self.assertEqual(target.read_bytes(), b"generated-image")
        self.assertFalse(list(self.boundary.glob(".image-*.png")))

    def test_hardlinks_are_rejected(self) -> None:
        target = self.boundary / "target.txt"
        target.write_bytes(b"protected")
        hardlink = self.boundary / "hardlink.txt"
        os.link(target, hardlink)
        for path in (target, hardlink):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ArtifactSafetyError, "hard-linked"):
                    validate_artifact_file(path, boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "hard-linked"):
            atomic_write_bytes(target, b"changed", boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "hard-linked"):
            validate_artifact_tree(self.boundary, self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "hard-linked"):
            clear_artifact_directory(self.boundary, boundary=self.boundary)
        self.assertEqual(target.read_bytes(), b"protected")
        self.assertEqual(hardlink.read_bytes(), b"protected")

    def test_directory_ancestor_symlink_is_rejected(self) -> None:
        outside = self.temp / "outside-directory"
        outside.mkdir()
        (outside / "sentinel.txt").write_bytes(b"outside")
        linked = self.boundary / "linked-directory"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ArtifactSafetyError, "symbolic links"):
            validate_artifact_file(linked / "sentinel.txt", boundary=self.boundary)
        self.assertEqual((outside / "sentinel.txt").read_bytes(), b"outside")

    def test_broken_symlink_is_recognized_with_lstat(self) -> None:
        broken = self.boundary / "broken.txt"
        try:
            os.symlink(self.temp / "missing-target", broken)
        except OSError as exc:
            self.skipTest(f"file symlinks unavailable: {exc}")
        self.assertFalse(broken.exists())
        self.assertTrue(os.path.lexists(broken))
        with self.assertRaisesRegex(ArtifactSafetyError, "symbolic links"):
            validate_artifact_file(
                broken, boundary=self.boundary, allow_missing=True
            )

    def test_clear_preflights_entire_tree_before_removing_anything(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated", boundary=self.boundary
        )
        keep = generated / "keep.txt"
        keep.write_bytes(b"keep")
        outside = self.temp / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"outside")
        linked = generated / "escape"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaisesRegex(ArtifactSafetyError, "symbolic links"):
            validate_artifact_tree(generated, self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "symbolic links"):
            clear_artifact_directory(generated, boundary=self.boundary)
        self.assertEqual(keep.read_bytes(), b"keep")
        self.assertEqual(sentinel.read_bytes(), b"outside")

    def test_clear_rechecks_complete_snapshot_before_first_removal(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated", boundary=self.boundary
        )
        first = generated / "aaa-first.txt"
        later = generated / "zzz-later.txt"
        first.write_bytes(b"first")
        later.write_bytes(b"later")
        outside = self.temp / "outside.txt"
        outside.write_bytes(b"outside")
        original_snapshot = safe_artifacts._snapshot_tree

        def replace_later_after_snapshot(root: Path, identities: tuple) -> tuple:
            result = original_snapshot(root, identities)
            later.unlink()
            os.link(outside, later)
            return result

        with mock.patch.object(
            safe_artifacts,
            "_snapshot_tree",
            side_effect=replace_later_after_snapshot,
        ):
            with self.assertRaisesRegex(ArtifactSafetyError, "hard-linked"):
                clear_artifact_directory(generated, boundary=self.boundary)
        self.assertEqual(first.read_bytes(), b"first")
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_clear_contents_and_remove_generated_directory(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated" / "nested", boundary=self.boundary
        ).parent
        atomic_write_bytes(
            generated / "top.bin", b"top", boundary=self.boundary
        )
        atomic_write_bytes(
            generated / "nested" / "child.bin", b"child", boundary=self.boundary
        )
        clear_artifact_directory(generated, boundary=self.boundary)
        self.assertTrue(generated.is_dir())
        self.assertEqual(list(generated.iterdir()), [])

        prepare_artifact_directory(generated / "again", boundary=self.boundary)
        clear_artifact_directory(
            generated, boundary=self.boundary, remove_directory=True
        )
        self.assertFalse(generated.exists())
        self.assertTrue(self.boundary.is_dir())
        with self.assertRaisesRegex(ArtifactSafetyError, "boundary itself"):
            clear_artifact_directory(
                self.boundary,
                boundary=self.boundary,
                remove_directory=True,
            )

    @unittest.skipIf(os.name == "nt", "POSIX nested-symlink swap regression")
    def test_clear_rechecks_every_nested_parent_before_deletion(self) -> None:
        generated = prepare_artifact_directory(
            self.boundary / "generated" / "nested", boundary=self.boundary
        ).parent
        nested = generated / "nested"
        artifact = nested / "artifact.txt"
        artifact.write_bytes(b"preserve")
        moved = self.temp / "moved-outside-boundary"
        original_snapshot = safe_artifacts._snapshot_tree

        def swap_after_snapshot(root: Path, identities: tuple) -> tuple:
            result = original_snapshot(root, identities)
            nested.rename(moved)
            os.symlink(moved, nested, target_is_directory=True)
            return result

        try:
            with mock.patch.object(
                safe_artifacts, "_snapshot_tree", side_effect=swap_after_snapshot
            ):
                with self.assertRaisesRegex(ArtifactSafetyError, "symbolic links"):
                    clear_artifact_directory(generated, boundary=self.boundary)
            self.assertEqual((moved / "artifact.txt").read_bytes(), b"preserve")
        finally:
            if os.path.lexists(nested):
                nested.unlink()
            if moved.exists():
                moved.rename(nested)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_non_regular_fifo_is_rejected_without_opening(self) -> None:
        fifo = self.boundary / "artifact.fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ArtifactSafetyError, "regular file"):
            validate_artifact_file(fifo, boundary=self.boundary)
        with self.assertRaisesRegex(ArtifactSafetyError, "regular file"):
            clear_artifact_directory(self.boundary, boundary=self.boundary)

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_windows_junction_is_rejected_without_traversal(self) -> None:
        outside = self.temp / "junction-target"
        outside.mkdir()
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"outside")
        keep = self.boundary / "keep.txt"
        keep.write_bytes(b"keep")
        junction = self.boundary / "junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
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
            with self.assertRaisesRegex(ArtifactSafetyError, "reparse points"):
                validate_artifact_directory(junction, boundary=self.boundary)
            with self.assertRaisesRegex(ArtifactSafetyError, "reparse points"):
                validate_artifact_tree(self.boundary, self.boundary)
            with self.assertRaisesRegex(ArtifactSafetyError, "reparse points"):
                clear_artifact_directory(self.boundary, boundary=self.boundary)
            self.assertEqual(sentinel.read_bytes(), b"outside")
            self.assertEqual(keep.read_bytes(), b"keep")
        finally:
            if os.path.lexists(junction):
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
