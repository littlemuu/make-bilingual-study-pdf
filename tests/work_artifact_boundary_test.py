#!/usr/bin/env python3
"""Regression tests for fail-closed WORK artifact publication boundaries."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

sys.dont_write_bytecode = True

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
TESTS = REPOSITORY / "tests"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

from PIL import Image

from adapters.base import AdapterError
import adapters.mineru as mineru_module
from adapters.mineru import _prepare_work_dir
from audit_source import (  # noqa: E402
    current_source_audit_bindings,
    current_source_pdf_binding,
    validate_source_audit_binding,
)
from common import read_json, sha256_file, write_json
import document_ir as document_ir_module
from document_ir import expected_ir, migrate_work_dir
import extract_pdf as extract_pdf_module
from extract_pdf import prepare_output
from safe_artifacts import ArtifactSafetyError
import v23_ir_source_test as ir_source_fixtures


REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
TreeRecord = tuple[object, ...]


def _is_reparse_point(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE
    )


def _link_target(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _entry_record(relative: str, path: Path, status: os.stat_result) -> TreeRecord:
    is_link = stat.S_ISLNK(status.st_mode) or _is_reparse_point(status)
    if is_link:
        kind = "link"
        digest = None
        target = _link_target(path)
    elif stat.S_ISDIR(status.st_mode):
        kind = "directory"
        digest = None
        target = None
    elif stat.S_ISREG(status.st_mode):
        kind = "file"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        target = None
    else:
        kind = "other"
        digest = None
        target = None
    return (
        relative,
        kind,
        stat.S_IMODE(status.st_mode),
        status.st_dev,
        status.st_ino,
        status.st_nlink,
        status.st_size,
        getattr(status, "st_file_attributes", 0),
        getattr(status, "st_reparse_tag", 0),
        target,
        digest,
    )


def tree_snapshot(root: Path) -> tuple[TreeRecord, ...]:
    """Snapshot a tree without traversing any symlink or reparse point."""
    try:
        root_status = os.lstat(root)
    except FileNotFoundError:
        return ((".", "missing"),)

    records = [_entry_record(".", root, root_status)]
    if stat.S_ISLNK(root_status.st_mode) or _is_reparse_point(root_status):
        return tuple(records)
    if not stat.S_ISDIR(root_status.st_mode):
        return tuple(records)

    def walk(directory: Path, prefix: Path) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            path = Path(entry.path)
            status = entry.stat(follow_symlinks=False)
            relative_path = prefix / entry.name
            relative = relative_path.as_posix()
            records.append(_entry_record(relative, path, status))
            if (
                stat.S_ISDIR(status.st_mode)
                and not stat.S_ISLNK(status.st_mode)
                and not _is_reparse_point(status)
            ):
                walk(path, relative_path)

    walk(root, Path())
    return tuple(records)


class WorkArtifactBoundaryTests(unittest.TestCase):
    maxDiff = 6000

    def _make_file_link(
        self,
        kind: str,
        link: Path,
        target: Path,
        *,
        target_payload: bytes | None,
    ) -> None:
        if target_payload is not None:
            target.write_bytes(target_payload)
        if kind == "hardlink":
            try:
                os.link(target, link)
            except OSError as exc:
                self.fail(f"hard links must be exercised on this filesystem: {exc}")
            return
        try:
            link.symlink_to(target, target_is_directory=False)
        except OSError as exc:
            if os.name == "nt":
                self.skipTest(f"file symbolic links require Windows privilege: {exc}")
            raise

    def _make_directory_link(self, link: Path, target: Path) -> str:
        if os.name != "nt":
            link.symlink_to(target, target_is_directory=True)
            return "symbolic link"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            self.fail(
                "Windows junction regression must execute: "
                f"{completed.stdout}{completed.stderr}"
            )
        return "junction"

    def _remove_directory_link(self, link: Path) -> None:
        if not os.path.lexists(link):
            return
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()

    def _capture_failure(
        self, operation: Callable[[], object]
    ) -> BaseException | None:
        # SystemExit is part of the CLI preflight contract.
        try:
            operation()
        except BaseException as exc:
            return exc
        return None

    def _make_ir_work(self, root: Path) -> tuple[Path, dict, unittest.TestCase]:
        fixture_root = root / "fixture"
        fixture_root.mkdir()
        case = ir_source_fixtures.V23IrSourceTests(
            methodName="test_adapter_freeze_dispositions_assets_and_drift"
        )
        case.setUp()
        work_dir, manifest, _blocks = case.make_work(fixture_root)
        return work_dir, manifest, case

    def _make_auditable_work(self, root: Path) -> Path:
        work_dir, manifest, case = self._make_ir_work(root)
        (work_dir / "oracle.txt").write_text(
            "Title Abstract Introduction Body paragraph References\f",
            encoding="utf-8",
        )
        renders = work_dir / "renders"
        renders.mkdir(exist_ok=True)
        Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
        contacts = work_dir / "source-contact"
        contacts.mkdir()
        contact_path = contacts / "contact-001.png"
        Image.new("RGB", (2, 3), "white").save(contact_path)
        manifest["source_contact_sheets"] = [
            {
                "path": contact_path.relative_to(work_dir).as_posix(),
                "sha256": sha256_file(contact_path),
                "first_page": 1,
                "last_page": 1,
            }
        ]
        write_json(work_dir / "manifest.json", manifest)
        write_json(work_dir / "document-ir.json", expected_ir(work_dir, case.profile))
        return work_dir

    def test_extract_rejects_linked_manifest_before_mutation(self) -> None:
        for kind in ("hardlink", "symlink", "broken-symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-extract-{kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = root / "work"
                outside_dir = root / "outside"
                work_dir.mkdir()
                outside_dir.mkdir()
                outside = outside_dir / "manifest-target.json"
                self._make_file_link(
                    kind,
                    work_dir / "manifest.json",
                    outside,
                    target_payload=(
                        None if kind == "broken-symlink" else b"outside-manifest\n"
                    ),
                )
                work_before = tree_snapshot(work_dir)
                outside_before = tree_snapshot(outside_dir)

                error = self._capture_failure(
                    lambda: prepare_output(work_dir, force=True)
                )

                self.assertEqual(tree_snapshot(outside_dir), outside_before)
                self.assertEqual(tree_snapshot(work_dir), work_before)
                self.assertIsInstance(error, SystemExit)

    def test_extract_rejects_linked_renders_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-extract-renders-"
        ) as temporary:
            root = Path(temporary)
            work_dir = root / "work"
            outside_dir = root / "outside-renders"
            work_dir.mkdir()
            outside_dir.mkdir()
            (outside_dir / "page-1.png").write_bytes(b"outside-page\n")
            (outside_dir / "keep.bin").write_bytes(b"outside-keep\n")
            link = work_dir / "renders"
            link_kind = self._make_directory_link(link, outside_dir)
            work_before = tree_snapshot(work_dir)
            outside_before = tree_snapshot(outside_dir)

            try:
                error = self._capture_failure(
                    lambda: prepare_output(work_dir, force=True)
                )
                work_after = tree_snapshot(work_dir)
                outside_after = tree_snapshot(outside_dir)
            finally:
                self._remove_directory_link(link)

            self.assertEqual(outside_after, outside_before, link_kind)
            self.assertEqual(work_after, work_before, link_kind)
            self.assertIsInstance(error, SystemExit, link_kind)

    def test_mineru_force_rejects_late_junction_before_deleting_manifest(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-mineru-preflight-"
        ) as temporary:
            root = Path(temporary)
            work_dir = root / "work"
            outside_dir = root / "outside-renders"
            work_dir.mkdir()
            outside_dir.mkdir()
            (work_dir / "manifest.json").write_bytes(b"manifest-sentinel\n")
            (outside_dir / "page-1.png").write_bytes(b"outside-page\n")
            link = work_dir / "renders"
            link_kind = self._make_directory_link(link, outside_dir)
            work_before = tree_snapshot(work_dir)
            outside_before = tree_snapshot(outside_dir)

            try:
                error = self._capture_failure(
                    lambda: _prepare_work_dir(work_dir, force=True)
                )
                work_after = tree_snapshot(work_dir)
                outside_after = tree_snapshot(outside_dir)
            finally:
                self._remove_directory_link(link)

            self.assertEqual(outside_after, outside_before, link_kind)
            self.assertEqual(work_after, work_before, link_kind)
            self.assertIsInstance(error, AdapterError, link_kind)

    def test_force_invalidates_source_audit_before_later_file_failure(self) -> None:
        cases = (
            (
                "extract",
                extract_pdf_module,
                prepare_output,
                "adapter-evidence.json",
                SystemExit,
                ("document-ir.json", "adapter-evidence.json"),
            ),
            (
                "mineru",
                mineru_module,
                _prepare_work_dir,
                "blocks.jsonl",
                AdapterError,
                ("manifest.json", "blocks.jsonl"),
            ),
        )
        for name, module, operation, failure_name, error_type, filenames in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"artifact-gate-order-{name}-"
            ) as temporary:
                work_dir = Path(temporary) / "work"
                work_dir.mkdir()
                for filename in filenames:
                    (work_dir / filename).write_bytes(filename.encode("ascii"))
                report_path = work_dir / "source-audit.json"
                report_path.write_bytes(b'\n{"status":"passed"}\n')
                real_remove = module.remove_artifact_file

                def injected_remove(path: Path, **kwargs: object) -> None:
                    if Path(path).name == failure_name:
                        raise ArtifactSafetyError("injected later file failure")
                    real_remove(path, **kwargs)

                with mock.patch.object(
                    module, "remove_artifact_file", side_effect=injected_remove
                ):
                    error = self._capture_failure(
                        lambda: operation(work_dir, force=True)
                    )
                self.assertIsInstance(error, error_type)
                self.assertFalse(os.path.lexists(report_path))

    def test_profile_target_errors_keep_cli_and_adapter_contracts(self) -> None:
        for name, operation, error_type in (
            ("extract", prepare_output, SystemExit),
            ("mineru", _prepare_work_dir, AdapterError),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"artifact-profile-contract-{name}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = root / "work"
                work_dir.mkdir()
                outside = root / "outside-profile.json"
                outside.write_bytes(b"outside-profile")
                os.link(outside, work_dir / "profile.json")

                error = self._capture_failure(
                    lambda: operation(work_dir, force=True)
                )

                self.assertIsInstance(error, error_type)
                self.assertEqual(outside.read_bytes(), b"outside-profile")

    def test_document_ir_migration_rejects_linked_manifest(self) -> None:
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-ir-manifest-{kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir, _manifest, _case = self._make_ir_work(root)
                outside_dir = root / "outside"
                outside_dir.mkdir()
                manifest_path = work_dir / "manifest.json"
                outside = outside_dir / "manifest.json"
                if kind == "hardlink":
                    try:
                        os.link(manifest_path, outside)
                    except OSError as exc:
                        self.fail(
                            "hard links must be exercised on this filesystem: "
                            f"{exc}"
                        )
                else:
                    payload = manifest_path.read_bytes()
                    manifest_path.unlink()
                    outside.write_bytes(payload)
                    self._make_file_link(
                        kind,
                        manifest_path,
                        outside,
                        target_payload=None,
                    )
                work_before = tree_snapshot(work_dir)
                outside_before = tree_snapshot(outside_dir)

                error = self._capture_failure(
                    lambda: migrate_work_dir(
                        work_dir, "academic-paper-en-zh", force=False
                    )
                )

                self.assertEqual(tree_snapshot(outside_dir), outside_before)
                self.assertEqual(tree_snapshot(work_dir), work_before)
                self.assertIsInstance(error, ValueError)

    def test_source_entrypoints_reject_linked_pdf_without_reading_target(self) -> None:
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-source-input-{kind}-"
            ) as temporary:
                root = Path(temporary)
                outside = root / "outside.pdf"
                payload = b"outside source bytes must remain unchanged\n"
                link = root / "source.pdf"
                outside.write_bytes(payload)
                self._make_file_link(
                    kind, link, outside, target_payload=None
                )
                environment = dict(os.environ)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"

                extracted = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPTS / "extract_pdf.py"),
                        str(link),
                        "--work-dir",
                        str(root / "extract-work"),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )
                self.assertNotEqual(extracted.returncode, 0)
                expected_extract_error = (
                    "unsafe PDF input"
                    if kind == "hardlink"
                    else "unsafe source PDF or WORK path"
                )
                self.assertIn(
                    expected_extract_error, extracted.stdout + extracted.stderr
                )
                self.assertFalse(os.path.lexists(root / "extract-work"))
                self.assertEqual(outside.read_bytes(), payload)

                error = self._capture_failure(
                    lambda: mineru_module.import_mineru(
                        link,
                        root / "mineru-output",
                        root / "mineru-work",
                        "academic-paper-en-zh",
                    )
                )
                self.assertIsInstance(error, AdapterError)
                self.assertIn("unsafe source PDF input", str(error))
                self.assertFalse(os.path.lexists(root / "mineru-work"))
                self.assertEqual(outside.read_bytes(), payload)

    def test_extract_force_rejects_source_or_profile_inside_work_before_cleanup(self) -> None:
        for label, source_relative, profile_relative in (
            ("source-renders", "renders/source.pdf", None),
            ("source-output", "output/source.pdf", None),
            ("custom-profile", None, "output/custom-profile.json"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"artifact-extract-overlap-{label}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = root / "work"
                work_dir.mkdir()
                (work_dir / "manifest.json").write_bytes(b"old-manifest\n")
                source_pdf = root / "source.pdf"
                if source_relative is not None:
                    source_pdf = work_dir / source_relative
                    source_pdf.parent.mkdir(parents=True)
                source_pdf.write_bytes(b"source-pdf-sentinel\n")
                command = [
                    sys.executable,
                    str(SCRIPTS / "extract_pdf.py"),
                    str(source_pdf),
                    "--work-dir",
                    str(work_dir),
                    "--force",
                ]
                if profile_relative is not None:
                    profile_path = work_dir / profile_relative
                    profile_path.parent.mkdir(parents=True, exist_ok=True)
                    profile_path.write_bytes(b'{"profile":"sentinel"}\n')
                    command.extend(("--profile", str(profile_path)))
                before = tree_snapshot(root)
                environment = dict(os.environ)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"

                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("outside WORK", completed.stdout + completed.stderr)
                self.assertEqual(tree_snapshot(root), before)

    def test_mineru_rejects_any_work_input_tree_overlap_before_cleanup(self) -> None:
        for label in ("same", "input-contains-work", "work-contains-input"):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"artifact-mineru-overlap-{label}-"
            ) as temporary:
                root = Path(temporary)
                source_pdf = root / "source.pdf"
                source_pdf.write_bytes(b"source-pdf-sentinel\n")
                if label == "same":
                    output_dir = work_dir = root / "shared"
                elif label == "input-contains-work":
                    output_dir = root / "mineru-input"
                    work_dir = output_dir / "work"
                else:
                    work_dir = root / "work"
                    output_dir = work_dir / "mineru-input"
                output_dir.mkdir(parents=True)
                work_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "input-sentinel.bin").write_bytes(b"input\n")
                (work_dir / "work-sentinel.bin").write_bytes(b"work\n")
                before = tree_snapshot(root)

                error = self._capture_failure(
                    lambda: mineru_module.import_mineru(
                        source_pdf,
                        output_dir,
                        work_dir,
                        "academic-paper-en-zh",
                        force=True,
                    )
                )

                self.assertIsInstance(error, AdapterError)
                self.assertIn("filesystem-disjoint", str(error))
                self.assertEqual(tree_snapshot(root), before)

    def test_mineru_rejects_linked_input_tree_before_reading_or_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-mineru-input-link-"
        ) as temporary:
            root = Path(temporary)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"source-pdf-sentinel\n")
            outside_dir = root / "outside-input"
            outside_dir.mkdir()
            (outside_dir / "input-sentinel.bin").write_bytes(b"outside\n")
            output_dir = root / "mineru-input"
            link_kind = self._make_directory_link(output_dir, outside_dir)
            work_dir = root / "work"
            outside_before = tree_snapshot(outside_dir)

            try:
                error = self._capture_failure(
                    lambda: mineru_module.import_mineru(
                        source_pdf,
                        output_dir,
                        work_dir,
                        "academic-paper-en-zh",
                        force=True,
                    )
                )
            finally:
                self._remove_directory_link(output_dir)

            self.assertIsInstance(error, AdapterError, link_kind)
            self.assertIn("unsafe MinerU output directory", str(error))
            self.assertEqual(tree_snapshot(outside_dir), outside_before, link_kind)
            self.assertFalse(os.path.lexists(work_dir), link_kind)

    def test_mineru_rejects_work_profile_and_hardlinked_input_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-mineru-input-preflight-"
        ) as temporary:
            root = Path(temporary)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"source-pdf-sentinel\n")
            output_dir = root / "mineru-input"
            output_dir.mkdir()
            work_dir = root / "work"
            work_dir.mkdir()
            profile_path = work_dir / "output" / "custom-profile.json"
            profile_path.parent.mkdir()
            profile_path.write_bytes(b'{"profile":"sentinel"}\n')
            before = tree_snapshot(root)

            profile_error = self._capture_failure(
                lambda: mineru_module.import_mineru(
                    source_pdf,
                    output_dir,
                    work_dir,
                    profile_path,
                    force=True,
                )
            )

            self.assertIsInstance(profile_error, AdapterError)
            self.assertIn("custom Profile must be outside WORK", str(profile_error))
            self.assertEqual(tree_snapshot(root), before)

            external = root / "outside-input.json"
            external.write_bytes(b"outside-input-sentinel\n")
            os.link(external, output_dir / "fixture_content_list.json")
            hardlink_before = tree_snapshot(root)

            input_error = self._capture_failure(
                lambda: mineru_module.import_mineru(
                    source_pdf,
                    output_dir,
                    root / "separate-work",
                    "academic-paper-en-zh",
                    force=True,
                )
            )

            self.assertIsInstance(input_error, AdapterError)
            self.assertIn("unsafe MinerU output directory", str(input_error))
            self.assertEqual(tree_snapshot(root), hardlink_before)

    def test_document_ir_migration_rejects_linked_output(self) -> None:
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-ir-output-{kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir, _manifest, _case = self._make_ir_work(root)
                outside_dir = root / "outside"
                outside_dir.mkdir()
                self._make_file_link(
                    kind,
                    work_dir / "document-ir.json",
                    outside_dir / "document-ir.json",
                    target_payload=b"outside-document-ir\n",
                )
                work_before = tree_snapshot(work_dir)
                outside_before = tree_snapshot(outside_dir)

                error = self._capture_failure(
                    lambda: migrate_work_dir(
                        work_dir, "academic-paper-en-zh", force=False
                    )
                )

                self.assertEqual(tree_snapshot(outside_dir), outside_before)
                self.assertEqual(tree_snapshot(work_dir), work_before)
                self.assertIsInstance(error, ValueError)

    def test_migration_invalidates_source_audit_before_profile_binding(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-ir-gate-order-"
        ) as temporary:
            root = Path(temporary)
            work_dir, _manifest, _case = self._make_ir_work(root)
            report_path = work_dir / "source-audit.json"
            report_path.write_bytes(b'{"status":"passed"}\n')

            with mock.patch.object(
                document_ir_module,
                "_bind_validated_profile",
                side_effect=ValueError("injected profile binding failure"),
            ):
                error = self._capture_failure(
                    lambda: migrate_work_dir(
                        work_dir, "lecture-notes-en-zh", force=True
                    )
                )

            self.assertIsInstance(error, ValueError)
            self.assertFalse(os.path.lexists(report_path))

    def test_source_audit_cli_rejects_linked_report_without_publication(self) -> None:
        for kind in ("hardlink", "symlink", "broken-symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-source-audit-{kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self._make_auditable_work(root)
                outside_dir = root / "outside"
                outside_dir.mkdir()
                self._make_file_link(
                    kind,
                    work_dir / "source-audit.json",
                    outside_dir / "source-audit.json",
                    target_payload=(
                        None if kind == "broken-symlink" else b"outside-report\n"
                    ),
                )
                work_before = tree_snapshot(work_dir)
                outside_before = tree_snapshot(outside_dir)
                environment = dict(os.environ)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"

                completed = subprocess.run(
                    [sys.executable, str(SCRIPTS / "audit_source.py"), str(work_dir)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )

                self.assertEqual(tree_snapshot(outside_dir), outside_before)
                self.assertEqual(tree_snapshot(work_dir), work_before)
                self.assertNotEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertNotIn('"status": "passed"', completed.stdout)

    def test_source_audit_invalid_manifest_invalidates_previous_pass(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="artifact-source-audit-stale-pass-"
        ) as temporary:
            root = Path(temporary)
            work_dir = self._make_auditable_work(root)
            report_path = work_dir / "source-audit.json"
            write_json(report_path, {"status": "passed"})
            (work_dir / "manifest.json").write_text("{invalid\n", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"

            completed = subprocess.run(
                [sys.executable, str(SCRIPTS / "audit_source.py"), str(work_dir)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=environment,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(os.path.lexists(report_path))

    def test_source_binding_rejects_linked_pdf_and_preserves_external_bytes(self) -> None:
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(
                prefix=f"artifact-source-binding-{kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self._make_auditable_work(root)
                report_path = work_dir / "source-audit.json"
                current_bindings = current_source_audit_bindings(work_dir)
                frozen_profile = read_json(work_dir / "profile.json")
                passed_report = {
                    "status": "passed",
                    **current_bindings,
                    "minimum_global_coverage": frozen_profile["qa"][
                        "minimum_global_fivegram_coverage"
                    ],
                    "failures": [],
                }
                write_json(
                    report_path,
                    passed_report,
                )
                _current, precondition_errors = validate_source_audit_binding(
                    work_dir, report_path
                )
                self.assertEqual(precondition_errors, [])
                manifest = read_json(work_dir / "manifest.json")
                source_pdf = Path(manifest["source_pdf"])
                payload = source_pdf.read_bytes()
                outside = root / "outside-source.pdf"
                outside.write_bytes(payload)
                source_pdf.unlink()
                self._make_file_link(
                    kind, source_pdf, outside, target_payload=None
                )

                _report, errors = validate_source_audit_binding(
                    work_dir, report_path
                )
                self.assertTrue(errors)
                self.assertTrue(
                    any("current source freeze chain is invalid" in item for item in errors),
                    errors,
                )
                self.assertEqual(outside.read_bytes(), payload)

                environment = dict(os.environ)
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                completed = subprocess.run(
                    [sys.executable, str(SCRIPTS / "audit_source.py"), str(work_dir)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )
                self.assertNotEqual(completed.returncode, 0)
                failed_report = read_json(report_path)
                self.assertEqual(failed_report["status"], "failed")
                self.assertTrue(
                    any("invalid source PDF binding" in item for item in failed_report["failures"]),
                    failed_report,
                )
                self.assertEqual(outside.read_bytes(), payload)

    def test_source_binding_rejects_relative_manifest_pdf_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            current_source_pdf_binding(
                {"source_pdf": "relative.pdf", "source_sha256": "0" * 64}
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
