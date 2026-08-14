#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from common import read_json, sha256_file, write_json


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

    print(
        json.dumps(
            {"status": "passed", "tests": len(results), "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
