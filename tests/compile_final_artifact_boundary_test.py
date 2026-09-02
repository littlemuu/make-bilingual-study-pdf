#!/usr/bin/env python3
"""Compile/final-stage regressions for unsafe WORK artifacts."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.dont_write_bytecode = True
sys.path.insert(0, str(SCRIPTS))

from audit_docx import (  # noqa: E402
    validate_compile_docx_binding,
    validate_v2_compile_docx_binding,
)
from audit_source import current_source_audit_bindings  # noqa: E402
from document_ir import expected_ir  # noqa: E402
import compile_docx_pdf as compile_docx_pdf_module  # noqa: E402
import compile_pdf as compile_pdf_module  # noqa: E402
import pipeline as pipeline_module  # noqa: E402
from job_state import evaluate_job  # noqa: E402
from profile import canonical_profile_sha256, load_profile, profile_contract  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def tex_compile_evidence() -> dict[str, object]:
    return {
        "rendered_pages": 1,
        "ink_ratios": [0.5],
        "blank_pages": [],
        "missing_character_count": 0,
        "undefined_reference_count": 0,
        "overfull_box_count": 0,
        "problem_ids_expected": [],
        "problem_ids_missing": [],
        "source_contains_cjk": False,
        "extracted_text_contains_cjk": False,
        "checks": {
            "latexmk_succeeded": True,
            "pdf_created": True,
            "no_missing_characters": True,
            "no_undefined_references": True,
            "all_pages_rendered": True,
            "no_apparently_blank_pages": True,
            "all_problem_ids_present": True,
            "chinese_text_extractable": True,
        },
    }


def docx_compile_evidence() -> dict[str, object]:
    return {
        "rendered_page_count": 1,
        "invalid_renders": [],
        "blank_pages": [],
        "problem_count": 0,
        "font_count": 1,
        "pdf_image_count": 0,
        "requested_cjk_font": "Fixture CJK",
        "resolved_cjk_family": "Fixture CJK",
        "resolved_cjk_file": "fixture-cjk.ttf",
        "minimum_page_text_characters": 1,
        "minimum_nonwhite_fraction": 0.1,
        "checks": {
            "pdf_created": True,
            "all_pages_rendered": True,
            "all_renders_decodable": True,
            "contact_sheets_complete": True,
            "all_pages_a4": True,
            "no_apparently_blank_pages": True,
            "chinese_extractable": True,
            "all_fonts_embedded": True,
            "requested_cjk_font_resolved_exactly": True,
            "expected_cjk_font_embedded": True,
            "problem_count_matches": True,
        },
    }


def docx_audit_checks() -> dict[str, bool]:
    return {
        "docx_opens": True,
        "problem_ids_are_unique": True,
        "no_internal_problem_markers": True,
        "chinese_present": True,
        "minimum_images_met": True,
        "problem_callout_borders_are_aligned": True,
        "problem_count_matches": True,
        "example_count_matches": True,
        "tip_count_matches": True,
        "external_link_count_matches": True,
    }


def docx_audit_evidence() -> dict[str, object]:
    return {
        "problem_count": 0,
        "problem_ids": [],
        "problem_range_count": 0,
        "example_count": 0,
        "low_resource_tip_count": 0,
        "external_link_count": 0,
        "external_links": [],
        "image_count": 1,
        "chinese_character_count": 1,
        "checks": docx_audit_checks(),
    }


class CompileFinalArtifactBoundaryTests(unittest.TestCase):
    def run_script(
        self,
        name: str,
        *arguments: object,
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUTF8"] = "1"
        if environment_overrides:
            environment.update(environment_overrides)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *(str(item) for item in arguments)],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env=environment,
        )

    def run_pipeline_main(self, fake_run_script, *arguments: object) -> str:
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["pipeline.py", *(str(item) for item in arguments)],
            ),
            patch.object(
                pipeline_module, "run_script", side_effect=fake_run_script
            ),
            redirect_stdout(stdout),
        ):
            pipeline_module.main()
        return stdout.getvalue()

    def run_with_discovery_trap(
        self, root: Path, name: str, *arguments: object
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        hook_dir = root / "python-hook"
        hook_dir.mkdir()
        sentinel = root / "tool-discovery.txt"
        (hook_dir / "sitecustomize.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import shutil\n\n"
            "def blocked_tool_discovery(name):\n"
            "    Path(os.environ['TOOL_DISCOVERY_SENTINEL']).write_text(\n"
            "        name, encoding='utf-8'\n"
            "    )\n"
            "    raise AssertionError(f'external tool discovery reached: {name}')\n\n"
            "shutil.which = blocked_tool_discovery\n",
            encoding="utf-8",
        )
        python_path = str(hook_dir)
        if os.environ.get("PYTHONPATH"):
            python_path += os.pathsep + os.environ["PYTHONPATH"]
        completed = self.run_script(
            name,
            *arguments,
            environment_overrides={
                "PYTHONPATH": python_path,
                "TOOL_DISCOVERY_SENTINEL": str(sentinel),
            },
        )
        return completed, sentinel

    @staticmethod
    def write_json(path: Path, value: object) -> None:
        import json

        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def make_review_work(self, root: Path) -> tuple[Path, dict]:
        work_dir = root / "work"
        output = work_dir / "output"
        build_dir = output / "build"
        contact = output / "contact"
        build_dir.mkdir(parents=True)
        contact.mkdir(parents=True)
        pdf_bytes = b"compiled pdf fixture"
        tex_bytes = b"frozen TeX fixture"
        contact_bytes = b"contact sheet fixture"
        (output / "fixture.tex").write_bytes(tex_bytes)
        (build_dir / "fixture.pdf").write_bytes(pdf_bytes)
        (contact / "contact-001.png").write_bytes(contact_bytes)
        compile_report = {
            "status": "needs_visual_review",
            "automated_status": "passed",
            "pdf": "build/fixture.pdf",
            "pdf_sha256": digest(pdf_bytes),
            "page_count": 1,
            **tex_compile_evidence(),
            "failures": [],
            "contact_sheets": [
                {
                    "path": "contact/contact-001.png",
                    "sha256": digest(contact_bytes),
                    "first_page": 1,
                    "last_page": 1,
                }
            ],
        }
        self.write_json(
            output / "build-manifest.json",
            {
                "markdown": None,
                "latex": "fixture.tex",
                "latex_sha256": digest(tex_bytes),
                "assets": [],
                "block_count": 0,
                "disposition_counts": {},
                "role_inventory": {
                    role: {"occurrence_count": 0}
                    for role in ("problem", "example", "tip")
                },
                "problem_ids": [],
                "external_uris": [],
            },
        )
        self.write_source_gate(work_dir)
        self.write_translation_gate(work_dir)
        build_path = output / "build-manifest.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        build["translation_audit_sha256"] = digest(
            (work_dir / "translation" / "translation-audit.json").read_bytes()
        )
        self.write_json(build_path, build)
        self.write_output_gate(output)
        self.bind_compile_to_output(output, compile_report)
        return work_dir, compile_report

    def write_translation_gate(self, work_dir: Path) -> None:
        translation = work_dir / "translation"
        translation.mkdir(exist_ok=True)
        responses = translation / "responses"
        responses.mkdir(exist_ok=True)
        profile = load_profile("assignment-en-zh")
        glossary_path = translation / "glossary.json"
        self.write_json(glossary_path, {"terms": []})
        manifest = json.loads(
            (work_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.write_json(
            translation / "plan.json",
            {
                "schema_version": 2,
                "profile_id": profile["id"],
                "profile_sha256": canonical_profile_sha256(profile),
                "profile_file_sha256": digest(
                    (work_dir / "profile.json").read_bytes()
                ),
                "document_ir_sha256": digest(
                    (work_dir / "document-ir.json").read_bytes()
                ),
                "source_pdf_sha256": manifest["source_sha256"],
                "source_manifest_sha256": digest(
                    (work_dir / "manifest.json").read_bytes()
                ),
                "source_blocks_sha256": digest(
                    (work_dir / "blocks.jsonl").read_bytes()
                ),
                "source_audit_sha256": digest(
                    (work_dir / "source-audit.json").read_bytes()
                ),
                "glossary_sha256": digest(glossary_path.read_bytes()),
                "target_language": profile["translation"]["target_language"],
                "batch_count": 0,
                "batches": [],
                "expected_segment_count": 0,
                "expected_ids": [],
            },
        )
        (translation / "translations-merged.jsonl").write_bytes(b"")
        self.write_json(
            translation / "translation-audit.json",
            {
                "status": "passed",
                "plan_sha256": digest((translation / "plan.json").read_bytes()),
                "merged_sha256": digest(
                    (translation / "translations-merged.jsonl").read_bytes()
                ),
                "response_bindings": [],
                "response_files": [],
                "merged_output": "translation/translations-merged.jsonl",
                "expected_segments": 0,
                "response_segments": 0,
                "validated_segments": 0,
                "missing_ids": [],
                "extra_ids": [],
                "duplicate_ids": [],
                "invalid_source_hash_ids": [],
                "empty_translation_ids": [],
                "untranslated_ids": [],
                "source_copy_ids": [],
                "placeholder_failures": {},
                "glossary_failures": {},
                "failures": [],
            },
        )

    def write_source_gate(self, work_dir: Path) -> None:
        profile = load_profile("assignment-en-zh")
        self.write_json(work_dir / "profile.json", profile)
        (work_dir / "blocks.jsonl").write_bytes(b"")
        (work_dir / "oracle.txt").write_text("fixture\f", encoding="utf-8")
        (work_dir / "oracle-layout.txt").write_text("fixture\f", encoding="utf-8")
        renders = work_dir / "renders"
        renders.mkdir(exist_ok=True)
        Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
        source_contact = work_dir / "source-contact"
        source_contact.mkdir(exist_ok=True)
        source_contact_path = source_contact / "contact-1.png"
        Image.new("RGB", (2, 3), "white").save(source_contact_path)
        source_pdf = work_dir.parent / "source.pdf"
        source_pdf.write_bytes(b"synthetic source PDF fixture\n")
        self.write_json(
            work_dir / "manifest.json",
            {
                "source_pdf": str(source_pdf.absolute()),
                "source_sha256": digest(source_pdf.read_bytes()),
                "page_count": 1,
                "artifacts": {
                    "profile": "profile.json",
                    "document_ir": "document-ir.json",
                    "blocks": "blocks.jsonl",
                    "oracle": "oracle.txt",
                    "oracle_layout": "oracle-layout.txt",
                    "renders": "renders/page-*.png",
                    "source_contact": "source-contact/contact-*.png",
                },
                "visuals": [],
                "source_contact_sheets": [
                    {
                        "path": "source-contact/contact-1.png",
                        "sha256": digest(source_contact_path.read_bytes()),
                        "first_page": 1,
                        "last_page": 1,
                    }
                ],
            },
        )
        self.write_json(
            work_dir / "document-ir.json", expected_ir(work_dir, profile)
        )
        self.write_json(
            work_dir / "source-audit.json",
            {
                "status": "passed",
                **current_source_audit_bindings(work_dir),
                "problem_ids": {
                    "oracle": [],
                    "extracted": [],
                    "missing": [],
                    "extra": [],
                },
                "manual_source_review_required": False,
                "manual_review_pages": [],
                "adapter_page_statuses": [],
                "global_fivegram_coverage": 1.0,
                "minimum_global_coverage": profile["qa"][
                    "minimum_global_fivegram_coverage"
                ],
                "rendered_pages": 1,
                "page_results": [
                    {
                        "page": 1,
                        "matched_fivegrams": 0,
                        "oracle_fivegrams": 0,
                        "block_count": 0,
                        "coverage": 1.0,
                    }
                ],
                "failures": [],
            },
        )

    def write_output_gate(
        self,
        output: Path,
        *,
        artifact_bindings: dict[str, object] | None = None,
    ) -> None:
        build_path = output / "build-manifest.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        if artifact_bindings is None:
            artifact_bindings = {}
            for field in ("markdown", "latex"):
                value = build.get(field)
                if value is None:
                    continue
                path = output / value
                artifact_bindings[field] = {
                    "path": value,
                    "sha256": digest(path.read_bytes()),
                }
            artifact_bindings["assets"] = [
                {
                    "path": asset["path"],
                    "sha256": digest((output / asset["path"]).read_bytes()),
                }
                for asset in build.get("assets", [])
            ]
        self.write_json(
            output / "output-audit.json",
            {
                "status": "passed",
                "build_manifest_sha256": digest(build_path.read_bytes()),
                "artifact_bindings": artifact_bindings,
                "markdown": build.get("markdown"),
                "latex": build.get("latex"),
                "asset_count": len(build.get("assets", [])),
                "block_count": build.get("block_count"),
                "disposition_counts": build.get("disposition_counts"),
                "semantic_constraint_checks": {
                    item["id"]: True
                    for item in profile_contract(
                        load_profile("assignment-en-zh")
                    )["constraints"]
                },
                "failures": [],
            },
        )

    def bind_compile_to_output(
        self, output: Path, compile_report: dict[str, object]
    ) -> None:
        compile_report.update(
            {
                "build_manifest_sha256": digest(
                    (output / "build-manifest.json").read_bytes()
                ),
                "output_audit_sha256": digest(
                    (output / "output-audit.json").read_bytes()
                ),
            }
        )
        self.write_json(output / "compile-audit.json", compile_report)

    def make_compile_work(self, root: Path) -> tuple[Path, dict[str, Path]]:
        work = root / "work"
        output = work / "output"
        assets = output / "assets"
        build_dir = output / "build"
        assets.mkdir(parents=True)
        build_dir.mkdir()
        paths = {
            "markdown": output / "fixture.md",
            "latex": output / "fixture.tex",
            "asset": assets / "diagram.bin",
            "docx": output / "fixture.docx",
            "latex_pdf": build_dir / "fixture.pdf",
            "docx_pdf": output / "fixture.pdf",
            "outside": root / "outside.bin",
        }
        payloads = {
            "markdown": b"frozen markdown\n",
            "latex": b"frozen latex\n",
            "asset": b"frozen asset\n",
            "docx": b"frozen docx\n",
            "latex_pdf": b"old latex pdf\n",
            "docx_pdf": b"old docx pdf\n",
            "outside": b"outside sentinel\n",
        }
        for name, path in paths.items():
            path.write_bytes(payloads[name])
        self.write_json(
            output / "build-manifest.json",
            {
                "markdown": paths["markdown"].name,
                "markdown_sha256": digest(payloads["markdown"]),
                "latex": paths["latex"].name,
                "latex_sha256": digest(payloads["latex"]),
                "assets": [
                    {
                        "id": "fixture-asset",
                        "path": "assets/diagram.bin",
                        "sha256": digest(payloads["asset"]),
                    }
                ],
                "block_count": 0,
                "disposition_counts": {},
                "role_inventory": {
                    role: {"occurrence_count": 0}
                    for role in ("problem", "example", "tip")
                },
                "problem_ids": [],
                "external_uris": [],
            },
        )
        self.write_source_gate(work)
        self.write_translation_gate(work)
        build_path = output / "build-manifest.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        build["translation_audit_sha256"] = digest(
            (work / "translation" / "translation-audit.json").read_bytes()
        )
        self.write_json(build_path, build)
        self.write_output_gate(output)
        for name in ("compile-audit.json", "visual-review.json", "qa-report.json"):
            (output / name).write_bytes(f"old {name}\n".encode("utf-8"))
        return work, paths

    def tamper_frozen_output(self, output: Path, kind: str) -> None:
        if kind == "markdown":
            (output / "fixture.md").write_bytes(b"changed markdown\n")
        elif kind == "asset":
            (output / "assets" / "diagram.bin").write_bytes(b"changed asset\n")
        elif kind == "build-manifest":
            build_path = output / "build-manifest.json"
            build = json.loads(build_path.read_text(encoding="utf-8"))
            build["tampered"] = True
            self.write_json(build_path, build)
        else:  # pragma: no cover - test helper contract
            raise AssertionError(f"unknown tamper kind: {kind}")

    def make_finalizable_work(self, root: Path) -> tuple[Path, dict]:
        work_dir, compile_report = self.make_review_work(root)
        output = work_dir / "output"
        self.bind_compile_to_output(output, compile_report)
        contact = compile_report["contact_sheets"][0]
        self.write_json(
            output / "visual-review.json",
            {
                "status": "passed",
                "compile_audit_sha256": digest(
                    (output / "compile-audit.json").read_bytes()
                ),
                "pdf": compile_report["pdf"],
                "pdf_sha256": compile_report["pdf_sha256"],
                "page_count": compile_report["page_count"],
                "reviewed_pages": [1],
                "contact_sheets_inspected": [contact["path"]],
                "contact_sheets_sha256": {contact["path"]: contact["sha256"]},
                "notes": "Inspected the complete output.",
                "failures": [],
            },
        )
        return work_dir, compile_report

    def make_docx_finalizable_work(self, root: Path) -> tuple[Path, dict]:
        work_dir, compile_report = self.make_finalizable_work(root)
        output = work_dir / "output"
        profile = load_profile("assignment-en-zh")
        markdown = output / "fixture.md"
        markdown.write_text("English fixture.\n\n中文夹具。\n", encoding="utf-8")
        build_path = output / "build-manifest.json"
        build = json.loads(build_path.read_text(encoding="utf-8"))
        build.update(
            {
                "markdown": markdown.name,
                "markdown_sha256": digest(markdown.read_bytes()),
                "role_inventory": {
                    role: {"occurrence_count": 0}
                    for role in ("problem", "example", "tip")
                },
                "problem_ids": [],
                "external_uris": [],
            }
        )
        self.write_json(build_path, build)
        self.write_output_gate(output)
        docx = output / "fixture.docx"
        pdf = output / "fixture.pdf"
        docx.write_bytes(b"audited DOCX fixture\n")
        pdf.write_bytes((output / str(compile_report["pdf"])).read_bytes())
        bindings = {
            "profile": profile["id"],
            "profile_file_sha256": digest((work_dir / "profile.json").read_bytes()),
            "build_manifest_sha256": digest(
                (output / "build-manifest.json").read_bytes()
            ),
            "output_audit_sha256": digest(
                (output / "output-audit.json").read_bytes()
            ),
            "docx_sha256": digest(docx.read_bytes()),
        }
        docx_audit = output / "docx-audit.json"
        self.write_json(
            docx_audit,
            {
                "status": "passed",
                "docx": str(docx),
                **bindings,
                **docx_audit_evidence(),
                "failures": [],
            },
        )
        compile_report.update(
            {
                "status": "passed",
                "automated_status": "passed",
                "docx": docx.name,
                "docx_sha256": bindings["docx_sha256"],
                "pdf": pdf.name,
                "pdf_sha256": digest(pdf.read_bytes()),
                **docx_compile_evidence(),
                **bindings,
                "docx_audit_sha256": digest(docx_audit.read_bytes()),
                "docx_audit_bindings": bindings,
                "failures": [],
            }
        )
        self.bind_compile_to_output(output, compile_report)
        contact = compile_report["contact_sheets"][0]
        self.write_json(
            output / "visual-review.json",
            {
                "status": "passed",
                "compile_audit_sha256": digest(
                    (output / "compile-audit.json").read_bytes()
                ),
                "pdf": compile_report["pdf"],
                "pdf_sha256": compile_report["pdf_sha256"],
                "page_count": 1,
                "reviewed_pages": [1],
                "contact_sheets_inspected": [contact["path"]],
                "contact_sheets_sha256": {contact["path"]: contact["sha256"]},
                "notes": "Inspected complete DOCX-derived fixture.",
                "failures": [],
            },
        )
        return work_dir, compile_report

    def review_command(self, work_dir: Path) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            "record_visual_review.py",
            work_dir,
            "--status",
            "passed",
            "--reviewed-pages",
            "all",
            "--notes",
            "Inspected all output pages and the complete contact sheet.",
        )

    def test_contact_tree_link_is_rejected_without_touching_external_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-contact-") as temp:
            root = Path(temp)
            work_dir, _ = self.make_review_work(root)
            output = work_dir / "output"
            old_review = b"old review\n"
            old_qa = b"old qa\n"
            (output / "visual-review.json").write_bytes(old_review)
            (output / "qa-report.json").write_bytes(old_qa)
            contact = output / "contact"
            saved = root / "saved-contact"
            contact.rename(saved)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.bin"
            sentinel.write_bytes(b"outside")
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(contact), str(outside)],
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
                os.symlink(outside, contact, target_is_directory=True)
            try:
                completed = self.review_command(work_dir)
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(
                    any(
                        marker in completed.stdout + completed.stderr
                        for marker in ("symbolic links", "reparse points")
                    )
                )
                self.assertEqual(sentinel.read_bytes(), b"outside")
                self.assertEqual((output / "visual-review.json").read_bytes(), old_review)
                self.assertEqual((output / "qa-report.json").read_bytes(), old_qa)
            finally:
                if os.path.lexists(contact):
                    os.rmdir(contact) if os.name == "nt" else contact.unlink()
                saved.rename(contact)

    def test_compile_preflight_rejects_hardlinked_fixed_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-hardlink-") as temp:
            root = Path(temp)
            work = root / "work"
            output = work / "output"
            output.mkdir(parents=True)
            tex = output / "fixture.tex"
            tex.write_bytes(b"plain TeX fixture")
            self.write_json(output / "output-audit.json", {"status": "passed"})
            self.write_json(
                output / "build-manifest.json",
                {"latex": tex.name, "latex_sha256": digest(tex.read_bytes())},
            )
            outside = root / "outside-audit.json"
            outside.write_bytes(b"outside audit\n")
            os.link(outside, output / "compile-audit.json")

            completed = self.run_script(
                "compile_pdf.py", work, "--force"
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("hard-linked", completed.stdout + completed.stderr)
            self.assertEqual(outside.read_bytes(), b"outside audit\n")

    def test_partial_compile_publication_removes_old_passed_gates_first(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-partial-publish-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            output = work / "output"
            build_dir = output / "build"
            renders_dir = output / "pdf-renders"
            contact_dir = output / "contact"
            renders_dir.mkdir()
            contact_dir.mkdir()
            (renders_dir / "old-render.png").write_bytes(b"old render\n")
            (contact_dir / "old-contact.png").write_bytes(b"old contact\n")

            stage_root = work / ".compile-stage-test"
            stage_build = stage_root / "build"
            stage_renders = stage_root / "renders"
            stage_contact = stage_root / "contact"
            for directory in (stage_build, stage_renders, stage_contact):
                directory.mkdir(parents=True)
            (stage_build / "fixture.pdf").write_bytes(b"new compiled PDF\n")
            (stage_renders / "page-0001.png").write_bytes(b"new render\n")
            (stage_contact / "contact-001.png").write_bytes(b"new contact\n")

            real_publish = compile_pdf_module._publish_flat_directory
            publish_targets: list[Path] = []

            def fail_second_publish(source: Path, target: Path, boundary: Path) -> None:
                publish_targets.append(target)
                if len(publish_targets) == 2:
                    raise OSError("injected second publication failure")
                real_publish(source, target, boundary)

            outside_before = paths["outside"].read_bytes()
            with patch.object(
                compile_pdf_module,
                "_publish_flat_directory",
                side_effect=fail_second_publish,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected second publication failure"
                ):
                    compile_pdf_module._publish_compile_stage(
                        work_dir=work,
                        compile_audit_path=output / "compile-audit.json",
                        visual_review_path=output / "visual-review.json",
                        qa_report_path=output / "qa-report.json",
                        build_dir=build_dir,
                        renders_dir=renders_dir,
                        contact_dir=contact_dir,
                        stage_build=stage_build,
                        stage_renders=stage_renders,
                        stage_contact=stage_contact,
                        report={"status": "needs_visual_review"},
                        force=True,
                    )

            self.assertEqual(publish_targets, [build_dir, renders_dir])
            self.assertEqual(
                (build_dir / "fixture.pdf").read_bytes(), b"new compiled PDF\n"
            )
            for name in (
                "compile-audit.json",
                "visual-review.json",
                "qa-report.json",
            ):
                self.assertFalse(os.path.lexists(output / name), name)
            self.assertEqual(paths["outside"].read_bytes(), outside_before)

            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            gate_statuses = json.loads(status.stdout)["gate_statuses"]
            for name in ("compile_audit", "visual_review", "qa_report"):
                self.assertIsNone(gate_statuses[name], name)

    def test_latex_compile_rejects_every_stale_output_binding_before_discovery(
        self,
    ) -> None:
        for tamper_kind in ("markdown", "asset", "build-manifest"):
            with self.subTest(tamper_kind=tamper_kind), tempfile.TemporaryDirectory(
                prefix=f"compile-output-{tamper_kind}-"
            ) as temp:
                root = Path(temp)
                work, paths = self.make_compile_work(root)
                output = work / "output"
                frozen = {
                    name: paths[name].read_bytes()
                    for name in ("docx", "latex_pdf", "docx_pdf", "outside")
                }
                self.tamper_frozen_output(output, tamper_kind)

                completed, discovery = self.run_with_discovery_trap(
                    root, "compile_pdf.py", work, "--force"
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(
                    discovery.exists(), completed.stdout + completed.stderr
                )
                report = json.loads(
                    (output / "compile-audit.json").read_text(encoding="utf-8")
                )
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["stage"], "output-audit")
                self.assertFalse((output / "visual-review.json").exists())
                self.assertFalse((output / "qa-report.json").exists())
                for name, payload in frozen.items():
                    self.assertEqual(paths[name].read_bytes(), payload)

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") is not None,
        "PyMuPDF is required to import the DOCX compile entry point",
    )
    def test_docx_compile_rejects_every_stale_output_binding_before_discovery(
        self,
    ) -> None:
        for tamper_kind in ("markdown", "asset", "build-manifest"):
            with self.subTest(tamper_kind=tamper_kind), tempfile.TemporaryDirectory(
                prefix=f"docx-output-{tamper_kind}-"
            ) as temp:
                root = Path(temp)
                work, paths = self.make_compile_work(root)
                output = work / "output"
                frozen = {
                    name: paths[name].read_bytes()
                    for name in ("docx", "latex_pdf", "docx_pdf", "outside")
                }
                self.tamper_frozen_output(output, tamper_kind)

                completed, discovery = self.run_with_discovery_trap(
                    root,
                    "compile_docx_pdf.py",
                    paths["docx"],
                    paths["docx_pdf"],
                    "--render-dir",
                    output / "pdf-renders",
                    "--audit-output",
                    output / "compile-audit.json",
                    "--work-dir",
                    work,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(
                    discovery.exists(), completed.stdout + completed.stderr
                )
                report = json.loads(
                    (output / "compile-audit.json").read_text(encoding="utf-8")
                )
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["stage"], "output-audit")
                self.assertFalse((output / "visual-review.json").exists())
                self.assertFalse((output / "qa-report.json").exists())
                for name, payload in frozen.items():
                    self.assertEqual(paths[name].read_bytes(), payload)

    @unittest.skipUnless(
        importlib.util.find_spec("fitz") is not None,
        "PyMuPDF is required to import the DOCX compile entry point",
    )
    def test_docx_compile_rejects_stale_docx_audit_before_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="docx-audit-stale-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            output = work / "output"
            profile_path = work / "profile.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            build_path = output / "build-manifest.json"
            self.write_json(
                output / "docx-audit.json",
                {
                    "status": "passed",
                    "profile": profile["id"],
                    "profile_file_sha256": digest(profile_path.read_bytes()),
                    "build_manifest_sha256": digest(build_path.read_bytes()),
                    "output_audit_sha256": digest(
                        (output / "output-audit.json").read_bytes()
                    ),
                    "docx": str(paths["docx"]),
                    "docx_sha256": "0" * 64,
                    **docx_audit_evidence(),
                    "failures": [],
                },
            )
            frozen = {
                name: paths[name].read_bytes()
                for name in ("docx", "latex_pdf", "docx_pdf", "outside")
            }

            completed, discovery = self.run_with_discovery_trap(
                root,
                "compile_docx_pdf.py",
                paths["docx"],
                paths["docx_pdf"],
                "--render-dir",
                output / "pdf-renders",
                "--audit-output",
                output / "compile-audit.json",
                "--work-dir",
                work,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(discovery.exists(), completed.stdout + completed.stderr)
            report = json.loads(
                (output / "compile-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["stage"], "docx-audit")
            self.assertFalse((output / "visual-review.json").exists())
            self.assertFalse((output / "qa-report.json").exists())
            for name, payload in frozen.items():
                self.assertEqual(paths[name].read_bytes(), payload)

    def test_broken_review_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-broken-") as temp:
            root = Path(temp)
            work_dir, _ = self.make_review_work(root)
            output = work_dir / "output"
            missing = root / "missing-review.json"
            try:
                os.symlink(missing, output / "visual-review.json")
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")

            completed = self.review_command(work_dir)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("symbolic links", completed.stdout + completed.stderr)
            self.assertFalse(missing.exists())

    def test_visual_and_final_producers_reject_hardlinked_reports(self) -> None:
        for producer, filename in (
            ("visual", "visual-review.json"),
            ("final", "qa-report.json"),
        ):
            with self.subTest(producer=producer), tempfile.TemporaryDirectory(
                prefix=f"{producer}-report-hardlink-"
            ) as temp:
                root = Path(temp)
                work_dir, _ = self.make_finalizable_work(root)
                output = work_dir / "output"
                report_path = output / filename
                report_path.unlink(missing_ok=True)
                payload = f"outside {producer} report\n".encode("utf-8")
                outside = root / f"outside-{filename}"
                outside.write_bytes(payload)
                os.link(outside, report_path)

                completed = (
                    self.review_command(work_dir)
                    if producer == "visual"
                    else self.run_script("finalize_qa.py", work_dir)
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("hard-linked", completed.stdout + completed.stderr)
                self.assertEqual(outside.read_bytes(), payload)

    def test_final_deliverable_traversal_invalidates_old_qa(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-traversal-") as temp:
            root = Path(temp)
            work = root / "work"
            output = work / "output"
            contact = output / "contact"
            translation = work / "translation"
            contact.mkdir(parents=True)
            translation.mkdir()
            pdf_bytes = b"final pdf"
            contact_bytes = b"contact"
            (output / "final.pdf").write_bytes(pdf_bytes)
            (contact / "contact-001.png").write_bytes(contact_bytes)
            self.write_source_gate(work)
            self.write_translation_gate(work)
            contact_record = {
                "path": "contact/contact-001.png",
                "sha256": digest(contact_bytes),
                "first_page": 1,
                "last_page": 1,
            }
            self.write_json(
                output / "compile-audit.json",
                {
                    "status": "passed",
                    "automated_status": "passed",
                    "pdf": "final.pdf",
                    "pdf_sha256": digest(pdf_bytes),
                    "page_count": 1,
                    "contact_sheets": [contact_record],
                    "failures": [],
                },
            )
            self.write_json(
                output / "visual-review.json",
                {
                    "status": "passed",
                    "pdf_sha256": digest(pdf_bytes),
                    "reviewed_pages": [1],
                    "contact_sheets_inspected": [contact_record["path"]],
                    "contact_sheets_sha256": {
                        contact_record["path"]: contact_record["sha256"]
                    },
                    "notes": "Inspected the complete output.",
                    "failures": [],
                },
            )
            self.write_json(
                output / "build-manifest.json",
                {"markdown": "../outside.md", "latex": None},
            )
            self.write_output_gate(output, artifact_bindings={"assets": []})
            compile_report = json.loads(
                (output / "compile-audit.json").read_text(encoding="utf-8")
            )
            self.bind_compile_to_output(output, compile_report)
            self.write_json(
                output / "visual-review.json",
                {
                    "status": "passed",
                    "compile_audit_sha256": digest(
                        (output / "compile-audit.json").read_bytes()
                    ),
                    "pdf": compile_report["pdf"],
                    "pdf_sha256": compile_report["pdf_sha256"],
                    "page_count": compile_report["page_count"],
                    "reviewed_pages": [1],
                    "contact_sheets_inspected": [contact_record["path"]],
                    "contact_sheets_sha256": {
                        contact_record["path"]: contact_record["sha256"]
                    },
                    "notes": "Inspected the complete output.",
                    "failures": [],
                },
            )
            outside = root / "outside.md"
            outside.write_bytes(b"outside")
            qa_path = output / "qa-report.json"
            qa_path.write_bytes(b"old qa\n")

            completed = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("build Markdown path", completed.stdout + completed.stderr)
            self.assertEqual(outside.read_bytes(), b"outside")
            self.assertNotEqual(qa_path.read_bytes(), b"old qa\n")
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
            self.assertEqual(qa["status"], "failed")
            self.assertTrue(
                any("output freeze chain" in failure for failure in qa["failures"])
            )

    def test_final_qa_rejects_pdf_changed_after_visual_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-pdf-stale-") as temp:
            work, compile_report = self.make_finalizable_work(Path(temp))
            output = work / "output"
            pdf = output / compile_report["pdf"]
            contact = output / compile_report["contact_sheets"][0]["path"]
            compile_bytes = (output / "compile-audit.json").read_bytes()
            contact_bytes = contact.read_bytes()

            first = self.run_script("finalize_qa.py", work)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(
                json.loads((output / "qa-report.json").read_text(encoding="utf-8"))[
                    "status"
                ],
                "passed",
            )

            pdf.write_bytes(b"changed after automated and visual QA")
            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            gate_statuses = json.loads(status.stdout)["gate_statuses"]
            self.assertEqual(gate_statuses["visual_review"], "stale")
            self.assertEqual(gate_statuses["qa_report"], "stale")

            second = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(second.returncode, 0)
            qa = json.loads((output / "qa-report.json").read_text(encoding="utf-8"))
            self.assertEqual(qa["status"], "failed")
            self.assertIn(
                "compiled pdf changed after automated compile QA", qa["failures"]
            )
            self.assertEqual((output / "compile-audit.json").read_bytes(), compile_bytes)
            self.assertEqual(contact.read_bytes(), contact_bytes)

    def test_rebuilt_outputs_make_old_compile_visual_and_qa_stale(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-output-rebuilt-") as temp:
            root = Path(temp)
            work, compile_report = self.make_finalizable_work(root)
            output = work / "output"
            pdf = output / str(compile_report["pdf"])
            contact = output / str(compile_report["contact_sheets"][0]["path"])
            outside = root / "outside.bin"
            outside.write_bytes(b"outside sentinel\n")

            first = self.run_script("finalize_qa.py", work)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            old_qa = (output / "qa-report.json").read_bytes()
            compile_bytes = (output / "compile-audit.json").read_bytes()
            visual_bytes = (output / "visual-review.json").read_bytes()
            frozen = {
                "pdf": pdf.read_bytes(),
                "contact": contact.read_bytes(),
                "outside": outside.read_bytes(),
            }

            build_path = output / "build-manifest.json"
            build = json.loads(build_path.read_text(encoding="utf-8"))
            build["generation"] = 2
            self.write_json(build_path, build)
            self.write_output_gate(output)

            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            gate_statuses = json.loads(status.stdout)["gate_statuses"]
            self.assertEqual(gate_statuses["output_audit"], "passed")
            self.assertEqual(gate_statuses["compile_audit"], "stale")
            self.assertEqual(gate_statuses["visual_review"], "stale")
            self.assertEqual(gate_statuses["qa_report"], "stale")

            second = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(second.returncode, 0)
            qa = json.loads(
                (output / "qa-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(qa["status"], "failed")
            self.assertTrue(
                any(
                    "compile output freeze chain" in failure
                    for failure in qa["failures"]
                )
            )
            self.assertNotEqual((output / "qa-report.json").read_bytes(), old_qa)
            self.assertEqual((output / "compile-audit.json").read_bytes(), compile_bytes)
            self.assertEqual((output / "visual-review.json").read_bytes(), visual_bytes)
            self.assertEqual(pdf.read_bytes(), frozen["pdf"])
            self.assertEqual(contact.read_bytes(), frozen["contact"])
            self.assertEqual(outside.read_bytes(), frozen["outside"])

    def test_v1_pipeline_docx_compile_and_finalize_use_work_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-pipeline-docx-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            output = work / "output"
            self.write_source_gate(work)
            self.write_translation_gate(work)
            for name in (
                "compile-audit.json",
                "visual-review.json",
                "qa-report.json",
            ):
                (output / name).unlink(missing_ok=True)
            outside = root / "outside.bin"
            outside.write_bytes(b"outside sentinel\n")
            calls: list[tuple[str, tuple[str, ...]]] = []

            def option(arguments: tuple[str, ...], name: str) -> str:
                return arguments[arguments.index(name) + 1]

            def fake_run_script(script: str, *arguments: str) -> None:
                calls.append((script, arguments))
                self.assertIn("--work-dir", arguments)
                if script == "build_docx.py":
                    Path(arguments[1]).write_bytes(b"pipeline V1 DOCX\n")
                    return
                if script == "audit_docx.py":
                    self.write_json(
                        Path(option(arguments, "--output")),
                        {
                            "status": "passed",
                            "profile": load_profile("assignment-en-zh")["id"],
                            "profile_file_sha256": digest(
                                (work / "profile.json").read_bytes()
                            ),
                            "build_manifest_sha256": digest(
                                (output / "build-manifest.json").read_bytes()
                            ),
                            "output_audit_sha256": digest(
                                (output / "output-audit.json").read_bytes()
                            ),
                            "docx": arguments[0],
                            "docx_sha256": digest(Path(arguments[0]).read_bytes()),
                            **docx_audit_evidence(),
                            "failures": [],
                        },
                    )
                    return
                if script == "compile_docx_pdf.py":
                    docx = Path(arguments[0])
                    pdf = Path(arguments[1])
                    pdf.write_bytes(b"pipeline V1 PDF\n")
                    contact_dir = output / "contact"
                    contact_dir.mkdir(exist_ok=True)
                    contact = contact_dir / "contact-001.png"
                    contact.write_bytes(b"pipeline contact sheet\n")
                    docx_audit_path = output / "docx-audit.json"
                    docx_audit = json.loads(
                        docx_audit_path.read_text(encoding="utf-8")
                    )
                    docx_bindings = {
                        field: docx_audit[field]
                        for field in (
                            "profile",
                            "profile_file_sha256",
                            "build_manifest_sha256",
                            "output_audit_sha256",
                            "docx_sha256",
                        )
                    }
                    self.write_json(
                        Path(option(arguments, "--audit-output")),
                        {
                            "status": "passed",
                            "automated_status": "passed",
                            "docx": docx.name,
                            "docx_sha256": digest(docx.read_bytes()),
                            "pdf": pdf.name,
                            "pdf_sha256": digest(pdf.read_bytes()),
                            "page_count": 1,
                            **docx_compile_evidence(),
                            "contact_sheets": [
                                {
                                    "path": "contact/contact-001.png",
                                    "sha256": digest(contact.read_bytes()),
                                    "first_page": 1,
                                    "last_page": 1,
                                }
                            ],
                            "build_manifest_sha256": digest(
                                (output / "build-manifest.json").read_bytes()
                            ),
                            "output_audit_sha256": digest(
                                (output / "output-audit.json").read_bytes()
                            ),
                            **docx_bindings,
                            "docx_audit_sha256": digest(
                                docx_audit_path.read_bytes()
                            ),
                            "docx_audit_bindings": docx_bindings,
                            "warnings": [],
                            "failures": [],
                        },
                    )
                    return
                raise AssertionError(f"unexpected pipeline script: {script}")

            self.run_pipeline_main(
                fake_run_script,
                "docx",
                work,
                "--markdown",
                paths["markdown"],
            )
            build_call = next(call for call in calls if call[0] == "build_docx.py")
            audit_call = next(call for call in calls if call[0] == "audit_docx.py")
            self.assertEqual(option(build_call[1], "--work-dir"), str(work))
            self.assertEqual(option(audit_call[1], "--work-dir"), str(work))

            custom_build = work / "custom-docx-stage"
            calls.clear()
            self.run_pipeline_main(
                fake_run_script,
                "docx",
                work,
                "--markdown",
                paths["markdown"],
                "--build-dir",
                custom_build,
            )
            custom_call = next(call for call in calls if call[0] == "build_docx.py")
            custom_audit = next(call for call in calls if call[0] == "audit_docx.py")
            self.assertEqual(
                option(custom_call[1], "--work-dir"), str(work)
            )
            self.assertEqual(option(custom_call[1], "--build-dir"), str(custom_build))
            self.assertEqual(option(custom_audit[1], "--work-dir"), str(work))

            calls.clear()
            self.run_pipeline_main(fake_run_script, "compile-docx", work)
            compile_call = next(
                call for call in calls if call[0] == "compile_docx_pdf.py"
            )
            self.assertEqual(option(compile_call[1], "--work-dir"), str(work))
            compile_report = json.loads(
                (output / "compile-audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                compile_report["build_manifest_sha256"],
                digest((output / "build-manifest.json").read_bytes()),
            )
            self.assertEqual(
                compile_report["output_audit_sha256"],
                digest((output / "output-audit.json").read_bytes()),
            )
            contact = compile_report["contact_sheets"][0]
            self.write_json(
                output / "visual-review.json",
                {
                    "status": "passed",
                    "compile_audit_sha256": digest(
                        (output / "compile-audit.json").read_bytes()
                    ),
                    "pdf": compile_report["pdf"],
                    "pdf_sha256": compile_report["pdf_sha256"],
                    "page_count": compile_report["page_count"],
                    "reviewed_pages": [1],
                    "contact_sheets_inspected": [contact["path"]],
                    "contact_sheets_sha256": {
                        contact["path"]: contact["sha256"]
                    },
                    "notes": "Inspected the complete V1 pipeline fixture.",
                    "failures": [],
                },
            )

            finalized = self.run_script("pipeline.py", "finalize", work)
            self.assertEqual(
                finalized.returncode, 0, finalized.stdout + finalized.stderr
            )
            qa = json.loads(
                (output / "qa-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(qa["status"], "passed")
            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["compile_audit"],
                "passed",
            )
            self.assertEqual(outside.read_bytes(), b"outside sentinel\n")

            escaped_build = root / "outside-build"
            escaped_build.mkdir()
            escaped_sentinel = escaped_build / "sentinel.bin"
            escaped_sentinel.write_bytes(b"escaped sentinel\n")
            calls.clear()
            with self.assertRaisesRegex(ValueError, "must stay inside WORK"):
                self.run_pipeline_main(
                    fake_run_script,
                    "docx",
                    work,
                    "--markdown",
                    paths["markdown"],
                    "--build-dir",
                    escaped_build,
                )
            self.assertFalse(calls)
            self.assertEqual(escaped_sentinel.read_bytes(), b"escaped sentinel\n")

    def test_v1_pipeline_docx_rejects_hardlinked_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-docx-hardlink-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            self.write_source_gate(work)
            outside = root / "outside.docx"
            outside.write_bytes(b"outside DOCX sentinel\n")
            paths["docx"].unlink()
            os.link(outside, paths["docx"])
            calls: list[str] = []

            with self.assertRaisesRegex(ValueError, "hard-linked"):
                self.run_pipeline_main(
                    lambda script, *_arguments: calls.append(script),
                    "docx",
                    work,
                    "--markdown",
                    paths["markdown"],
                )
            self.assertFalse(calls)
            self.assertEqual(outside.read_bytes(), b"outside DOCX sentinel\n")

    def test_v1_pipeline_compile_rejects_symlinked_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-compile-symlink-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            self.write_source_gate(work)
            outside = root / "outside.pdf"
            outside.write_bytes(b"outside PDF sentinel\n")
            compile_target = work / "output" / "fixture.pdf"
            compile_target.unlink()
            try:
                os.symlink(outside, compile_target)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) in {5, 1314}:
                    self.skipTest(f"file symlink privilege unavailable: {exc}")
                self.fail(f"file symlink regression setup failed: {exc}")
            calls: list[str] = []

            with self.assertRaisesRegex(ValueError, "symbolic links"):
                self.run_pipeline_main(
                    lambda script, *_arguments: calls.append(script),
                    "compile-docx",
                    work,
                )
            self.assertFalse(calls)
            self.assertEqual(outside.read_bytes(), b"outside PDF sentinel\n")

    def test_pipeline_status_rejects_output_junction(self) -> None:
        with tempfile.TemporaryDirectory(prefix="compile-final-status-") as temp:
            root = Path(temp)
            work = root / "work"
            work.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "qa-report.json"
            sentinel.write_bytes(b'{"status":"passed"}\n')
            output = work / "output"
            if os.name == "nt":
                created = subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(output), str(outside)],
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
                os.symlink(outside, output, target_is_directory=True)
            try:
                completed = self.run_script("pipeline.py", "status", work)
                self.assertNotEqual(completed.returncode, 0)
                self.assertTrue(
                    any(
                        marker in completed.stdout + completed.stderr
                        for marker in ("symbolic links", "reparse points")
                    )
                )
                self.assertEqual(sentinel.read_bytes(), b'{"status":"passed"}\n')
            finally:
                if os.path.lexists(output):
                    os.rmdir(output) if os.name == "nt" else output.unlink()

    def test_visual_and_qa_shapes_reject_old_false_positive_forms(self) -> None:
        for reviewed_pages in ("1", [True], [1, 1], None):
            with self.subTest(reviewed_pages=reviewed_pages), tempfile.TemporaryDirectory(
                prefix="visual-shape-"
            ) as temp:
                work, _compile = self.make_finalizable_work(Path(temp))
                output = work / "output"
                visual_path = output / "visual-review.json"
                visual = json.loads(visual_path.read_text(encoding="utf-8"))
                visual["reviewed_pages"] = reviewed_pages
                self.write_json(visual_path, visual)
                status = self.run_script("pipeline.py", "status", work)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertEqual(
                    json.loads(status.stdout)["gate_statuses"]["visual_review"],
                    "stale",
                )

        for mutation in (
            "deliverable",
            "source-hash",
            "failures",
            "missing-status-with-automated-pass",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"qa-shape-{mutation}-"
            ) as temp:
                work, _compile = self.make_finalizable_work(Path(temp))
                output = work / "output"
                finalized = self.run_script("finalize_qa.py", work)
                self.assertEqual(finalized.returncode, 0, finalized.stdout + finalized.stderr)
                qa_path = output / "qa-report.json"
                qa = json.loads(qa_path.read_text(encoding="utf-8"))
                if mutation == "deliverable":
                    qa["deliverables"]["banana"] = next(iter(qa["deliverables"].values()))
                elif mutation == "source-hash":
                    qa["source_pdf_sha256"] = False
                elif mutation == "failures":
                    qa["failures"] = False
                else:
                    qa.pop("status")
                    qa["automated_status"] = "passed"
                self.write_json(qa_path, qa)
                status = self.run_script("pipeline.py", "status", work)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn(
                    json.loads(status.stdout)["gate_statuses"]["qa_report"],
                    {"invalid", "stale"},
                )

    def test_visual_gate_rejects_duplicate_contact_path_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="visual-duplicate-contact-") as temp:
            work, compile_report = self.make_finalizable_work(Path(temp))
            output = work / "output"
            contact = compile_report["contact_sheets"][0]
            compile_report["page_count"] = 2
            compile_report["contact_sheets"] = [
                {**contact, "first_page": 1, "last_page": 1},
                {**contact, "first_page": 2, "last_page": 2},
            ]
            self.write_json(output / "compile-audit.json", compile_report)
            self.write_json(
                output / "visual-review.json",
                {
                    "status": "passed",
                    "compile_audit_sha256": digest((output / "compile-audit.json").read_bytes()),
                    "pdf": compile_report["pdf"],
                    "pdf_sha256": compile_report["pdf_sha256"],
                    "page_count": 2,
                    "reviewed_pages": [1, 2],
                    "contact_sheets_inspected": [contact["path"], contact["path"]],
                    "contact_sheets_sha256": {contact["path"]: contact["sha256"]},
                    "notes": "Inspected duplicate synthetic evidence.",
                    "failures": [],
                },
            )
            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["visual_review"], "stale"
            )

    def test_compile_shape_and_pdf_provenance_reject_same_hash_aliases(self) -> None:
        for mutation in (
            "missing-automated",
            "wrong-status",
            "false-check",
            "reduced-checks",
            "erased-problem-evidence",
            "alternate-pdf",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"compile-provenance-{mutation}-"
            ) as temp:
                root = Path(temp)
                work, compile_report = self.make_finalizable_work(root)
                output = work / "output"
                original_pdf = output / str(compile_report["pdf"])
                original_bytes = original_pdf.read_bytes()
                outside = root / "outside.bin"
                outside.write_bytes(b"outside sentinel\n")
                if mutation == "erased-problem-evidence":
                    build_path = output / "build-manifest.json"
                    build = json.loads(build_path.read_text(encoding="utf-8"))
                    build["problem_ids"] = ["fixture-problem"]
                    self.write_json(build_path, build)
                    self.write_output_gate(output)
                    compile_report["problem_ids_expected"] = ["fixture-problem"]
                if mutation == "missing-automated":
                    compile_report.pop("automated_status")
                elif mutation == "wrong-status":
                    compile_report["status"] = "passed"
                elif mutation == "false-check":
                    compile_report["checks"]["all_pages_rendered"] = False
                elif mutation == "reduced-checks":
                    compile_report["checks"] = {"pdf_created": True}
                elif mutation == "erased-problem-evidence":
                    compile_report["problem_ids_expected"] = []
                else:
                    alternate = output / "fixture.pdf"
                    alternate.write_bytes(original_bytes)
                    compile_report["pdf"] = alternate.name
                    compile_report["pdf_sha256"] = digest(alternate.read_bytes())
                self.bind_compile_to_output(output, compile_report)
                (output / "qa-report.json").write_bytes(b"old QA sentinel\n")

                status = self.run_script("pipeline.py", "status", work)
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                self.assertIn(
                    json.loads(status.stdout)["gate_statuses"]["compile_audit"],
                    {"invalid", "stale"},
                )

                finalized = self.run_script("finalize_qa.py", work)
                self.assertNotEqual(finalized.returncode, 0)
                qa = json.loads(
                    (output / "qa-report.json").read_text(encoding="utf-8")
                )
                self.assertEqual(qa["status"], "failed")

                reviewed = self.review_command(work)
                self.assertNotEqual(reviewed.returncode, 0)
                self.assertFalse((output / "visual-review.json").exists())
                self.assertFalse((output / "qa-report.json").exists())
                self.assertEqual(original_pdf.read_bytes(), original_bytes)
                self.assertEqual(outside.read_bytes(), b"outside sentinel\n")

    def test_v2_compile_docx_binding_rejects_staging_path_even_with_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v2-docx-staging-binding-") as temp:
            work = Path(temp) / "work"
            staging = work / "output" / "docx-build"
            staging.mkdir(parents=True)
            staged_docx = staging / "styled.docx"
            staged_docx.write_bytes(b"same audited DOCX bytes\n")
            compile_report = {
                "status": "passed",
                "automated_status": "passed",
                "docx": "docx-build/styled.docx",
                "docx_sha256": digest(staged_docx.read_bytes()),
                "failures": [],
            }

            _expected, errors = validate_v2_compile_docx_binding(
                work, compile_report
            )

            self.assertTrue(errors)
            self.assertTrue(
                any("direct output DOCX child" in error for error in errors),
                errors,
            )

    def test_docx_audit_checks_cannot_be_relabelled_or_reduced(self) -> None:
        for mutation in ("false", "reduced"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix=f"docx-audit-check-shape-{mutation}-"
            ) as temp:
                work, compile_report = self.make_docx_finalizable_work(Path(temp))
                output = work / "output"
                audit_path = output / "docx-audit.json"
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                if mutation == "false":
                    audit["checks"]["docx_opens"] = False
                else:
                    audit["checks"] = {"docx_opens": True}
                audit["status"] = "passed"
                audit["failures"] = []
                self.write_json(audit_path, audit)
                compile_report["docx_audit_sha256"] = digest(audit_path.read_bytes())
                compile_report["docx_audit_bindings"] = {
                    field: audit[field]
                    for field in (
                        "profile",
                        "profile_file_sha256",
                        "build_manifest_sha256",
                        "output_audit_sha256",
                        "docx_sha256",
                    )
                }
                self.bind_compile_to_output(output, compile_report)

                _expected, errors = validate_compile_docx_binding(
                    work, compile_report, audit_path
                )

                self.assertTrue(errors)
                self.assertTrue(
                    any("check" in error and ("true" in error or "contract" in error) for error in errors),
                    errors,
                )

    def test_visual_and_final_reject_staging_docx_same_hash_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="docx-staging-consumers-") as temp:
            root = Path(temp)
            work, compile_report = self.make_docx_finalizable_work(root)
            output = work / "output"
            original_docx = output / str(compile_report["docx"])
            original_pdf = output / str(compile_report["pdf"])
            staging = output / "docx-build"
            staging.mkdir()
            staged_docx = staging / "styled.docx"
            staged_docx.write_bytes(original_docx.read_bytes())
            compile_report["docx"] = "docx-build/styled.docx"
            compile_report["docx_sha256"] = digest(staged_docx.read_bytes())
            self.bind_compile_to_output(output, compile_report)

            status = self.run_script("pipeline.py", "status", work)
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            self.assertEqual(
                json.loads(status.stdout)["gate_statuses"]["compile_audit"],
                "stale",
            )
            finalized = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(finalized.returncode, 0)
            self.assertEqual(
                json.loads(
                    (output / "qa-report.json").read_text(encoding="utf-8")
                )["status"],
                "failed",
            )
            reviewed = self.review_command(work)
            self.assertNotEqual(reviewed.returncode, 0)
            self.assertFalse((output / "visual-review.json").exists())
            self.assertEqual(original_docx.read_bytes(), b"audited DOCX fixture\n")
            self.assertEqual(staged_docx.read_bytes(), original_docx.read_bytes())
            self.assertEqual(original_pdf.read_bytes(), b"compiled pdf fixture")

    def test_docx_compile_path_role_aliases_fail_before_tool_discovery(self) -> None:
        for role in ("render", "target", "audit"):
            with self.subTest(role=role), tempfile.TemporaryDirectory(
                prefix=f"docx-compile-alias-{role}-"
            ) as temp:
                root = Path(temp)
                work, paths = self.make_compile_work(root)
                output = work / "output"
                before = {
                    path.relative_to(work).as_posix(): path.read_bytes()
                    for path in work.rglob("*")
                    if path.is_file()
                }
                completed, discovery = self.run_with_discovery_trap(
                    root,
                    "compile_docx_pdf.py",
                    paths["docx"],
                    output / "build-manifest.json" if role == "target" else paths["docx_pdf"],
                    "--render-dir",
                    output if role == "render" else output / "pdf-renders",
                    "--audit-output",
                    output / "output-audit.json" if role == "audit" else output / "compile-audit.json",
                    "--work-dir",
                    work,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(discovery.exists(), completed.stdout + completed.stderr)
                after = {
                    path.relative_to(work).as_posix(): path.read_bytes()
                    for path in work.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)

    def test_v1_docx_compile_rejects_docx_changed_after_audit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v1-docx-freeze-stale-") as temp:
            root = Path(temp)
            work, paths = self.make_compile_work(root)
            output = work / "output"
            profile = load_profile("assignment-en-zh")
            self.write_json(work / "profile.json", profile)
            bindings = {
                "profile": profile["id"],
                "profile_file_sha256": digest((work / "profile.json").read_bytes()),
                "build_manifest_sha256": digest((output / "build-manifest.json").read_bytes()),
                "output_audit_sha256": digest((output / "output-audit.json").read_bytes()),
                "docx_sha256": digest(paths["docx"].read_bytes()),
            }
            self.write_json(
                output / "docx-audit.json",
                {"status": "passed", "docx": str(paths["docx"]), **bindings, "failures": []},
            )
            paths["docx"].write_bytes(b"changed after DOCX audit\n")
            outside = paths["outside"].read_bytes()
            completed, discovery = self.run_with_discovery_trap(
                root,
                "compile_docx_pdf.py",
                paths["docx"],
                paths["docx_pdf"],
                "--render-dir",
                output / "pdf-renders",
                "--audit-output",
                output / "compile-audit.json",
                "--work-dir",
                work,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(discovery.exists(), completed.stdout + completed.stderr)
            report = json.loads((output / "compile-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(report["stage"], "docx-audit")
            self.assertEqual(paths["outside"].read_bytes(), outside)

    def test_job_state_v1_and_v2_finalize_freeze_same_snapshot(self) -> None:
        for schema, maker in (
            ("v1", self.make_finalizable_work),
            ("v2", self.make_docx_finalizable_work),
        ):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory(
                prefix=f"job-state-{schema}-"
            ) as temp:
                work, _compile = maker(Path(temp))
                before = {
                    path: path.read_bytes() for path in work.rglob("*") if path.is_file()
                }
                state = evaluate_job(work)
                self.assertEqual(state.final_report["status"], "passed")
                self.assertEqual(
                    before,
                    {
                        path: path.read_bytes()
                        for path in work.rglob("*")
                        if path.is_file()
                    },
                )

                completed = self.run_script("finalize_qa.py", work)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                qa = json.loads(
                    (work / "output" / "qa-report.json").read_text(encoding="utf-8")
                )
                self.assertEqual(qa, state.final_report)
                self.assertEqual(
                    evaluate_job(work).status_report["gate_statuses"]["qa_report"],
                    "passed",
                )

    def test_job_state_changed_deliverable_stales_and_refinalizes_failed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-state-deliverable-") as temp:
            work, compile_report = self.make_finalizable_work(Path(temp))
            first = self.run_script("finalize_qa.py", work)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            (work / "output" / compile_report["pdf"]).write_bytes(b"changed")

            state = evaluate_job(work)
            self.assertEqual(
                state.status_report["gate_statuses"]["visual_review"], "stale"
            )
            self.assertEqual(state.status_report["gate_statuses"]["qa_report"], "stale")
            self.assertEqual(state.final_report["status"], "failed")

            second = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(second.returncode, 0)
            qa = json.loads(
                (work / "output" / "qa-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(qa, state.final_report)

    def test_job_state_missing_docx_and_compile_status_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-state-missing-docx-") as temp:
            work, _compile = self.make_docx_finalizable_work(Path(temp))
            (work / "output" / "docx-audit.json").unlink()
            state = evaluate_job(work)
            self.assertIsNone(state.status_report["gate_statuses"]["docx_audit"])
            self.assertIn(
                f"missing docx gate: {Path('output') / 'docx-audit.json'}",
                state.final_report["failures"],
            )

        with tempfile.TemporaryDirectory(prefix="job-state-missing-status-") as temp:
            work, _compile = self.make_finalizable_work(Path(temp))
            path = work / "output" / "compile-audit.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            report.pop("automated_status")
            path.write_text(json.dumps(report), encoding="utf-8")
            state = evaluate_job(work)
            self.assertEqual(
                state.status_report["gate_statuses"]["compile_audit"], "invalid"
            )
            self.assertEqual(state.final_report["status"], "failed")

    def test_pipeline_status_accepts_standard_failed_compile_reports(self) -> None:
        writers = (
            (
                "tex",
                self.make_finalizable_work,
                lambda work, path: compile_pdf_module.write_failure(
                    path, work, "latexmk", ["synthetic compiler failure"]
                ),
            ),
            (
                "docx",
                self.make_docx_finalizable_work,
                lambda work, path: compile_docx_pdf_module._fail_work_binding(
                    work,
                    path,
                    stage="docx-audit",
                    message="synthetic binding failure",
                    cause=ValueError("stale fixture"),
                ),
            ),
        )
        for backend, maker, write_failure in writers:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory(
                prefix=f"job-state-failed-{backend}-"
            ) as temp:
                work, _compile = maker(Path(temp))
                audit = work / "output" / "compile-audit.json"
                with redirect_stdout(io.StringIO()):
                    if backend == "docx":
                        with self.assertRaises(SystemExit):
                            write_failure(work, audit)
                    else:
                        write_failure(work, audit)

                completed = self.run_script("pipeline.py", "status", work)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                status = json.loads(completed.stdout)
                self.assertEqual(status["gate_statuses"]["compile_audit"], "failed")
                self.assertNotEqual(status["next_action"], "complete")
                self.assertEqual(evaluate_job(work).final_report["status"], "failed")

    def test_v1_docx_hash_marker_cannot_downgrade_to_tex_chain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="job-state-v1-docx-marker-") as temp:
            work, _compile = self.make_docx_finalizable_work(Path(temp))
            output = work / "output"
            compile_path = output / "compile-audit.json"
            compile_report = json.loads(compile_path.read_text(encoding="utf-8"))
            compile_report.pop("docx")
            compile_report.pop("docx_audit_bindings")
            self.write_json(compile_path, compile_report)
            visual_path = output / "visual-review.json"
            visual = json.loads(visual_path.read_text(encoding="utf-8"))
            visual["compile_audit_sha256"] = digest(compile_path.read_bytes())
            self.write_json(visual_path, visual)

            state = evaluate_job(work)
            self.assertNotEqual(
                state.status_report["gate_statuses"]["compile_audit"], "passed"
            )
            self.assertEqual(state.final_report["status"], "failed")
            completed = self.run_script("finalize_qa.py", work)
            self.assertNotEqual(completed.returncode, 0)
            qa = json.loads((output / "qa-report.json").read_text(encoding="utf-8"))
            self.assertEqual(qa["status"], "failed")

    def test_pipeline_status_contains_invalid_passed_compile_metadata(self) -> None:
        mutations = (
            ("missing-pdf", lambda report: report.pop("pdf")),
            ("unsafe-pdf", lambda report: report.__setitem__("pdf", "../escape.pdf")),
            (
                "malformed-contact",
                lambda report: report.__setitem__("contact_sheets", [None]),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix=f"job-state-invalid-compile-{label}-"
            ) as temp:
                work, _compile = self.make_finalizable_work(Path(temp))
                compile_path = work / "output" / "compile-audit.json"
                compile_report = json.loads(compile_path.read_text(encoding="utf-8"))
                mutate(compile_report)
                self.write_json(compile_path, compile_report)

                completed = self.run_script("pipeline.py", "status", work)
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )
                status = json.loads(completed.stdout)
                self.assertNotEqual(status["next_action"], "complete")
                state = evaluate_job(work)
                self.assertEqual(state.final_report["status"], "failed")
                self.assertTrue(state.final_report["failures"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
