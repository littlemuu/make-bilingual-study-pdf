#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from audit_docx import validate_compile_docx_binding
from audit_outputs import (
    validate_compile_output_binding,
    validate_output_audit_binding,
)
from audit_source import validate_source_audit_binding
from audit_translation import validate_translation_audit_binding
from common import json_loads_strict
from safe_artifacts import (
    ArtifactSafetyError,
    atomic_write_text,
    lexical_absolute_path,
    read_artifact_text,
    remove_artifact_file,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)
from visual_utils import validate_visual_review_binding


def _read_json(path: Path, work_dir: Path) -> dict[str, Any]:
    value = json_loads_strict(read_artifact_text(path, boundary=work_dir))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    if not os.path.lexists(path):
        return False
    validate_artifact_file(path, boundary=work_dir)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the final QA report only after all automated and visual gates pass."
    )
    parser.add_argument("work_dir", type=Path)
    args = parser.parse_args()

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    validate_artifact_tree(output_dir, work_dir, allow_missing=False)
    for directory in (output_dir / "build", output_dir / "pdf-renders", output_dir / "contact"):
        validate_artifact_tree(directory, work_dir, allow_missing=True)
    qa_path = output_dir / "qa-report.json"
    validate_artifact_file(qa_path, boundary=work_dir, allow_missing=True)
    # The complete final-stage tree is safe. Invalidate any previous pass before
    # reading mutable gate metadata or current deliverable bytes.
    remove_artifact_file(qa_path, boundary=work_dir)
    failures: list[str] = []
    profile_path = work_dir / "profile.json"
    try:
        profile = (
            _read_json(profile_path, work_dir)
            if _artifact_exists(profile_path, work_dir)
            else {}
        )
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        profile = {}
        failures.append(f"invalid frozen Profile: {exc}")
    ir_path = work_dir / "document-ir.json"
    try:
        ir = (
            _read_json(ir_path, work_dir)
            if _artifact_exists(ir_path, work_dir)
            else {}
        )
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        ir = {}
        failures.append(f"invalid document IR: {exc}")
    compile_path = output_dir / "compile-audit.json"
    try:
        compile_hint = (
            _read_json(compile_path, work_dir)
            if _artifact_exists(compile_path, work_dir)
            else {}
        )
    except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
        compile_hint = {}
        failures.append(f"invalid compile gate: {exc}")
    schema_v2 = (
        profile.get("schema_version") == 2
        or ir.get("schema_version") == 2
        or "docx_audit_bindings" in compile_hint
    )
    requires_docx = schema_v2 or (
        isinstance(compile_hint, dict)
        and (
            "docx" in compile_hint
            or "docx_audit_bindings" in compile_hint
        )
    )
    gates = {
        "source": work_dir / "source-audit.json",
        "translation": work_dir / "translation" / "translation-audit.json",
        "output": output_dir / "output-audit.json",
        "compile": compile_path,
        "visual": output_dir / "visual-review.json",
    }
    if requires_docx:
        gates["docx"] = output_dir / "docx-audit.json"
    reports = {}
    for name, path in gates.items():
        if not _artifact_exists(path, work_dir):
            failures.append(f"missing {name} gate: {path.relative_to(work_dir)}")
            continue
        try:
            reports[name] = _read_json(path, work_dir)
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"invalid {name} gate: {exc}")

    if "compile" in reports:
        compile_report = reports["compile"]
        for label in ("docx", "pdf"):
            relative = compile_report.get(label)
            if relative is None and label == "docx":
                continue
            path = work_relative_artifact_path(
                output_dir, relative, label=f"compile {label} path"
            )
            if not _artifact_exists(path, work_dir):
                failures.append(f"missing compiled {label}: {relative}")
                continue
            expected_hash = compile_report.get(f"{label}_sha256")
            if (
                not isinstance(expected_hash, str)
                or sha256_artifact(path, boundary=work_dir) != expected_hash
            ):
                failures.append(
                    f"compiled {label} changed after automated compile QA"
                )
        contacts = compile_report.get("contact_sheets", [])
        if isinstance(contacts, list):
            for item in contacts:
                if not isinstance(item, dict):
                    raise ArtifactSafetyError(
                        "compile contact-sheet entries must be objects"
                    )
                contact_path = work_relative_artifact_path(
                    output_dir,
                    item.get("path"),
                    label="compile contact-sheet path",
                )
                try:
                    contact_path.relative_to(output_dir / "contact")
                except ValueError as exc:
                    raise ArtifactSafetyError(
                        "compile contact-sheet paths must stay inside output/contact"
                    ) from exc
                if not _artifact_exists(contact_path, work_dir) or sha256_artifact(
                    contact_path, boundary=work_dir
                ) != item.get("sha256"):
                    failures.append(
                        f"compiled contact sheet missing or changed: {item.get('path')}"
                    )

    status_gates = ["source", "translation", "output", "visual"]
    if requires_docx:
        status_gates.append("docx")
    for name in status_gates:
        if name in reports and reports[name].get("status") != "passed":
            failures.append(f"{name} gate is {reports[name].get('status')}")
    compile_status = None
    if "compile" in reports:
        compile_status = reports["compile"].get("automated_status")
        if not isinstance(compile_status, str):
            failures.append("compile automated gate status must be a string")
        if compile_status != "passed":
            failures.append("compile automated gate is not passed")
    if "compile" in reports and compile_status == "passed":
        try:
            _, binding_errors = validate_compile_output_binding(
                work_dir, reports["compile"], gates["output"]
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            binding_errors = [f"cannot validate compile output binding: {exc}"]
        failures.extend(
            f"compile output freeze chain: {message}"
            for message in binding_errors
        )
    if "compile" in reports and "visual" in reports:
        _, visual_errors = validate_visual_review_binding(
            work_dir, reports["visual"], reports["compile"]
        )
        failures.extend(
            f"visual freeze chain: {message}" for message in visual_errors
        )

    if requires_docx and "compile" in reports and "docx" in reports:
        _, binding_errors = validate_compile_docx_binding(
            work_dir, reports["compile"], gates["docx"]
        )
        failures.extend(f"DOCX freeze chain: {message}" for message in binding_errors)

    if reports.get("source", {}).get("status") == "passed":
        _, binding_errors = validate_source_audit_binding(
            work_dir, gates["source"]
        )
        failures.extend(
            f"source freeze chain: {message}" for message in binding_errors
        )
        try:
            current_source_hash = _read_json(
                work_dir / "manifest.json", work_dir
            ).get("source_sha256")
            if (
                not isinstance(current_source_hash, str)
                or len(current_source_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in current_source_hash
                )
            ):
                failures.append("source manifest SHA-256 is invalid")
            compile_source_hash = reports.get("compile", {}).get(
                "source_pdf_sha256"
            )
            if (
                compile_source_hash is not None
                and compile_source_hash != current_source_hash
            ):
                failures.append(
                    "compile gate refers to a different source PDF hash"
                )
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"cannot validate current source PDF hash: {exc}")

    if reports.get("output", {}).get("status") == "passed":
        _, _, binding_errors = validate_output_audit_binding(
            work_dir,
            gates["output"],
        )
        failures.extend(
            f"output freeze chain: {message}" for message in binding_errors
        )

    if reports.get("translation", {}).get("status") == "passed":
        _, binding_errors = validate_translation_audit_binding(
            work_dir, gates["translation"]
        )
        failures.extend(
            f"translation freeze chain: {message}" for message in binding_errors
        )

    deliverables = {}
    if not failures:
        build_path = output_dir / "build-manifest.json"
        if not _artifact_exists(build_path, work_dir):
            raise ArtifactSafetyError("missing output build manifest")
        build = _read_json(build_path, work_dir)
        candidates = [
            ("markdown", build.get("markdown")),
            ("latex", build.get("latex")),
            ("docx", reports["compile"].get("docx")),
            ("pdf", reports["compile"].get("pdf")),
        ]
        for label, relative in candidates:
            if not relative:
                continue
            path = work_relative_artifact_path(
                output_dir, relative, label=f"final {label} path"
            )
            if not _artifact_exists(path, work_dir):
                failures.append(f"missing final {label}: {relative}")
            else:
                deliverables[label] = {
                    "path": f"output/{relative}",
                    "sha256": sha256_artifact(path, boundary=work_dir),
                }

    source_pdf_sha256 = reports.get("compile", {}).get("source_pdf_sha256")
    if source_pdf_sha256 is None:
        try:
            source_pdf_sha256 = _read_json(
                work_dir / "manifest.json", work_dir
            ).get("source_sha256")
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"invalid source manifest: {exc}")
    report = {
        "status": "failed" if failures else "passed",
        "source_pdf_sha256": source_pdf_sha256,
        "gate_statuses": {
            name: (
                report.get("automated_status")
                if name == "compile"
                else report.get("status")
            )
            for name, report in reports.items()
        },
        "gate_sha256": {
            name: sha256_artifact(gates[name], boundary=work_dir)
            for name in reports
        },
        "deliverables": deliverables,
        "warnings": reports.get("compile", {}).get("warnings", []),
        "failures": failures,
    }
    atomic_write_text(
        qa_path,
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        boundary=work_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
