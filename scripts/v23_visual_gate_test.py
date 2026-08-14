#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from common import read_json, sha256_file, write_json
from pipeline import report_status


SCRIPT_DIR = Path(__file__).resolve().parent


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


def main() -> None:
    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bilingual-v23-visual-gate-") as temp:
        work_dir = Path(temp)
        output_dir = work_dir / "output"
        output_dir.mkdir()
        pdf = output_dir / "fixture.pdf"
        pdf.write_bytes(b"synthetic PDF identity fixture")
        base_audit = {
            "status": "passed",
            "automated_status": "passed",
            "pdf": pdf.name,
            "pdf_sha256": sha256_file(pdf),
            "page_count": 1,
        }

        write_json(output_dir / "compile-audit.json", base_audit)
        missing = run_review(work_dir)
        assert missing.returncode != 0
        assert "no contact sheets" in (missing.stdout + missing.stderr)
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
        output_dir = work_dir / "output"
        translation_dir = work_dir / "translation"
        output_dir.mkdir()
        translation_dir.mkdir()
        profile_path = work_dir / "profile.json"
        ir_path = work_dir / "document-ir.json"
        build_path = output_dir / "build-manifest.json"
        docx_path = output_dir / "fixture.docx"
        pdf_path = output_dir / "fixture.pdf"
        audit_path = output_dir / "docx-audit.json"
        contact_dir = output_dir / "contact"
        contact_dir.mkdir()
        contact_path = contact_dir / "contact-001.png"

        write_json(profile_path, {"schema_version": 2, "id": "freeze-test"})
        write_json(ir_path, {"schema_version": 2, "nodes": []})
        write_json(build_path, {"schema_version": 1, "profile_id": "freeze-test"})
        docx_path.write_bytes(b"audited DOCX fixture")
        pdf_path.write_bytes(b"compiled PDF fixture")
        Image.new("RGB", (80, 80), "white").save(contact_path)
        bindings = {
            "profile": "freeze-test",
            "profile_file_sha256": sha256_file(profile_path),
            "document_ir_sha256": sha256_file(ir_path),
            "build_manifest_sha256": sha256_file(build_path),
            "docx_sha256": sha256_file(docx_path),
        }
        write_json(
            audit_path,
            {"status": "passed", "docx": str(docx_path.resolve()), **bindings},
        )
        contact_record = {
            "path": "contact/contact-001.png",
            "sha256": sha256_file(contact_path),
            "first_page": 1,
            "last_page": 1,
        }
        compile_report = {
            "status": "passed",
            "automated_status": "passed",
            "profile": bindings["profile"],
            "docx": docx_path.name,
            "docx_sha256": bindings["docx_sha256"],
            "pdf": pdf_path.name,
            "pdf_sha256": sha256_file(pdf_path),
            "page_count": 1,
            "contact_sheets": [contact_record],
            "document_ir_sha256": bindings["document_ir_sha256"],
            "build_manifest_sha256": bindings["build_manifest_sha256"],
            "docx_audit_sha256": sha256_file(audit_path),
            "docx_audit_bindings": bindings,
            "source_pdf_sha256": "1" * 64,
        }
        write_json(output_dir / "compile-audit.json", compile_report)
        write_json(work_dir / "source-audit.json", {"status": "passed"})
        write_json(translation_dir / "translation-audit.json", {"status": "passed"})
        write_json(output_dir / "output-audit.json", {"status": "passed"})
        write_json(
            output_dir / "visual-review.json",
            {
                "status": "passed",
                "pdf_sha256": compile_report["pdf_sha256"],
                "reviewed_pages": [1],
                "contact_sheets_inspected": [contact_record["path"]],
                "contact_sheets_sha256": {
                    contact_record["path"]: contact_record["sha256"]
                },
                "notes": "Inspected the complete synthetic output.",
            },
        )

        valid = run_finalize(work_dir)
        assert valid.returncode == 0, valid.stdout + valid.stderr
        assert read_json(output_dir / "qa-report.json")["status"] == "passed"
        valid_status = report_status(work_dir)
        assert valid_status["gate_statuses"]["docx_audit"] == "passed"
        assert valid_status["gate_statuses"]["compile_audit"] == "passed"
        results.append("final QA binds the passed DOCX audit to compile and DOCX bytes")

        audit_bytes = audit_path.read_bytes()
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
