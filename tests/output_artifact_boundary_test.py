#!/usr/bin/env python3
"""Output-stage regressions for fail-closed work artifact boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

from audit_source import current_source_audit_bindings  # noqa: E402
from audit_outputs import validate_output_audit_binding  # noqa: E402
from common import read_json, read_jsonl, sha256_file, sha256_text, write_json, write_jsonl  # noqa: E402
from document_ir import expected_ir  # noqa: E402
from profile import canonical_profile_sha256, load_profile  # noqa: E402


def passed_source_audit(work_dir: Path) -> dict[str, object]:
    blocks = read_jsonl(work_dir / "blocks.jsonl")
    profile = read_json(work_dir / "profile.json")
    return {
        "status": "passed",
        "failures": [],
        "adapter_source": {"manual_source_review_required": False},
        "problem_ids": {"oracle": [], "extracted": [], "missing": [], "extra": []},
        "global_fivegram_coverage": 1.0,
        "minimum_global_coverage": profile["qa"][
            "minimum_global_fivegram_coverage"
        ],
        "page_results": [
            {
                "page": 1,
                "coverage": 1.0,
                "oracle_fivegrams": 0,
                "matched_fivegrams": 0,
                "block_count": len(blocks),
            }
        ],
        "rendered_pages": 1,
        **current_source_audit_bindings(work_dir),
    }


class OutputArtifactBoundaryTests(unittest.TestCase):
    def run_script(
        self, name: str, work_dir: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUTF8"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), str(work_dir), *arguments],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=environment,
        )

    def make_work(self, root: Path) -> Path:
        work_dir = root / "work"
        work_dir.mkdir()
        profile = load_profile("assignment-en-zh")
        write_json(work_dir / "profile.json", profile)
        source = "A complete source sentence for the bilingual output fixture."
        blocks = [
            {
                "id": "p001-b001",
                "page": 1,
                "bbox": [20, 20, 400, 40],
                "source": source,
                "source_sha256": sha256_text(source),
                "kind": "prose",
                "translatable": True,
                "links": [],
                "protected_spans": [],
            },
            {
                "id": "p001-b002",
                "page": 1,
                "bbox": [20, 60, 400, 180],
                "source": "Source visual",
                "source_sha256": sha256_text("Source visual"),
                "kind": "image",
                "translatable": False,
                "links": [],
                "protected_spans": [],
            },
        ]
        write_jsonl(work_dir / "blocks.jsonl", blocks)
        visuals = work_dir / "visuals"
        visuals.mkdir()
        Image.new("RGB", (2, 3), "white").save(
            visuals / "figure.bin", format="PNG"
        )
        source_pdf = root / "fixture.pdf"
        source_pdf.write_bytes(b"temporary output source PDF fixture bytes\n")
        (work_dir / "oracle.txt").write_text(source + "\f", encoding="utf-8")
        (work_dir / "oracle-layout.txt").write_text(source + "\f", encoding="utf-8")
        renders = work_dir / "renders"
        renders.mkdir()
        Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
        contacts = work_dir / "source-contact"
        contacts.mkdir()
        contact_path = contacts / "contact-001.png"
        Image.new("RGB", (2, 3), "white").save(contact_path)
        write_json(
            work_dir / "manifest.json",
            {
                "schema_version": 1,
                "source_pdf": str(source_pdf),
                "source_sha256": sha256_file(source_pdf),
                "page_count": 1,
                "artifacts": {
                    "profile": "profile.json",
                    "document_ir": "document-ir.json",
                    "blocks": "blocks.jsonl",
                    "oracle": "oracle.txt",
                    "oracle_layout": "oracle-layout.txt",
                    "renders": "renders/page-*.png",
                    "visuals": "visuals/visual-*.png",
                    "source_contact": "source-contact/contact-*.png",
                },
                "profile": {"id": profile["id"]},
                "visuals": [
                    {
                        "id": "vis-1",
                        "anchor_id": "p001-b002",
                        "path": "visuals/figure.bin",
                        "contained_block_ids": [],
                        "caption_continuation_ids": [],
                    }
                ],
                "links": [],
                "external_uris": [],
                "problem_ids": [],
                "source_contact_sheets": [
                    {
                        "path": "source-contact/contact-001.png",
                        "sha256": sha256_file(contact_path),
                        "first_page": 1,
                        "last_page": 1,
                    }
                ],
            },
        )
        write_json(work_dir / "document-ir.json", expected_ir(work_dir, profile))
        write_json(
            work_dir / "source-audit.json",
            passed_source_audit(work_dir),
        )
        translation = work_dir / "translation"
        translation.mkdir()
        write_json(
            translation / "glossary.json",
            {
                "schema_version": 1,
                "profile_id": profile["id"],
                "profile_sha256": canonical_profile_sha256(profile),
                "target_language": profile["translation"]["target_language"],
                "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
                "terms": [],
            },
        )
        return work_dir

    def build_and_audit(self, work_dir: Path) -> None:
        prepared = self.run_script(
            "prepare_translation.py", work_dir, "--max-source-chars", "1000"
        )
        self.assertEqual(prepared.returncode, 0, prepared.stdout + prepared.stderr)
        plan = read_json(work_dir / "translation" / "plan.json")
        for batch in plan["batches"]:
            requests = read_jsonl(work_dir / "translation" / batch["request_file"])
            write_jsonl(
                work_dir / "translation" / batch["response_file"],
                [
                    {
                        "id": request["id"],
                        "source_sha256": request["source_sha256"],
                        "translation": "这是完整的双语输出测试译文。",
                    }
                    for request in requests
                ],
            )
        translation_audit = self.run_script("audit_translation.py", work_dir)
        self.assertEqual(
            translation_audit.returncode,
            0,
            translation_audit.stdout + translation_audit.stderr,
        )
        built = self.run_script(
            "build_outputs.py", work_dir, "--basename", "fixture"
        )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        audited = self.run_script("audit_outputs.py", work_dir)
        self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)

    def pipeline_status(self, work_dir: Path) -> subprocess.CompletedProcess[str]:
        return self.run_script("pipeline.py", Path("status"), str(work_dir))

    @staticmethod
    def output_snapshot(work_dir: Path) -> dict[str, bytes]:
        output = work_dir / "output"
        return {
            name: (output / name).read_bytes()
            for name in (
                "fixture.md",
                "fixture.tex",
                "build-manifest.json",
                "output-audit.json",
                "assets/figure.bin",
            )
        }

    def assert_snapshot(self, work_dir: Path, expected: dict[str, bytes]) -> None:
        output = work_dir / "output"
        for name, payload in expected.items():
            self.assertEqual((output / name).read_bytes(), payload, name)

    def test_happy_path_atomically_publishes_and_audits_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-boundary-happy-") as temp:
            work_dir = self.make_work(Path(temp))
            self.build_and_audit(work_dir)
            output = work_dir / "output"
            report = read_json(output / "output-audit.json")
            build = read_json(output / "build-manifest.json")
            self.assertEqual(report["status"], "passed")
            self.assertEqual(
                report["artifact_bindings"]["markdown"]["sha256"],
                build["markdown_sha256"],
            )
            self.assertEqual(
                report["artifact_bindings"]["latex"]["sha256"],
                build["latex_sha256"],
            )
            self.assertEqual(
                report["artifact_bindings"]["assets"],
                [{"path": "assets/figure.bin", "sha256": build["assets"][0]["sha256"]}],
            )
            self.assertEqual(
                (output / "assets" / "figure.bin").read_bytes(),
                (work_dir / "visuals" / "figure.bin").read_bytes(),
            )
            self.assertFalse(list(output.rglob(".*.tmp")))

    def test_pipeline_status_marks_changed_output_binding_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-binding-stale-") as temp:
            work_dir = self.make_work(Path(temp))
            self.build_and_audit(work_dir)
            (work_dir / "output" / "fixture.md").write_text(
                "changed after output audit\n", encoding="utf-8"
            )

            completed = self.pipeline_status(work_dir)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            status = json.loads(completed.stdout)
            self.assertEqual(status["gate_statuses"]["output_audit"], "stale")

    def test_output_audit_producer_rejects_recursive_source_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-recursive-source-drift-") as temp:
            work_dir = self.make_work(Path(temp))
            self.build_and_audit(work_dir)
            manifest = read_json(work_dir / "manifest.json")
            source_pdf = Path(manifest["source_pdf"])
            source_pdf.write_bytes(b"source changed after every upstream pass\n")

            completed = self.run_script("audit_outputs.py", work_dir)
            self.assertNotEqual(completed.returncode, 0)
            report = read_json(work_dir / "output" / "output-audit.json")
            self.assertEqual(report["status"], "failed")
            self.assertTrue(
                any("translation freeze chain" in failure for failure in report["failures"]),
                report,
            )

            _report, _build, errors = validate_output_audit_binding(work_dir)
            self.assertTrue(errors)

    def test_output_audit_producer_rejects_hardlinked_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-audit-hardlink-") as temp:
            root = Path(temp)
            work_dir = self.make_work(root)
            self.build_and_audit(work_dir)
            report_path = work_dir / "output" / "output-audit.json"
            payload = report_path.read_bytes()
            report_path.unlink()
            outside = root / "outside-output-audit.json"
            outside.write_bytes(payload)
            os.link(outside, report_path)

            completed = self.run_script("audit_outputs.py", work_dir)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("hard-linked", completed.stdout + completed.stderr)
            self.assertEqual(outside.read_bytes(), payload)

    def test_force_rejects_linked_assets_before_changing_old_outputs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-assets-link-") as temp:
            root = Path(temp)
            work_dir = self.make_work(root)
            self.build_and_audit(work_dir)
            expected = self.output_snapshot(work_dir)
            assets = work_dir / "output" / "assets"
            saved_assets = root / "saved-assets"
            assets.rename(saved_assets)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.bin"
            sentinel.write_bytes(b"outside")
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(assets), str(outside)],
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
            else:
                os.symlink(outside, assets, target_is_directory=True)
            try:
                completed = self.run_script(
                    "build_outputs.py",
                    work_dir,
                    "--basename",
                    "fixture",
                    "--force",
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(
                    any(
                        marker in completed.stdout + completed.stderr
                        for marker in ("symbolic links", "reparse points")
                    ),
                    completed.stdout + completed.stderr,
                )
                self.assertEqual(sentinel.read_bytes(), b"outside")
                for name, payload in expected.items():
                    if name != "assets/figure.bin":
                        self.assertEqual((work_dir / "output" / name).read_bytes(), payload)
                self.assertEqual((saved_assets / "figure.bin").read_bytes(), expected["assets/figure.bin"])
            finally:
                if os.path.lexists(assets):
                    if os.name == "nt":
                        os.rmdir(assets)
                    else:
                        assets.unlink()
                saved_assets.rename(assets)

    def test_force_rejects_hardlinked_output_before_any_invalidation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-hardlink-") as temp:
            root = Path(temp)
            work_dir = self.make_work(root)
            self.build_and_audit(work_dir)
            expected = self.output_snapshot(work_dir)
            markdown = work_dir / "output" / "fixture.md"
            markdown.unlink()
            outside = root / "outside.md"
            outside.write_bytes(expected["fixture.md"])
            os.link(outside, markdown)

            completed = self.run_script(
                "build_outputs.py",
                work_dir,
                "--basename",
                "fixture",
                "--force",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("hard-linked", completed.stdout + completed.stderr)
            self.assertEqual(outside.read_bytes(), expected["fixture.md"])
            for name in (
                "fixture.tex",
                "build-manifest.json",
                "output-audit.json",
                "assets/figure.bin",
            ):
                self.assertEqual((work_dir / "output" / name).read_bytes(), expected[name])

    def test_force_rejects_broken_output_symlink(self) -> None:
        with tempfile.TemporaryDirectory(prefix="output-broken-link-") as temp:
            root = Path(temp)
            work_dir = self.make_work(root)
            self.build_and_audit(work_dir)
            expected = self.output_snapshot(work_dir)
            latex = work_dir / "output" / "fixture.tex"
            latex.unlink()
            outside = root / "missing.tex"
            try:
                os.symlink(outside, latex)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            completed = self.run_script(
                "build_outputs.py",
                work_dir,
                "--basename",
                "fixture",
                "--force",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("symbolic links", completed.stdout + completed.stderr)
            self.assertFalse(outside.exists())
            for name in (
                "fixture.md",
                "build-manifest.json",
                "output-audit.json",
                "assets/figure.bin",
            ):
                self.assertEqual((work_dir / "output" / name).read_bytes(), expected[name])

    def test_visual_metadata_escape_preserves_previous_outputs(self) -> None:
        values = (
            "../outside.bin",
            "C:/outside.bin",
            "\\\\server\\share.bin",
            "output/fixture.md",
            "output/assets/figure.bin",
        )
        for value in values:
            with self.subTest(value=value), tempfile.TemporaryDirectory(
                prefix="output-visual-escape-"
            ) as temp:
                root = Path(temp)
                work_dir = self.make_work(root)
                self.build_and_audit(work_dir)
                expected = self.output_snapshot(work_dir)
                outside = root / "outside.bin"
                outside.write_bytes(b"outside")
                manifest_path = work_dir / "manifest.json"
                manifest = read_json(manifest_path)
                manifest["visuals"][0]["path"] = value
                write_json(manifest_path, manifest)

                completed = self.run_script(
                    "build_outputs.py",
                    work_dir,
                    "--basename",
                    "fixture",
                    "--force",
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "source audit bindings are stale",
                    completed.stdout + completed.stderr,
                )
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertFalse(
                    (work_dir / "output" / "output-audit.json").exists()
                )
                for name, payload in expected.items():
                    if name != "output-audit.json":
                        self.assertEqual(
                            (work_dir / "output" / name).read_bytes(), payload, name
                        )

    def test_audit_metadata_escape_preserves_previous_report(self) -> None:
        cases = (
            ("markdown", "../outside.md"),
            ("latex", "C:/outside.tex"),
            ("asset", "assets\\outside.bin"),
            ("asset", "//server/share.bin"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory(
                prefix="output-audit-escape-"
            ) as temp:
                root = Path(temp)
                work_dir = self.make_work(root)
                self.build_and_audit(work_dir)
                report = work_dir / "output" / "output-audit.json"
                report_payload = report.read_bytes()
                outside = root / "outside.bin"
                outside.write_bytes(b"outside")
                build_path = work_dir / "output" / "build-manifest.json"
                build = read_json(build_path)
                if field == "asset":
                    build["assets"][0]["path"] = value
                else:
                    build[field] = value
                write_json(build_path, build)

                completed = self.run_script("audit_outputs.py", work_dir)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("path", completed.stdout + completed.stderr)
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertEqual(report.read_bytes(), report_payload)
                status = self.pipeline_status(work_dir)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn(
                    json.loads(status.stdout)["gate_statuses"]["output_audit"],
                    {"invalid", "stale"},
                )

    def test_audit_rejects_build_role_aliases_before_mutation(self) -> None:
        cases = (
            ("markdown-gate", lambda build: build.update(markdown="output-audit.json")),
            ("latex-gate", lambda build: build.update(latex="build-manifest.json")),
            ("deliverables", lambda build: build.update(latex=build["markdown"])),
            ("fixed-directory", lambda build: build.update(markdown="assets")),
            (
                "duplicate-asset-path",
                lambda build: build["assets"].append(
                    {**build["assets"][0], "id": "vis-copy"}
                ),
            ),
            (
                "duplicate-asset-id",
                lambda build: build["assets"].append(
                    {
                        **build["assets"][0],
                        "path": "assets/second.bin",
                    }
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"output-role-alias-{name}-"
            ) as temp:
                work_dir = self.make_work(Path(temp))
                self.build_and_audit(work_dir)
                build_path = work_dir / "output" / "build-manifest.json"
                build = read_json(build_path)
                mutate(build)
                write_json(build_path, build)
                expected = {
                    path.relative_to(work_dir).as_posix(): path.read_bytes()
                    for path in work_dir.rglob("*")
                    if path.is_file()
                }

                completed = self.run_script("audit_outputs.py", work_dir)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "invalid build artifact roles", completed.stdout + completed.stderr
                )
                actual = {
                    path.relative_to(work_dir).as_posix(): path.read_bytes()
                    for path in work_dir.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(actual, expected)

    def test_output_validator_rejects_contradictory_passed_shapes(self) -> None:
        cases = {
            "status-type": lambda report: report.update(status=True),
            "failures": lambda report: report.update(failures=["forged"]),
            "semantic-check": lambda report: report.update(
                semantic_constraint_checks={"forged": False}
            ),
            "asset-count": lambda report: report.update(asset_count=True),
            "markdown-role": lambda report: report.update(markdown="other.md"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"output-passed-shape-{name}-"
            ) as temp:
                work_dir = self.make_work(Path(temp))
                self.build_and_audit(work_dir)
                report_path = work_dir / "output" / "output-audit.json"
                report = read_json(report_path)
                mutate(report)
                write_json(report_path, report)

                _report, _build, errors = validate_output_audit_binding(work_dir)
                self.assertTrue(errors, name)
                status = self.pipeline_status(work_dir)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn(
                    json.loads(status.stdout)["gate_statuses"]["output_audit"],
                    {"invalid", "stale"},
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
