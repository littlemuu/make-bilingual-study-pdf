#!/usr/bin/env python3
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

from common import (  # noqa: E402
    read_json,
    read_jsonl,
    sha256_file,
    sha256_text,
    write_json,
    write_jsonl,
)
from audit_source import (  # noqa: E402
    current_source_audit_bindings,
    validate_source_audit_binding,
)
from audit_translation import validate_translation_audit_binding  # noqa: E402
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


class TranslationArtifactBoundaryTests(unittest.TestCase):
    def run_script(
        self, name: str, work_dir: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), str(work_dir), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )

    def make_work(self, root: Path, *, with_translation: bool = True) -> Path:
        work_dir = root / "work"
        work_dir.mkdir()
        profile = load_profile("assignment-en-zh")
        write_json(work_dir / "profile.json", profile)
        source = "This concise source sentence contains enough English words for translation."
        blocks = [
            {
                "id": "p001-b001",
                "page": 1,
                "bbox": [10, 10, 300, 30],
                "source": source,
                "source_sha256": sha256_text(source),
                "kind": "prose",
                "translatable": True,
                "links": [],
                "protected_spans": [],
            }
        ]
        write_jsonl(work_dir / "blocks.jsonl", blocks)
        source_pdf = work_dir / "fixture.pdf"
        source_pdf.write_bytes(b"temporary source PDF fixture bytes\n")
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
                "source_contact_sheets": [
                    {
                        "path": "source-contact/contact-001.png",
                        "sha256": sha256_file(contact_path),
                        "first_page": 1,
                        "last_page": 1,
                    }
                ],
                "problem_ids": [],
                "external_uris": [],
            },
        )
        write_json(work_dir / "document-ir.json", expected_ir(work_dir, profile))
        write_json(
            work_dir / "source-audit.json",
            passed_source_audit(work_dir),
        )
        if with_translation:
            translation_dir = work_dir / "translation"
            translation_dir.mkdir()
            write_json(
                translation_dir / "glossary.json",
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

    def prepare(self, work_dir: Path) -> None:
        completed = self.run_script(
            "prepare_translation.py", work_dir, "--max-source-chars", "1000"
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def add_valid_responses(self, work_dir: Path) -> None:
        plan = read_json(work_dir / "translation" / "plan.json")
        for batch in plan["batches"]:
            requests = read_jsonl(work_dir / "translation" / batch["request_file"])
            responses = [
                {
                    "id": request["id"],
                    "source_sha256": request["source_sha256"],
                    "translation": "这是完整、准确且可读的中文翻译。",
                }
                for request in requests
            ]
            write_jsonl(work_dir / "translation" / batch["response_file"], responses)

    def make_auditable_work(self, root: Path) -> Path:
        work_dir = self.make_work(root)
        self.prepare(work_dir)
        self.add_valid_responses(work_dir)
        return work_dir

    def pipeline_status(self, work_dir: Path) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "pipeline.py"), "status", str(work_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )

    def target_case(self, root: Path, name: str) -> tuple[Path, list[str]]:
        if name == "glossary":
            work_dir = self.make_work(root)
            return work_dir / "translation" / "glossary.json", [
                "init_glossary.py",
                "--force",
            ]
        if name == "plan":
            work_dir = self.make_work(root)
            self.prepare(work_dir)
            return work_dir / "translation" / "plan.json", [
                "prepare_translation.py",
                "--max-source-chars",
                "1000",
                "--force",
            ]
        work_dir = self.make_auditable_work(root)
        filename = {
            "audit": "translation-audit.json",
            "merged": "translations-merged.jsonl",
        }[name]
        return work_dir / "translation" / filename, ["audit_translation.py"]

    def assert_translation_directory_link_rejected(
        self, root: Path, stage: str, *, junction: bool
    ) -> None:
        if stage == "glossary":
            work_dir = self.make_work(root, with_translation=False)
            outside = root / "outside"
            outside.mkdir()
            command = ["init_glossary.py", "--force"]
        elif stage == "prepare":
            work_dir = self.make_work(root)
            outside = root / "outside"
            (work_dir / "translation").rename(outside)
            command = [
                "prepare_translation.py",
                "--max-source-chars",
                "1000",
                "--force",
            ]
        else:
            work_dir = self.make_auditable_work(root)
            outside = root / "outside"
            (work_dir / "translation").rename(outside)
            command = ["audit_translation.py"]
        sentinel = outside / "sentinel.txt"
        sentinel.write_bytes(b"outside")
        before = {
            path.relative_to(outside).as_posix(): path.read_bytes()
            for path in outside.rglob("*")
            if path.is_file()
        }
        translation_link = work_dir / "translation"
        if junction:
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(translation_link),
                    str(outside),
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
        else:
            os.symlink(outside, translation_link, target_is_directory=True)
        try:
            completed = self.run_script(command[0], work_dir, *command[1:])
            self.assertNotEqual(completed.returncode, 0)
            marker = "reparse points" if junction else "symbolic links"
            self.assertIn(marker, completed.stdout + completed.stderr)
            after = {
                path.relative_to(outside).as_posix(): path.read_bytes()
                for path in outside.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
        finally:
            if os.path.lexists(translation_link):
                if junction:
                    os.rmdir(translation_link)
                else:
                    translation_link.unlink()

    def test_happy_path_publishes_complete_translation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="translation-boundary-happy-") as temporary:
            root = Path(temporary)
            work_dir = self.make_work(root, with_translation=False)
            initialized = self.run_script("init_glossary.py", work_dir)
            self.assertEqual(
                initialized.returncode, 0, initialized.stdout + initialized.stderr
            )
            self.prepare(work_dir)
            self.add_valid_responses(work_dir)

            audited = self.run_script("audit_translation.py", work_dir)
            self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
            translation_dir = work_dir / "translation"
            report = read_json(translation_dir / "translation-audit.json")
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(report["plan_sha256"]), 64)
            self.assertEqual(len(report["merged_sha256"]), 64)
            self.assertEqual(
                [item["path"] for item in report["response_bindings"]],
                ["responses/part-0001.jsonl"],
            )
            self.assertEqual(
                len(read_jsonl(translation_dir / "translations-merged.jsonl")), 1
            )
            self.assertFalse(list(translation_dir.glob(".*.tmp")))

    def test_passed_gate_validators_reject_contradictory_report_shapes(self) -> None:
        source_cases = {
            "failures": lambda report: report.pop("failures"),
            "adapter-manual": lambda report: report["adapter_source"].update(
                manual_source_review_required=True
            ),
            "problem-inventory": lambda report: report["problem_ids"].update(
                missing=["p001"]
            ),
            "manual-binding": lambda report: report.update(
                manual_source_review_required=True
            ),
            "lower-threshold": lambda report: report.update(
                minimum_global_coverage=0.1
            ),
            "forged-metrics": lambda report: report["page_results"][0].update(
                block_count=0
            ),
        }
        for name, mutate in source_cases.items():
            with self.subTest(gate="source", name=name), tempfile.TemporaryDirectory(
                prefix=f"source-passed-shape-{name}-"
            ) as temporary:
                work_dir = self.make_work(Path(temporary))
                report_path = work_dir / "source-audit.json"
                report = read_json(report_path)
                mutate(report)
                write_json(report_path, report)
                _report, errors = validate_source_audit_binding(work_dir)
                self.assertTrue(errors, name)
                initialized = self.run_script(
                    "init_glossary.py", work_dir, "--force"
                )
                self.assertNotEqual(initialized.returncode, 0)
                self.assertIn(
                    "source audit bindings are stale",
                    initialized.stdout + initialized.stderr,
                )

        translation_cases = {
            "status-type": lambda report: report.update(status=True),
            "failures": lambda report: report.update(failures=["forged"]),
            "missing": lambda report: report.update(missing_ids=["p001-b001"]),
            "placeholder": lambda report: report.update(
                placeholder_failures={"p001-b001": {}}
            ),
            "counts": lambda report: report.update(validated_segments=True),
            "merged": lambda report: report.update(merged_output=None),
        }
        for name, mutate in translation_cases.items():
            with self.subTest(gate="translation", name=name), tempfile.TemporaryDirectory(
                prefix=f"translation-passed-shape-{name}-"
            ) as temporary:
                work_dir = self.make_auditable_work(Path(temporary))
                audited = self.run_script("audit_translation.py", work_dir)
                self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                report_path = work_dir / "translation" / "translation-audit.json"
                report = read_json(report_path)
                mutate(report)
                write_json(report_path, report)
                _report, errors = validate_translation_audit_binding(work_dir)
                self.assertTrue(errors, name)
                built = self.run_script("build_outputs.py", work_dir)
                self.assertNotEqual(built.returncode, 0)
                self.assertIn(
                    "translation audit",
                    built.stdout + built.stderr,
                )

    def test_source_page_results_cover_the_exact_manifest_page_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-page-results-") as temporary:
            work_dir = self.make_work(Path(temporary))
            render_root = work_dir / "renders"
            Image.new("RGB", (2, 3), "white").save(render_root / "page-2.png")
            source = read_jsonl(work_dir / "blocks.jsonl")[0]["source"]
            (work_dir / "oracle.txt").write_text(
                source + "\fshort second page\f", encoding="utf-8"
            )
            manifest_path = work_dir / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["page_count"] = 2
            manifest["source_contact_sheets"][0]["last_page"] = 2
            write_json(manifest_path, manifest)
            profile = read_json(work_dir / "profile.json")
            write_json(work_dir / "document-ir.json", expected_ir(work_dir, profile))
            report_path = work_dir / "source-audit.json"
            report = passed_source_audit(work_dir)
            report["page_results"].pop()
            report["rendered_pages"] = 1
            report["global_fivegram_coverage"] = report["page_results"][0][
                "coverage"
            ]
            write_json(report_path, report)

            _report, errors = validate_source_audit_binding(work_dir)
            self.assertTrue(
                any("every source page" in error or "stale" in error for error in errors),
                errors,
            )

    def test_source_audit_cli_cannot_lower_the_profile_minimum(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-lower-threshold-") as temporary:
            work_dir = self.make_work(Path(temporary))
            completed = self.run_script(
                "audit_source.py",
                work_dir,
                "--minimum-global-coverage",
                "0.1",
            )
            self.assertNotEqual(completed.returncode, 0)
            report = read_json(work_dir / "source-audit.json")
            self.assertTrue(
                any("cannot lower" in failure for failure in report["failures"]),
                report["failures"],
            )
            self.assertEqual(
                report["minimum_global_coverage"],
                read_json(work_dir / "profile.json")["qa"][
                    "minimum_global_fivegram_coverage"
                ],
            )

    def test_passed_translation_bindings_fail_closed_after_tampering(self) -> None:
        for case in (
            "merged",
            "plan",
            "glossary",
            "request",
            "extra-request",
            "response",
            "extra-response",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"translation-stale-{case}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self.make_auditable_work(root)
                audited = self.run_script("audit_translation.py", work_dir)
                self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
                translation_dir = work_dir / "translation"
                if case == "merged":
                    merged = read_jsonl(
                        translation_dir / "translations-merged.jsonl"
                    )
                    merged[0]["translation"] = "审计后篡改的中文译文。"
                    write_jsonl(
                        translation_dir / "translations-merged.jsonl", merged
                    )
                elif case == "plan":
                    plan = read_json(translation_dir / "plan.json")
                    plan["target_language"] = "zh-stale"
                    write_json(translation_dir / "plan.json", plan)
                elif case == "glossary":
                    glossary_path = translation_dir / "glossary.json"
                    glossary = read_json(glossary_path)
                    glossary["tampered"] = True
                    write_json(glossary_path, glossary)
                elif case == "request":
                    request_path = translation_dir / "requests" / "part-0001.jsonl"
                    requests = read_jsonl(request_path)
                    requests[0]["source_for_translation"] += " changed"
                    write_jsonl(request_path, requests)
                elif case == "extra-request":
                    (translation_dir / "requests" / "junk.txt").write_text(
                        "undeclared request artifact\n", encoding="utf-8"
                    )
                elif case == "response":
                    response_path = translation_dir / "responses" / "part-0001.jsonl"
                    responses = read_jsonl(response_path)
                    responses[0]["translation"] = "审计后改写的响应。"
                    write_jsonl(response_path, responses)
                else:
                    write_jsonl(
                        translation_dir / "responses" / "part-9999.jsonl",
                        [],
                    )

                outside = root / "outside.bin"
                outside.write_bytes(b"outside")
                output = work_dir / "output"
                output.mkdir()
                deliverable = output / "keep.md"
                deliverable.write_bytes(b"valid old deliverable")
                write_json(output / "output-audit.json", {"status": "passed"})

                status = self.pipeline_status(work_dir)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertEqual(
                    json.loads(status.stdout)["gate_statuses"]["translation_audit"],
                    "stale",
                )

                built = self.run_script("build_outputs.py", work_dir)
                self.assertNotEqual(built.returncode, 0)
                self.assertIn(
                    "translation audit bindings are stale",
                    built.stdout + built.stderr,
                )
                self.assertFalse((output / "output-audit.json").exists())
                self.assertEqual(deliverable.read_bytes(), b"valid old deliverable")

                write_json(output / "output-audit.json", {"status": "failed"})
                write_json(output / "qa-report.json", {"status": "passed"})
                finalized = self.run_script("finalize_qa.py", work_dir)
                self.assertNotEqual(finalized.returncode, 0)
                qa = read_json(output / "qa-report.json")
                self.assertEqual(qa["status"], "failed")
                self.assertTrue(
                    any(
                        "translation freeze chain" in failure
                        for failure in qa["failures"]
                    ),
                    qa,
                )
                self.assertEqual(deliverable.read_bytes(), b"valid old deliverable")
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_stale_source_audit_blocks_every_downstream_consumer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-binding-consumers-") as temporary:
            root = Path(temporary)
            work_dir = self.make_auditable_work(root)
            audited = self.run_script("audit_translation.py", work_dir)
            self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)
            built = self.run_script(
                "build_outputs.py", work_dir, "--basename", "fixture"
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            output_audited = self.run_script("audit_outputs.py", work_dir)
            self.assertEqual(
                output_audited.returncode,
                0,
                output_audited.stdout + output_audited.stderr,
            )

            translation_dir = work_dir / "translation"
            glossary_path = translation_dir / "glossary.json"
            plan_path = translation_dir / "plan.json"
            request_path = translation_dir / "requests" / "part-0001.jsonl"
            merged_path = translation_dir / "translations-merged.jsonl"
            translation_audit_path = translation_dir / "translation-audit.json"
            output_dir = work_dir / "output"
            output_audit_path = output_dir / "output-audit.json"
            deliverable_path = output_dir / "fixture.md"
            protected = {
                glossary_path: glossary_path.read_bytes(),
                plan_path: plan_path.read_bytes(),
                request_path: request_path.read_bytes(),
                deliverable_path: deliverable_path.read_bytes(),
            }
            outside = root / "outside.bin"
            outside.write_bytes(b"outside")

            source_pdf = Path(read_json(work_dir / "manifest.json")["source_pdf"])
            source_pdf.write_bytes(b"replacement source PDF bytes\n")

            status = self.pipeline_status(work_dir)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["source_audit"],
                "stale",
            )
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["translation_audit"],
                "stale",
            )
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["output_audit"],
                "stale",
            )
            for script, arguments in (
                ("init_glossary.py", ("--force",)),
                (
                    "prepare_translation.py",
                    ("--max-source-chars", "1000", "--force"),
                ),
            ):
                completed = self.run_script(script, work_dir, *arguments)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "source audit bindings are stale",
                    completed.stdout + completed.stderr,
                )
                for path, payload in protected.items():
                    self.assertEqual(path.read_bytes(), payload, path)
                self.assertEqual(outside.read_bytes(), b"outside")

            write_json(output_dir / "qa-report.json", {"status": "passed"})
            finalized = self.run_script("finalize_qa.py", work_dir)
            self.assertNotEqual(finalized.returncode, 0)
            qa = read_json(output_dir / "qa-report.json")
            self.assertEqual(qa["status"], "failed")
            self.assertTrue(
                any("source freeze chain" in failure for failure in qa["failures"]),
                qa,
            )

            rebuilt = self.run_script(
                "build_outputs.py", work_dir, "--basename", "fixture", "--force"
            )
            self.assertNotEqual(rebuilt.returncode, 0)
            self.assertIn(
                "source audit bindings are stale", rebuilt.stdout + rebuilt.stderr
            )
            self.assertFalse(output_audit_path.exists())
            self.assertEqual(deliverable_path.read_bytes(), protected[deliverable_path])

            reaudited = self.run_script("audit_translation.py", work_dir)
            self.assertNotEqual(reaudited.returncode, 0)
            self.assertIn(
                "source audit bindings are stale",
                reaudited.stdout + reaudited.stderr,
            )
            self.assertFalse(translation_audit_path.exists())
            self.assertFalse(merged_path.exists())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_oracle_drift_recursively_stales_every_passed_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-oracle-recursive-") as temporary:
            work_dir = self.make_auditable_work(Path(temporary))
            translated = self.run_script("audit_translation.py", work_dir)
            self.assertEqual(
                translated.returncode, 0, translated.stdout + translated.stderr
            )
            built = self.run_script(
                "build_outputs.py", work_dir, "--basename", "fixture"
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            audited = self.run_script("audit_outputs.py", work_dir)
            self.assertEqual(audited.returncode, 0, audited.stdout + audited.stderr)

            (work_dir / "oracle.txt").write_text(
                "changed oracle evidence after all gates passed\f", encoding="utf-8"
            )
            status = self.pipeline_status(work_dir)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            gate_statuses = json.loads(status.stdout)["gate_statuses"]
            self.assertEqual(gate_statuses["source_audit"], "stale")
            self.assertEqual(gate_statuses["translation_audit"], "stale")
            self.assertEqual(gate_statuses["output_audit"], "stale")

    def test_source_visual_evidence_bytes_are_bound(self) -> None:
        for case in ("contact", "visual"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"source-{case}-binding-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self.make_work(root)
                manifest_path = work_dir / "manifest.json"
                manifest = read_json(manifest_path)
                if case == "contact":
                    evidence_path = work_dir / "source-contact" / "contact-001.png"
                    evidence_path.parent.mkdir(exist_ok=True)
                    Image.new("RGB", (2, 3), "white").save(evidence_path)
                    manifest["source_contact_sheets"] = [
                        {
                            "path": "source-contact/contact-001.png",
                            "sha256": sha256_file(evidence_path),
                            "first_page": 1,
                            "last_page": 1,
                        }
                    ]
                else:
                    evidence_path = work_dir / "visuals" / "visual-001.png"
                    evidence_path.parent.mkdir()
                    Image.new("RGB", (2, 3), "white").save(evidence_path)
                    manifest["visuals"] = [
                        {
                            "id": "visual-001",
                            "path": "visuals/visual-001.png",
                            "contained_block_ids": [],
                        }
                    ]
                write_json(manifest_path, manifest)
                profile = read_json(work_dir / "profile.json")
                write_json(
                    work_dir / "document-ir.json", expected_ir(work_dir, profile)
                )
                write_json(
                    work_dir / "source-audit.json",
                    passed_source_audit(work_dir),
                )
                evidence_path.write_bytes(b"changed after source audit")

                status = self.pipeline_status(work_dir)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertEqual(
                    json.loads(status.stdout)["gate_statuses"]["source_audit"],
                    "stale",
                )
                prepared = self.run_script(
                    "prepare_translation.py",
                    work_dir,
                    "--max-source-chars",
                    "1000",
                )
                self.assertNotEqual(prepared.returncode, 0)
                self.assertIn(
                    "source audit bindings are stale",
                    prepared.stdout + prepared.stderr,
                )

    def test_source_audit_rejects_undecodable_native_image_evidence(self) -> None:
        expectations = {
            "render": "source render cannot be fully decoded",
            "contact": "source contact sheet cannot be fully decoded",
            "visual": "visual crop cannot be fully decoded",
            "visual_metadata": "visual crop declared width changed",
        }
        for case, expected in expectations.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"source-invalid-{case}-"
            ) as temporary:
                work_dir = self.make_work(Path(temporary))
                manifest_path = work_dir / "manifest.json"
                manifest = read_json(manifest_path)
                if case == "render":
                    (work_dir / "renders" / "page-1.png").write_bytes(b"not png")
                elif case == "contact":
                    path = work_dir / "source-contact" / "contact-001.png"
                    path.write_bytes(b"not png")
                    manifest["source_contact_sheets"][0]["sha256"] = sha256_file(path)
                else:
                    visual_root = work_dir / "visuals"
                    visual_root.mkdir()
                    path = visual_root / "visual-001.png"
                    if case == "visual":
                        path.write_bytes(b"not png")
                    else:
                        Image.new("RGB", (2, 3), "white").save(path)
                    manifest["visuals"] = [
                        {
                            "id": "visual-001",
                            "anchor_id": "p001-b001",
                            "path": "visuals/visual-001.png",
                            "sha256": sha256_file(path),
                            "contained_block_ids": [],
                            **({"width": 99} if case == "visual_metadata" else {}),
                        }
                    ]
                write_json(manifest_path, manifest)
                profile = read_json(work_dir / "profile.json")
                write_json(
                    work_dir / "document-ir.json", expected_ir(work_dir, profile)
                )

                completed = self.run_script("audit_source.py", work_dir)
                self.assertNotEqual(completed.returncode, 0)
                report = read_json(work_dir / "source-audit.json")
                self.assertEqual(report["status"], "failed")
                self.assertTrue(
                    any(expected in failure for failure in report["failures"]),
                    report["failures"],
                )

    def test_source_render_binding_accepts_poppler_zero_padding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="source-render-padding-") as temporary:
            work_dir = self.make_work(Path(temporary))
            render_root = work_dir / "renders"
            (render_root / "page-1.png").unlink()
            for page in range(1, 13):
                Image.new("RGB", (2, 3), "white").save(
                    render_root / f"page-{page:02d}.png"
                )
            source = read_jsonl(work_dir / "blocks.jsonl")[0]["source"]
            twelve_page_oracle = source + "\f" + "short\f" * 11
            (work_dir / "oracle.txt").write_text(
                twelve_page_oracle, encoding="utf-8"
            )
            (work_dir / "oracle-layout.txt").write_text(
                twelve_page_oracle, encoding="utf-8"
            )
            manifest_path = work_dir / "manifest.json"
            manifest = read_json(manifest_path)
            manifest["page_count"] = 12
            manifest["source_contact_sheets"][0]["last_page"] = 12
            write_json(manifest_path, manifest)
            profile = read_json(work_dir / "profile.json")
            write_json(work_dir / "document-ir.json", expected_ir(work_dir, profile))

            bindings = current_source_audit_bindings(work_dir)
            self.assertEqual(
                [item["path"] for item in bindings["source_renders"]],
                [f"renders/page-{page:02d}.png" for page in range(1, 13)],
            )

    def test_every_stage_rejects_a_linked_work_ancestor(self) -> None:
        for stage in ("glossary", "prepare", "audit"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                prefix=f"translation-work-ancestor-{stage}-"
            ) as temporary:
                root = Path(temporary)
                actual_parent = root / "actual-parent"
                actual_parent.mkdir()
                if stage == "glossary":
                    actual_work = self.make_work(
                        actual_parent, with_translation=False
                    )
                    command = ["init_glossary.py", "--force"]
                    forbidden = [actual_work / "translation"]
                elif stage == "prepare":
                    actual_work = self.make_work(actual_parent)
                    command = [
                        "prepare_translation.py",
                        "--max-source-chars",
                        "1000",
                    ]
                    forbidden = [actual_work / "translation" / "plan.json"]
                else:
                    actual_work = self.make_auditable_work(actual_parent)
                    command = ["audit_translation.py"]
                    forbidden = [
                        actual_work / "translation" / "translation-audit.json",
                        actual_work / "translation" / "translations-merged.jsonl",
                    ]

                linked_parent = root / "linked-parent"
                if os.name == "nt":
                    created = subprocess.run(
                        [
                            "cmd.exe",
                            "/d",
                            "/c",
                            "mklink",
                            "/J",
                            str(linked_parent),
                            str(actual_parent),
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
                else:
                    os.symlink(actual_parent, linked_parent, target_is_directory=True)
                try:
                    completed = self.run_script(
                        command[0], linked_parent / "work", *command[1:]
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertTrue(
                        any(
                            marker in completed.stdout + completed.stderr
                            for marker in ("symbolic links", "reparse points")
                        ),
                        completed.stdout + completed.stderr,
                    )
                    for path in forbidden:
                        self.assertFalse(path.exists(), path)
                finally:
                    if os.path.lexists(linked_parent):
                        if os.name == "nt":
                            os.rmdir(linked_parent)
                        else:
                            linked_parent.unlink()

    @unittest.skipIf(os.name == "nt", "POSIX directory-symlink regression")
    def test_translation_directory_symlink_is_rejected(self) -> None:
        for stage in ("glossary", "prepare", "audit"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                prefix=f"translation-dir-link-{stage}-"
            ) as temporary:
                self.assert_translation_directory_link_rejected(
                    Path(temporary), stage, junction=False
                )

    @unittest.skipUnless(os.name == "nt", "junction regression is Windows-specific")
    def test_translation_directory_junction_is_rejected(self) -> None:
        for stage in ("glossary", "prepare", "audit"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(
                prefix=f"translation-dir-junction-{stage}-"
            ) as temporary:
                self.assert_translation_directory_link_rejected(
                    Path(temporary), stage, junction=True
                )

    def test_fixed_translation_artifact_hardlinks_are_rejected(self) -> None:
        for name in ("glossary", "plan", "audit", "merged"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"translation-{name}-hardlink-"
            ) as temporary:
                root = Path(temporary)
                target, command = self.target_case(root, name)
                original = target.read_bytes() if target.exists() else b"outside sentinel\n"
                if target.exists():
                    target.unlink()
                outside = root / f"outside-{name}.bin"
                outside.write_bytes(original)
                os.link(outside, target)

                completed = self.run_script(command[0], root / "work", *command[1:])
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("hard-linked", completed.stdout + completed.stderr)
                self.assertEqual(outside.read_bytes(), original)

    def test_fixed_translation_artifact_broken_symlinks_are_rejected(self) -> None:
        symlink_supported = True
        for name in ("glossary", "plan", "audit", "merged"):
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"translation-{name}-broken-"
            ) as temporary:
                root = Path(temporary)
                target, command = self.target_case(root, name)
                if target.exists():
                    target.unlink()
                outside = root / f"missing-{name}.bin"
                try:
                    os.symlink(outside, target)
                except OSError:
                    symlink_supported = False
                    break

                completed = self.run_script(command[0], root / "work", *command[1:])
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("symbolic links", completed.stdout + completed.stderr)
                self.assertFalse(outside.exists())
        if not symlink_supported:
            self.skipTest("file symlinks are unavailable")

    def test_force_preflights_requests_and_responses_before_deleting(self) -> None:
        for unsafe_directory in ("requests", "responses"):
            with self.subTest(directory=unsafe_directory), tempfile.TemporaryDirectory(
                prefix=f"translation-force-{unsafe_directory}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self.make_work(root)
                self.prepare(work_dir)
                request = work_dir / "translation" / "requests" / "part-0001.jsonl"
                request_bytes = request.read_bytes()
                outside = root / "outside.bin"
                outside.write_bytes(b"outside")
                unsafe = work_dir / "translation" / unsafe_directory / "zz-unsafe.jsonl"
                os.link(outside, unsafe)

                completed = self.run_script(
                    "prepare_translation.py",
                    work_dir,
                    "--max-source-chars",
                    "1000",
                    "--force",
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("hard-linked", completed.stdout + completed.stderr)
                self.assertEqual(request.read_bytes(), request_bytes)
                self.assertEqual(outside.read_bytes(), b"outside")

    def test_force_validates_complete_plan_before_replacing_requests(self) -> None:
        with tempfile.TemporaryDirectory(prefix="translation-force-plan-") as temporary:
            root = Path(temporary)
            work_dir = self.make_work(root)
            self.prepare(work_dir)
            translation_dir = work_dir / "translation"
            request_path = translation_dir / "requests" / "part-0001.jsonl"
            plan_path = translation_dir / "plan.json"
            request_before = request_path.read_bytes()
            plan_before = plan_path.read_bytes()
            manifest_path = work_dir / "manifest.json"
            manifest = read_json(manifest_path)
            del manifest["source_sha256"]
            write_json(manifest_path, manifest)

            completed = self.run_script(
                "prepare_translation.py",
                work_dir,
                "--max-source-chars",
                "1000",
                "--force",
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(
                "source audit bindings are stale",
                completed.stdout + completed.stderr,
            )
            self.assertEqual(request_path.read_bytes(), request_before)
            self.assertEqual(plan_path.read_bytes(), plan_before)

    def test_request_and_response_metadata_paths_are_lexically_bounded(self) -> None:
        for field, value_kind in (
            ("request_file", "parent"),
            ("request_file", "absolute"),
            ("response_file", "parent"),
            ("response_file", "absolute"),
        ):
            with self.subTest(field=field, value_kind=value_kind), tempfile.TemporaryDirectory(
                prefix=f"translation-metadata-{field}-{value_kind}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self.make_auditable_work(root)
                initial = self.run_script("audit_translation.py", work_dir)
                self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
                translation_dir = work_dir / "translation"
                report_path = translation_dir / "translation-audit.json"
                merged_path = translation_dir / "translations-merged.jsonl"
                self.assertEqual(read_json(report_path)["status"], "passed")
                self.assertTrue(merged_path.exists())
                outside = root / "outside.jsonl"
                outside.write_bytes(b'{"id":"outside"}\n')
                original = outside.read_bytes()
                plan_path = translation_dir / "plan.json"
                plan = read_json(plan_path)
                plan["batches"][0][field] = (
                    "../outside.jsonl" if value_kind == "parent" else str(outside)
                )
                write_json(plan_path, plan)

                completed = self.run_script("audit_translation.py", work_dir)
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(
                    any(
                        marker in completed.stdout + completed.stderr
                        for marker in ("part-NNNN.jsonl", "translation-relative path")
                    ),
                    completed.stdout + completed.stderr,
                )
                self.assertEqual(outside.read_bytes(), original)
                self.assertEqual(read_json(report_path)["status"], "failed")
                self.assertFalse(merged_path.exists())

    def test_undeclared_response_file_cannot_satisfy_the_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="translation-response-binding-") as temporary:
            root = Path(temporary)
            work_dir = self.make_auditable_work(root)
            initial = self.run_script("audit_translation.py", work_dir)
            self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
            translation_dir = work_dir / "translation"
            report_path = translation_dir / "translation-audit.json"
            merged_path = translation_dir / "translations-merged.jsonl"
            response_path = translation_dir / "responses" / "part-0001.jsonl"
            rogue_path = translation_dir / "responses" / "rogue.jsonl"
            response_path.rename(rogue_path)

            completed = self.run_script("audit_translation.py", work_dir)
            self.assertNotEqual(completed.returncode, 0)
            report = read_json(report_path)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(
                any("undeclared translation response" in item for item in report["failures"]),
                report,
            )
            self.assertEqual(report["response_files"], [])
            self.assertFalse(merged_path.exists())

    def test_unreadable_plan_invalidates_previous_passed_outputs(self) -> None:
        for case in ("invalid-json", "non-object"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"translation-plan-parse-{case}-"
            ) as temporary:
                root = Path(temporary)
                work_dir = self.make_auditable_work(root)
                initial = self.run_script("audit_translation.py", work_dir)
                self.assertEqual(initial.returncode, 0, initial.stdout + initial.stderr)
                translation_dir = work_dir / "translation"
                plan_path = translation_dir / "plan.json"
                report_path = translation_dir / "translation-audit.json"
                merged_path = translation_dir / "translations-merged.jsonl"
                self.assertEqual(read_json(report_path)["status"], "passed")
                self.assertTrue(merged_path.exists())
                if case == "invalid-json":
                    plan_path.write_text("{invalid\n", encoding="utf-8")
                else:
                    write_json(plan_path, ["not", "an", "object"])

                completed = self.run_script("audit_translation.py", work_dir)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(report_path.exists())
                self.assertFalse(merged_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
