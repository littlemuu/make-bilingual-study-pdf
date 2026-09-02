from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audit_docx import validate_compile_docx_binding, validate_docx_audit_binding
from audit_outputs import validate_compile_output_binding, validate_output_audit_binding
from audit_source import validate_source_audit_binding
from audit_translation import (
    validate_translation_audit_binding,
    validate_translation_plan_binding,
)
from common import json_loads_strict
from profile import load_work_profile
from safe_artifacts import (
    ArtifactSafetyError,
    lexical_absolute_path,
    read_artifact_text,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)
from visual_utils import validate_visual_review_binding


@dataclass(frozen=True)
class JobState:
    status_report: dict[str, Any]
    final_report: dict[str, Any]


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


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def translation_plan_status(work_dir: Path, plan_path: Path) -> str:
    try:
        plan, errors = validate_translation_plan_binding(work_dir, plan_path)
        return "invalid" if plan is None else "stale" if errors else "passed"
    except (KeyError, TypeError, ValueError, OSError):
        return "invalid"


def evaluate_job(work_dir: Path) -> JobState:
    """Read and validate the current job without mutating any artifact."""
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    output_dir = work_dir / "output"
    artifacts = {
        "profile": work_dir / "profile.json",
        "manifest": work_dir / "manifest.json",
        "adapter_evidence": work_dir / "adapter-evidence.json",
        "document_ir": work_dir / "document-ir.json",
        "source_audit": work_dir / "source-audit.json",
        "glossary": work_dir / "translation" / "glossary.json",
        "translation_plan": work_dir / "translation" / "plan.json",
        "translation_audit": work_dir / "translation" / "translation-audit.json",
        "merged_translations": work_dir / "translation" / "translations-merged.jsonl",
        "build_manifest": output_dir / "build-manifest.json",
        "output_audit": output_dir / "output-audit.json",
        "docx_audit": output_dir / "docx-audit.json",
        "compile_audit": output_dir / "compile-audit.json",
        "visual_review": output_dir / "visual-review.json",
        "qa_report": output_dir / "qa-report.json",
    }
    exists = {
        name: _artifact_exists(path, work_dir) for name, path in artifacts.items()
    }
    reports: dict[str, dict[str, Any]] = {}
    read_errors: dict[str, Exception] = {}
    status_cache: dict[str, str | None] = {}
    binding_errors: dict[str, list[str]] = {}

    def read_report(name: str) -> dict[str, Any] | None:
        if name in reports:
            return reports[name]
        if name in read_errors or not exists[name]:
            return None
        try:
            report = _read_json(artifacts[name], work_dir)
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            read_errors[name] = exc
            return None
        reports[name] = report
        return report

    profile = read_report("profile") or {}
    ir = read_report("document_ir") or {}
    compile_hint = read_report("compile_audit") or {}
    schema_v2 = (
        profile.get("schema_version") == 2
        or ir.get("schema_version") == 2
        or "docx_audit_bindings" in compile_hint
    )
    requires_docx = schema_v2 or any(
        key in compile_hint
        for key in ("docx", "docx_audit_sha256", "docx_audit_bindings")
    )

    def gate_status(name: str) -> str | None:
        if name in status_cache:
            return status_cache[name]
        path = artifacts[name]
        if not exists[name] or path.suffix != ".json":
            status_cache[name] = None
            return None
        if name == "translation_plan":
            value = translation_plan_status(work_dir, path)
            status_cache[name] = value
            return value
        report = read_report(name)
        if report is None:
            status_cache[name] = "invalid"
            return "invalid"
        try:
            status_field = "automated_status" if name == "compile_audit" else "status"
            value = report.get(status_field)
            if not isinstance(value, str):
                status_cache[name] = "invalid"
                return "invalid"
            errors: list[str] = []
            if name == "source_audit" and value == "passed":
                _, errors = validate_source_audit_binding(work_dir, path)
            elif name == "translation_audit" and value == "passed":
                _, errors = validate_translation_audit_binding(work_dir, path)
            elif name == "output_audit" and value == "passed":
                _, _, errors = validate_output_audit_binding(work_dir, path)
            elif name == "docx_audit" and value == "passed":
                docx_reference = report.get("docx")
                if not isinstance(docx_reference, str) or not docx_reference:
                    status_cache[name] = "invalid"
                    return "invalid"
                docx_name = docx_reference.replace("\\", "/").rsplit("/", 1)[-1]
                if not docx_name or docx_name in {".", ".."}:
                    status_cache[name] = "invalid"
                    return "invalid"
                docx_path = work_relative_artifact_path(
                    output_dir, docx_name, label="status DOCX path"
                )
                _, _, errors = validate_docx_audit_binding(work_dir, docx_path, path)
            elif name == "compile_audit" and value == "passed":
                _, output_errors = validate_compile_output_binding(
                    work_dir, report, artifacts["output_audit"]
                )
                binding_errors["compile_output"] = output_errors
                errors.extend(output_errors)
                if requires_docx:
                    work_relative_artifact_path(
                        output_dir,
                        report.get("docx"),
                        label="compile status DOCX path",
                    )
                    _, docx_errors = validate_compile_docx_binding(
                        work_dir, report, artifacts["docx_audit"]
                    )
                    binding_errors["compile_docx"] = docx_errors
                    errors.extend(docx_errors)
            elif name == "visual_review" and value == "passed":
                if gate_status("compile_audit") != "passed":
                    status_cache[name] = "stale"
                    return "stale"
                compile_report = read_report("compile_audit") or {}
                _, errors = validate_visual_review_binding(
                    work_dir, report, compile_report
                )
            elif name == "qa_report" and value == "passed":
                upstream = [
                    "source_audit",
                    "translation_audit",
                    "output_audit",
                    "compile_audit",
                    "visual_review",
                ]
                if requires_docx:
                    upstream.append("docx_audit")
                if any(gate_status(gate) != "passed" for gate in upstream):
                    status_cache[name] = "stale"
                    return "stale"
                if report.get("failures") != []:
                    status_cache[name] = "invalid"
                    return "invalid"
                expected_gate_names = {
                    gate.removesuffix("_audit")
                    if gate != "visual_review"
                    else "visual"
                    for gate in upstream
                }
                recorded_statuses = report.get("gate_statuses")
                if (
                    not isinstance(recorded_statuses, dict)
                    or set(recorded_statuses) != expected_gate_names
                    or any(item != "passed" for item in recorded_statuses.values())
                ):
                    status_cache[name] = "invalid"
                    return "invalid"
                gate_hashes = report.get("gate_sha256")
                if (
                    not isinstance(gate_hashes, dict)
                    or set(gate_hashes) != expected_gate_names
                ):
                    status_cache[name] = "invalid"
                    return "invalid"
                gate_paths = {
                    "source": artifacts["source_audit"],
                    "translation": artifacts["translation_audit"],
                    "output": artifacts["output_audit"],
                    "compile": artifacts["compile_audit"],
                    "visual": artifacts["visual_review"],
                }
                if requires_docx:
                    gate_paths["docx"] = artifacts["docx_audit"]
                for gate, gate_path in gate_paths.items():
                    if (
                        not _artifact_exists(gate_path, work_dir)
                        or gate_hashes.get(gate)
                        != sha256_artifact(gate_path, boundary=work_dir)
                    ):
                        status_cache[name] = "stale"
                        return "stale"
                deliverables = report.get("deliverables")
                if not isinstance(deliverables, dict):
                    status_cache[name] = "invalid"
                    return "invalid"
                build = read_report("build_manifest") or {}
                expected_deliverables: dict[str, dict[str, str]] = {}
                for label, relative in (
                    ("markdown", build.get("markdown")),
                    ("latex", build.get("latex")),
                    ("docx", compile_hint.get("docx")),
                    ("pdf", compile_hint.get("pdf")),
                ):
                    if relative is None:
                        continue
                    deliverable_path = work_relative_artifact_path(
                        output_dir,
                        relative,
                        label=f"current QA {label} path",
                    )
                    if not _artifact_exists(deliverable_path, work_dir):
                        status_cache[name] = "stale"
                        return "stale"
                    expected_deliverables[label] = {
                        "path": f"output/{relative}",
                        "sha256": sha256_artifact(
                            deliverable_path, boundary=work_dir
                        ),
                    }
                if deliverables != expected_deliverables:
                    status_cache[name] = "stale"
                    return "stale"
                manifest = read_report("manifest") or {}
                source_hash = manifest.get("source_sha256")
                if not _valid_sha256(source_hash):
                    status_cache[name] = "invalid"
                    return "invalid"
                if report.get("source_pdf_sha256") != source_hash:
                    status_cache[name] = "stale"
                    return "stale"
                compiled_source_hash = compile_hint.get("source_pdf_sha256")
                if compiled_source_hash is not None and compiled_source_hash != source_hash:
                    status_cache[name] = "stale"
                    return "stale"
            binding_errors[name] = errors
            if errors:
                value = "stale"
            status_cache[name] = value
            return value
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            read_errors[name] = exc
            status_cache[name] = "invalid"
            return "invalid"

    gate_names = (
        "source_audit",
        "translation_plan",
        "translation_audit",
        "output_audit",
        "docx_audit",
        "compile_audit",
        "visual_review",
        "qa_report",
    )
    gate_statuses = {name: gate_status(name) for name in gate_names}

    profile_id: str | None = None
    if exists["profile"]:
        try:
            profile_id = load_work_profile(work_dir)["id"]
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError):
            profile_id = "invalid"
    manifest = read_report("manifest") or {}
    adapter_value = manifest.get("adapter")
    adapter_invalid = adapter_value is not None and not isinstance(adapter_value, dict)
    adapter = adapter_value if isinstance(adapter_value, dict) else {}
    adapter_id = ("invalid" if adapter_invalid else adapter.get("id")) or (
        load_work_profile(work_dir)["input"]["adapter"]
        if exists["profile"] and profile_id != "invalid"
        else None
    )

    if not exists["manifest"]:
        next_action = (
            "run `pipeline.py source SOURCE.pdf --work-dir WORK_DIR` for the native adapter, "
            "or `pipeline.py import-mineru SOURCE.pdf MINERU_OUTPUT_DIR --work-dir WORK_DIR "
            "--profile PROFILE` for a frozen MinerU output"
        )
    elif not exists["profile"] or not exists["document_ir"]:
        next_action = "run `pipeline.py ir WORK_DIR` to bind the default profile and create the IR"
    elif not exists["source_audit"]:
        next_action = "run `pipeline.py source-audit WORK_DIR`"
    elif gate_statuses["source_audit"] in {
        "needs_manual_review",
        "manual_source_review_required",
    }:
        next_action = (
            "complete the independent per-page source review; translation preparation and "
            "final QA remain blocked while source evidence needs manual review"
        )
    elif gate_statuses["source_audit"] != "passed":
        next_action = "repair extraction, then rerun `pipeline.py source-audit WORK_DIR`"
    elif not exists["glossary"]:
        next_action = "run `init_glossary.py WORK_DIR`, then review the glossary"
    elif not exists["translation_plan"]:
        next_action = "after reviewing the glossary, run `pipeline.py prepare WORK_DIR`"
    elif gate_statuses["translation_plan"] != "passed":
        next_action = "repair changed plan inputs/requests, then rerun `pipeline.py prepare WORK_DIR`"
    elif gate_statuses["translation_audit"] != "passed":
        next_action = "fill/resume response JSONL files, then run `audit_translation.py WORK_DIR --progress`"
    elif gate_statuses["output_audit"] != "passed":
        next_action = "run `pipeline.py build WORK_DIR`"
    elif gate_statuses["docx_audit"] != "passed":
        next_action = (
            "run `pipeline.py docx WORK_DIR --markdown AUDITED_STRUCTURED.md`; "
            "the Markdown must already contain complete semantic callout containers"
        )
    elif gate_statuses["compile_audit"] != "passed":
        next_action = "run `pipeline.py compile-docx WORK_DIR`"
    elif gate_statuses["visual_review"] != "passed":
        next_action = "inspect every final render and record the visual review"
    elif gate_statuses["qa_report"] != "passed":
        next_action = "run `pipeline.py finalize WORK_DIR`"
    else:
        next_action = "complete"

    status_report = {
        "work_dir": str(work_dir),
        "profile": profile_id,
        "adapter": {
            "id": adapter_id,
            "backend": adapter.get("backend"),
            "version": adapter.get("version"),
            "evidence": adapter.get("evidence"),
            "evidence_sha256": adapter.get("evidence_sha256"),
        },
        "artifacts": exists,
        "gate_statuses": gate_statuses,
        "next_action": next_action,
    }

    final_names = {
        "source": "source_audit",
        "translation": "translation_audit",
        "output": "output_audit",
        "compile": "compile_audit",
        "visual": "visual_review",
    }
    if requires_docx:
        final_names["docx"] = "docx_audit"
    failures: list[str] = []
    final_reports: dict[str, dict[str, Any]] = {}
    for label, name in final_names.items():
        if not exists[name]:
            failures.append(
                f"missing {label} gate: {artifacts[name].relative_to(work_dir)}"
            )
        elif name in read_errors:
            failures.append(f"invalid {label} gate: {read_errors[name]}")
        elif name in reports:
            final_reports[label] = reports[name]

    compile_report = final_reports.get("compile", {})
    if compile_report.get("automated_status") == "passed":
        try:
            for label in ("docx", "pdf"):
                relative = compile_report.get(label)
                if relative is None and label == "docx":
                    continue
                path = work_relative_artifact_path(
                    output_dir, relative, label=f"compile {label} path"
                )
                if not _artifact_exists(path, work_dir):
                    failures.append(f"missing compiled {label}: {relative}")
                elif (
                    not isinstance(compile_report.get(f"{label}_sha256"), str)
                    or sha256_artifact(path, boundary=work_dir)
                    != compile_report.get(f"{label}_sha256")
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
                    if not _artifact_exists(
                        contact_path, work_dir
                    ) or sha256_artifact(
                        contact_path, boundary=work_dir
                    ) != item.get("sha256"):
                        failures.append(
                            "compiled contact sheet missing or changed: "
                            f"{item.get('path')}"
                        )
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"invalid compile deliverable metadata: {exc}")

    for label in ("source", "translation", "output", "visual"):
        report = final_reports.get(label)
        if report is not None and report.get("status") != "passed":
            failures.append(f"{label} gate is {report.get('status')}")
    if requires_docx and "docx" in final_reports:
        report = final_reports["docx"]
        if report.get("status") != "passed":
            failures.append(f"docx gate is {report.get('status')}")
    compile_status = compile_report.get("automated_status") if compile_report else None
    if compile_report:
        if not isinstance(compile_status, str):
            failures.append("compile automated gate status must be a string")
        if compile_status != "passed":
            failures.append("compile automated gate is not passed")

    if compile_status == "passed":
        failures.extend(
            f"compile output freeze chain: {message}"
            for message in binding_errors.get("compile_output", [])
        )
    if "compile" in final_reports and "visual" in final_reports:
        try:
            _, visual_errors = validate_visual_review_binding(
                work_dir, final_reports["visual"], final_reports["compile"]
            )
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            visual_errors = [str(exc)]
        failures.extend(
            f"visual freeze chain: {message}" for message in visual_errors
        )
    if requires_docx and "compile" in final_reports and "docx" in final_reports:
        failures.extend(
            f"DOCX freeze chain: {message}"
            for message in binding_errors.get("compile_docx", [])
        )

    if final_reports.get("source", {}).get("status") == "passed":
        failures.extend(
            f"source freeze chain: {message}"
            for message in binding_errors.get("source_audit", [])
        )
        try:
            current_source_hash = (read_report("manifest") or {}).get("source_sha256")
            if not _valid_sha256(current_source_hash):
                failures.append("source manifest SHA-256 is invalid")
            compiled_source_hash = compile_report.get("source_pdf_sha256")
            if (
                compiled_source_hash is not None
                and compiled_source_hash != current_source_hash
            ):
                failures.append("compile gate refers to a different source PDF hash")
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"cannot validate current source PDF hash: {exc}")
    if final_reports.get("output", {}).get("status") == "passed":
        failures.extend(
            f"output freeze chain: {message}"
            for message in binding_errors.get("output_audit", [])
        )
    if final_reports.get("translation", {}).get("status") == "passed":
        failures.extend(
            f"translation freeze chain: {message}"
            for message in binding_errors.get("translation_audit", [])
        )

    deliverables: dict[str, dict[str, str]] = {}
    if not failures:
        if not exists["build_manifest"]:
            raise ArtifactSafetyError("missing output build manifest")
        build = read_report("build_manifest") or {}
        for label, relative in (
            ("markdown", build.get("markdown")),
            ("latex", build.get("latex")),
            ("docx", compile_report.get("docx")),
            ("pdf", compile_report.get("pdf")),
        ):
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

    source_pdf_sha256 = compile_report.get("source_pdf_sha256")
    if source_pdf_sha256 is None:
        try:
            source_pdf_sha256 = (read_report("manifest") or {}).get("source_sha256")
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError) as exc:
            failures.append(f"invalid source manifest: {exc}")
    final_report = {
        "status": "failed" if failures else "passed",
        "source_pdf_sha256": source_pdf_sha256,
        "gate_statuses": {
            name: (
                report.get("automated_status")
                if name == "compile"
                else report.get("status")
            )
            for name, report in final_reports.items()
        },
        "gate_sha256": {
            name: sha256_artifact(artifacts[artifact_name], boundary=work_dir)
            for name, artifact_name in final_names.items()
            if name in final_reports
        },
        "deliverables": deliverables,
        "warnings": compile_report.get("warnings", []),
        "failures": failures,
    }
    return JobState(status_report=status_report, final_report=final_report)


def report_status(work_dir: Path) -> dict[str, Any]:
    return evaluate_job(work_dir).status_report
