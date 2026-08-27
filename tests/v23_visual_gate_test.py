#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "skills" / "make-bilingual-study-pdf" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from PIL import Image

from audit_source import current_source_audit_bindings
from common import read_json, sha256_file, write_json
from document_ir import expected_ir
from pipeline import report_status
from profile import canonical_profile_sha256, load_profile, profile_contract
from v23_docx_test import prepare_frozen_v2_output


SCRIPT_DIR = SCRIPTS


def run_review(work_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "record_visual_review.py"),
            str(work_dir),
            "--status",
            "passed",
            "--reviewed-pages",
            "all",
            "--spot-check-pages",
            "1",
            "--notes",
            "Inspected the complete synthetic contact sheet and full-resolution page.",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def run_finalize(work_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "finalize_qa.py"), str(work_dir)],
        check=False,
        capture_output=True,
        text=True,
    )


def write_minimal_source_translation_chain(
    work_dir: Path, profile: dict
) -> tuple[str, str]:
    profile_path = work_dir / "profile.json"
    ir_path = work_dir / "document-ir.json"
    (work_dir / "blocks.jsonl").write_text("", encoding="utf-8")
    (work_dir / "oracle.txt").write_text("fixture\f", encoding="utf-8")
    (work_dir / "oracle-layout.txt").write_text(
        "fixture layout\f", encoding="utf-8"
    )
    renders = work_dir / "renders"
    renders.mkdir(exist_ok=True)
    Image.new("RGB", (2, 3), "white").save(renders / "page-1.png")
    source_contact = work_dir / "source-contact"
    source_contact.mkdir(exist_ok=True)
    source_contact_path = source_contact / "contact-1.png"
    Image.new("RGB", (2, 3), "white").save(source_contact_path)
    source_pdf_path = work_dir / "source.pdf"
    source_pdf_path.write_bytes(b"synthetic source PDF fixture\n")
    source_pdf_hash = sha256_file(source_pdf_path)
    write_json(
        work_dir / "manifest.json",
        {
            "source_pdf": str(source_pdf_path.absolute()),
            "source_sha256": source_pdf_hash,
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
                    "sha256": sha256_file(source_contact_path),
                    "first_page": 1,
                    "last_page": 1,
                }
            ],
        },
    )
    write_json(ir_path, expected_ir(work_dir, profile))
    write_json(
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
    translation_dir = work_dir / "translation"
    translation_dir.mkdir(exist_ok=True)
    (translation_dir / "requests").mkdir(exist_ok=True)
    (translation_dir / "responses").mkdir(exist_ok=True)
    glossary_path = translation_dir / "glossary.json"
    write_json(glossary_path, {"terms": []})
    plan_path = translation_dir / "plan.json"
    write_json(
        plan_path,
        {
            "schema_version": 2,
            "profile_id": profile["id"],
            "profile_sha256": canonical_profile_sha256(profile),
            "profile_file_sha256": sha256_file(profile_path),
            "document_ir_sha256": sha256_file(ir_path),
            "source_pdf_sha256": source_pdf_hash,
            "source_manifest_sha256": sha256_file(work_dir / "manifest.json"),
            "source_blocks_sha256": sha256_file(work_dir / "blocks.jsonl"),
            "source_audit_sha256": sha256_file(work_dir / "source-audit.json"),
            "glossary_sha256": sha256_file(glossary_path),
            "target_language": profile["translation"]["target_language"],
            "batch_count": 0,
            "batches": [],
            "expected_segment_count": 0,
            "expected_ids": [],
        },
    )
    merged_path = translation_dir / "translations-merged.jsonl"
    merged_path.write_text("", encoding="utf-8")
    translation_audit_path = translation_dir / "translation-audit.json"
    write_json(
        translation_audit_path,
        {
            "status": "passed",
            "plan_sha256": sha256_file(plan_path),
            "merged_sha256": sha256_file(merged_path),
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
    return source_pdf_hash, sha256_file(translation_audit_path)


def write_output_gate(output_dir: Path) -> None:
    build_path = output_dir / "build-manifest.json"
    build = read_json(build_path)
    artifact_bindings = {"assets": []}
    for field in ("markdown", "latex"):
        relative = build.get(field)
        if relative is not None:
            artifact_bindings[field] = {
                "path": relative,
                "sha256": sha256_file(output_dir / relative),
            }
    write_json(
        output_dir / "output-audit.json",
        {
            "status": "passed",
            "build_manifest_sha256": sha256_file(build_path),
            "artifact_bindings": artifact_bindings,
            "markdown": build.get("markdown"),
            "latex": build.get("latex"),
            "asset_count": 0,
            "block_count": build.get("block_count"),
            "disposition_counts": build.get("disposition_counts"),
            "semantic_constraint_checks": {
                item["id"]: True
                for item in profile_contract(
                    read_json(output_dir.parent / "profile.json")
                )["constraints"]
            },
            "failures": [],
        },
    )


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
            "all_textual_role_occurrences_present": True,
            "visual_role_occurrences_present": True,
            "profile_target_text_extractable": True,
        },
    }


def main() -> None:
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bilingual-v23-visual-gate-") as temp:
        work_dir = Path(temp)
        output_dir = work_dir / "output"
        output_dir.mkdir()
        profile = load_profile("assignment-en-zh")
        write_json(work_dir / "profile.json", profile)
        write_json(work_dir / "document-ir.json", {"fixture": True})
        build_dir = output_dir / "build"
        build_dir.mkdir()
        latex = output_dir / "fixture.tex"
        latex.write_text("fixture\n", encoding="utf-8")
        pdf = build_dir / "fixture.pdf"
        pdf.write_bytes(b"synthetic PDF identity fixture")
        _source_hash, translation_hash = write_minimal_source_translation_chain(
            work_dir, profile
        )
        build_path = output_dir / "build-manifest.json"
        write_json(
            build_path,
            {
                "markdown": None,
                "latex": latex.name,
                "latex_sha256": sha256_file(latex),
                "profile_id": profile["id"],
                "profile_file_sha256": sha256_file(work_dir / "profile.json"),
                "document_ir_sha256": sha256_file(
                    work_dir / "document-ir.json"
                ),
                "assets": [],
                "block_count": 0,
                "disposition_counts": {},
                "role_inventory": {
                    role: {"occurrence_count": 0}
                    for role in ("problem", "example", "tip")
                },
                "problem_ids": [],
                "external_uris": [],
                "translation_audit_sha256": translation_hash,
            },
        )
        output_audit_path = output_dir / "output-audit.json"
        write_output_gate(output_dir)
        base_audit = {
            "status": "needs_visual_review",
            "automated_status": "passed",
            "pdf": pdf.relative_to(output_dir).as_posix(),
            "pdf_sha256": sha256_file(pdf),
            "page_count": 1,
            **tex_compile_evidence(),
            "build_manifest_sha256": sha256_file(build_path),
            "output_audit_sha256": sha256_file(output_audit_path),
            "failures": [],
        }

        write_json(output_dir / "compile-audit.json", base_audit)
        missing = run_review(work_dir)
        assert missing.returncode != 0
        assert "no contact sheets" in (missing.stdout + missing.stderr), (
            missing.stdout + missing.stderr
        )
        assert not (output_dir / "visual-review.json").exists()
        results.append("visual approval rejects a compile report with no contact sheets")

        contact_dir = output_dir / "contact"
        contact_dir.mkdir()
        contact = contact_dir / "contact-001.png"
        Image.new("RGB", (80, 80), "white").save(contact)
        record = {
            "path": "contact/contact-001.png",
            "sha256": sha256_file(contact),
            "first_page": 1,
            "last_page": 1,
        }
        audit = dict(base_audit)
        audit["contact_sheets"] = [record]
        write_json(output_dir / "compile-audit.json", audit)
        passed = run_review(work_dir)
        assert passed.returncode == 0, passed.stdout + passed.stderr
        review = read_json(output_dir / "visual-review.json")
        assert review["status"] == "passed"
        assert review["contact_sheets_inspected"] == [record["path"]]
        assert review["contact_sheets_sha256"] == {
            record["path"]: record["sha256"]
        }
        results.append("visual approval binds complete contact sheets and final PDF bytes")

        contact.write_bytes(b"changed after review")
        changed = run_review(work_dir)
        assert changed.returncode != 0
        assert "missing or changed" in (changed.stdout + changed.stderr)
        results.append("visual approval rejects a changed contact sheet")

    with tempfile.TemporaryDirectory(prefix="bilingual-v23-docx-freeze-") as temp:
        work_dir = Path(temp)
        profile_path = work_dir / "profile.json"
        profile = load_profile("lecture-notes-en-zh")
        write_json(profile_path, profile)
        _inventory, markdown_path = prepare_frozen_v2_output(
            work_dir,
            profile,
            "Theorem 1. Frozen source statement.",
            "Definition 2. Scoped anchor source statement.",
            "E equals m c squared.",
        )
        output_dir = work_dir / "output"
        ir_path = work_dir / "document-ir.json"
        docx_path = output_dir / "fixture.docx"
        pdf_path = output_dir / "fixture.pdf"
        audit_path = output_dir / "docx-audit.json"
        contact_dir = output_dir / "contact"
        contact_dir.mkdir()
        contact_path = contact_dir / "contact-001.png"
        source_pdf_hash = read_json(work_dir / "manifest.json")["source_sha256"]
        ir = read_json(ir_path)
        role_inventory = ir["inventories"]["role_inventory"]
        role_counts = {
            role: item["occurrence_count"]
            for role, item in role_inventory.items()
        }
        build_docx = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "build_docx.py"),
                str(markdown_path),
                str(docx_path),
                "--work-dir",
                str(work_dir),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert build_docx.returncode == 0, build_docx.stdout + build_docx.stderr
        audit_docx = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "audit_docx.py"),
                str(docx_path),
                "--work-dir",
                str(work_dir),
                "--output",
                str(audit_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert audit_docx.returncode == 0, audit_docx.stdout + audit_docx.stderr
        pdf_path.write_bytes(b"compiled PDF fixture")
        Image.new("RGB", (80, 80), "white").save(contact_path)
        docx_audit = read_json(audit_path)
        bindings = {
            field: docx_audit[field]
            for field in (
                "profile",
                "profile_file_sha256",
                "document_ir_sha256",
                "build_manifest_sha256",
                "output_audit_sha256",
                "docx_sha256",
            )
        }
        contact_record = {
            "path": "contact/contact-001.png",
            "sha256": sha256_file(contact_path),
            "first_page": 1,
            "last_page": 1,
        }
        nodes_by_id = {
            node["id"]: node
            for node in ir["nodes"]
            if isinstance(node, dict) and isinstance(node.get("id"), str)
        }
        occurrence_presence: dict[str, bool] = {}
        role_presence_counts = {role: 0 for role in role_counts}
        for group in ir["semantic_groups"]:
            anchor = nodes_by_id[group["anchor_node_id"]]
            if anchor["semantic"]["output"] in {"visual-once", "artifact-omitted"}:
                continue
            occurrence_presence[group["id"]] = True
            role_presence_counts[group["role"]] += 1
        compile_report = {
            "status": "passed",
            "automated_status": "passed",
            "profile": bindings["profile"],
            "docx": docx_path.name,
            "docx_sha256": bindings["docx_sha256"],
            "pdf": pdf_path.name,
            "pdf_sha256": sha256_file(pdf_path),
            "page_count": 1,
            **docx_compile_evidence(),
            "role_counts": role_counts,
            "role_presence_counts": role_presence_counts,
            "occurrence_presence": occurrence_presence,
            "contact_sheets": [contact_record],
            "document_ir_sha256": bindings["document_ir_sha256"],
            "build_manifest_sha256": bindings["build_manifest_sha256"],
            "output_audit_sha256": bindings["output_audit_sha256"],
            "docx_audit_sha256": sha256_file(audit_path),
            "docx_audit_bindings": bindings,
            "source_pdf_sha256": source_pdf_hash,
            "failures": [],
        }
        write_json(output_dir / "compile-audit.json", compile_report)
        write_json(
            output_dir / "visual-review.json",
            {
                "status": "passed",
                "compile_audit_sha256": sha256_file(
                    output_dir / "compile-audit.json"
                ),
                "pdf": compile_report["pdf"],
                "pdf_sha256": compile_report["pdf_sha256"],
                "page_count": compile_report["page_count"],
                "reviewed_pages": [1],
                "contact_sheets_inspected": [contact_record["path"]],
                "contact_sheets_sha256": {
                    contact_record["path"]: contact_record["sha256"]
                },
                "notes": "Inspected the complete synthetic output.",
                "failures": [],
            },
        )

        valid = run_finalize(work_dir)
        assert valid.returncode == 0, valid.stdout + valid.stderr
        assert read_json(output_dir / "qa-report.json")["status"] == "passed"
        valid_status = report_status(work_dir)
        assert valid_status["gate_statuses"]["docx_audit"] == "passed"
        assert valid_status["gate_statuses"]["compile_audit"] == "passed"
        results.append(
            "final QA and status bind relocated DOCX audits by current bytes"
        )

        audit_bytes = audit_path.read_bytes()
        compile_bytes = (output_dir / "compile-audit.json").read_bytes()
        forged_audit = read_json(audit_path)
        forged_audit["checks"]["docx_opens"] = False
        forged_audit["status"] = "passed"
        forged_audit["failures"] = []
        write_json(audit_path, forged_audit)
        forged_compile = dict(compile_report)
        forged_compile["docx_audit_sha256"] = sha256_file(audit_path)
        write_json(output_dir / "compile-audit.json", forged_compile)
        forged_status = report_status(work_dir)
        assert forged_status["gate_statuses"]["docx_audit"] == "stale"
        audit_path.write_bytes(audit_bytes)
        (output_dir / "compile-audit.json").write_bytes(compile_bytes)
        results.append("schema V2 DOCX audits cannot relabel a false check as passed")

        audit_path.unlink()
        missing_audit = run_finalize(work_dir)
        assert missing_audit.returncode != 0
        assert "missing docx gate" in (missing_audit.stdout + missing_audit.stderr)
        audit_path.write_bytes(audit_bytes)
        results.append("final QA rejects a missing schema V2 DOCX audit")

        docx_bytes = docx_path.read_bytes()
        docx_path.write_bytes(docx_bytes + b"modified after audit")
        changed_docx = run_finalize(work_dir)
        assert changed_docx.returncode != 0
        assert "DOCX freeze chain" in (changed_docx.stdout + changed_docx.stderr)
        changed_status = report_status(work_dir)
        assert changed_status["gate_statuses"]["docx_audit"] == "stale"
        assert changed_status["gate_statuses"]["compile_audit"] == "stale"
        docx_path.write_bytes(docx_bytes)
        results.append("final QA and pipeline status reject DOCX bytes changed after audit")

        profile_bytes = profile_path.read_bytes()
        write_json(profile_path, {"schema_version": 1, "id": "freeze-test"})
        downgraded_profile = run_finalize(work_dir)
        assert downgraded_profile.returncode != 0
        assert "frozen Profile is not schema V2" in (
            downgraded_profile.stdout + downgraded_profile.stderr
        )
        downgraded_status = report_status(work_dir)
        assert downgraded_status["gate_statuses"]["docx_audit"] == "stale"
        assert downgraded_status["gate_statuses"]["compile_audit"] == "stale"
        profile_path.write_bytes(profile_bytes)
        results.append("schema metadata edits cannot bypass the V2 DOCX freeze gate")

        stale_compile = dict(compile_report)
        stale_compile["docx_audit_bindings"] = {
            **bindings,
            "document_ir_sha256": "0" * 64,
        }
        write_json(output_dir / "compile-audit.json", stale_compile)
        stale_binding = run_finalize(work_dir)
        assert stale_binding.returncode != 0
        assert "DOCX audit bindings are stale" in (
            stale_binding.stdout + stale_binding.stderr
        )
        stale_status = report_status(work_dir)
        assert stale_status["gate_statuses"]["docx_audit"] == "passed"
        assert stale_status["gate_statuses"]["compile_audit"] == "stale"
        results.append(
            "final QA and pipeline status reject stale compile-to-DOCX-audit bindings"
        )

    print(
        json.dumps(
            {"status": "passed", "tests": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
