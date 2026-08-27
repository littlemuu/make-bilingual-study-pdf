#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from adapters import get_adapter
from audit_docx import (
    validate_compile_docx_binding,
    validate_docx_audit_binding,
    validate_v2_compile_docx_binding,
    validate_v2_docx_audit_binding,
)
from audit_outputs import (
    validate_compile_output_binding,
    validate_output_audit_binding,
)
from audit_source import validate_source_audit_binding
from audit_translation import (
    validate_translation_audit_binding,
    validate_translation_plan_binding,
)
from common import json_loads_strict
from profile import (
    load_profile,
    load_work_profile,
    profile_contract,
)
from safe_artifacts import (
    ArtifactSafetyError,
    lexical_absolute_path,
    portable_artifact_basename,
    read_artifact_text,
    sha256_artifact,
    validate_artifact_directory,
    validate_artifact_file,
    validate_artifact_tree,
    work_relative_artifact_path,
)
from visual_utils import validate_visual_review_binding


SCRIPT_DIR = Path(__file__).resolve().parent


def _read_json(path: Path, work_dir: Path) -> dict:
    value = json_loads_strict(read_artifact_text(path, boundary=work_dir))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _read_jsonl(path: Path, work_dir: Path) -> list[dict]:
    values = []
    for line_number, raw in enumerate(
        read_artifact_text(path, boundary=work_dir).splitlines(), 1
    ):
        if not raw.strip():
            continue
        value = json_loads_strict(raw)
        if not isinstance(value, dict):
            raise ValueError(f"invalid JSONL object at {path}:{line_number}")
        values.append(value)
    return values


def _artifact_exists(path: Path, work_dir: Path) -> bool:
    if not os.path.lexists(path):
        return False
    validate_artifact_file(path, boundary=work_dir)
    return True


def _work_cli_path(value: Path, work_dir: Path, *, label: str) -> Path:
    candidate = lexical_absolute_path(value)
    try:
        candidate.relative_to(work_dir)
    except ValueError as exc:
        raise ArtifactSafetyError(f"{label} must stay inside WORK") from exc
    return candidate


def _portable_basename(value: str, work_dir: Path, *, label: str) -> str:
    """Require one portable filename component for generated deliverables."""
    del work_dir
    return portable_artifact_basename(value, label=label)


def translation_plan_status(work_dir: Path, plan_path: Path) -> str:
    try:
        plan, binding_errors = validate_translation_plan_binding(
            work_dir, plan_path
        )
        if plan is None:
            return "invalid"
        return "stale" if binding_errors else "passed"
    except (KeyError, TypeError, ValueError, OSError):
        return "invalid"


def run_script(script: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *arguments],
        check=True,
    )


def report_status(work_dir: Path) -> dict:
    work_dir = lexical_absolute_path(work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
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
        "build_manifest": work_dir / "output" / "build-manifest.json",
        "output_audit": work_dir / "output" / "output-audit.json",
        "docx_audit": work_dir / "output" / "docx-audit.json",
        "compile_audit": work_dir / "output" / "compile-audit.json",
        "visual_review": work_dir / "output" / "visual-review.json",
        "qa_report": work_dir / "output" / "qa-report.json",
    }
    exists = {
        name: _artifact_exists(path, work_dir) for name, path in artifacts.items()
    }

    def is_schema_v2() -> bool:
        try:
            profile = (
                _read_json(artifacts["profile"], work_dir)
                if exists["profile"]
                else {}
            )
            ir = (
                _read_json(artifacts["document_ir"], work_dir)
                if exists["document_ir"]
                else {}
            )
            compile_report = (
                _read_json(artifacts["compile_audit"], work_dir)
                if exists["compile_audit"]
                else {}
            )
            return (
                profile.get("schema_version") == 2
                or ir.get("schema_version") == 2
                or "docx_audit_bindings" in compile_report
            )
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError):
            return False

    def status(name: str) -> str | None:
        path = artifacts[name]
        if not exists[name] or path.suffix != ".json":
            return None
        if name == "translation_plan":
            return translation_plan_status(work_dir, path)
        try:
            report = _read_json(path, work_dir)
            # Every gate owns one exact status field. In particular, a final QA
            # report may not borrow compile-audit's automated_status field when
            # its required status field is missing.
            status_field = "automated_status" if name == "compile_audit" else "status"
            value = report.get(status_field)
            if not isinstance(value, str):
                return "invalid"
            if name == "source_audit" and value == "passed":
                _, binding_errors = validate_source_audit_binding(
                    work_dir, path
                )
                if binding_errors:
                    return "stale"
            if name == "output_audit" and value == "passed":
                _, _, binding_errors = validate_output_audit_binding(
                    work_dir, path
                )
                if binding_errors:
                    return "stale"
            if name == "translation_audit" and value == "passed":
                _, binding_errors = validate_translation_audit_binding(
                    work_dir, path
                )
                if binding_errors:
                    return "stale"
            if name == "docx_audit" and value == "passed":
                docx_reference = report.get("docx")
                if not isinstance(docx_reference, str) or not docx_reference:
                    return "invalid"
                # DOCX audit reports may be copied out of CI for human review.
                # Their absolute diagnostic path belongs to the producing host;
                # the frozen identity is the byte hash, so resolve the portable
                # basename inside this work directory's output folder.
                docx_name = docx_reference.replace("\\", "/").rsplit("/", 1)[-1]
                if not docx_name or docx_name in {".", ".."}:
                    return "invalid"
                docx_path = work_relative_artifact_path(
                    work_dir / "output", docx_name, label="status DOCX path"
                )
                _, _, binding_errors = validate_docx_audit_binding(
                    work_dir, docx_path, path
                )
                if binding_errors:
                    return "stale"
            if name == "compile_audit" and value == "passed":
                _, binding_errors = validate_compile_output_binding(
                    work_dir, report, artifacts["output_audit"]
                )
                if binding_errors:
                    return "stale"
                if (
                    is_schema_v2()
                    or "docx" in report
                    or "docx_audit_bindings" in report
                ):
                    docx_reference = report.get("docx")
                    work_relative_artifact_path(
                        work_dir / "output",
                        docx_reference,
                        label="compile status DOCX path",
                    )
                    _, binding_errors = validate_compile_docx_binding(
                        work_dir, report, artifacts["docx_audit"]
                    )
                    if binding_errors:
                        return "stale"
            if name == "visual_review" and value == "passed":
                if status("compile_audit") != "passed":
                    return "stale"
                compile_report = _read_json(
                    artifacts["compile_audit"], work_dir
                )
                _, binding_errors = validate_visual_review_binding(
                    work_dir, report, compile_report
                )
                if binding_errors:
                    return "stale"
            if name == "qa_report" and value == "passed":
                current_compile = _read_json(
                    artifacts["compile_audit"], work_dir
                )
                requires_docx = is_schema_v2() or any(
                    key in current_compile
                    for key in ("docx", "docx_audit_sha256", "docx_audit_bindings")
                )
                upstream = [
                    "source_audit",
                    "translation_audit",
                    "output_audit",
                    "compile_audit",
                    "visual_review",
                ]
                if requires_docx:
                    upstream.append("docx_audit")
                if any(status(gate) != "passed" for gate in upstream):
                    return "stale"
                failures = report.get("failures")
                if failures != []:
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
                    or any(
                        not isinstance(item, str) or item != "passed"
                        for item in recorded_statuses.values()
                    )
                ):
                    return "invalid"
                gate_hashes = report.get("gate_sha256")
                if not isinstance(gate_hashes, dict) or set(gate_hashes) != expected_gate_names:
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
                        return "stale"
                deliverables = report.get("deliverables")
                if not isinstance(deliverables, dict):
                    return "invalid"
                build = _read_json(artifacts["build_manifest"], work_dir)
                compile_report = current_compile
                expected_deliverables: dict[str, dict[str, str]] = {}
                candidates = (
                    ("markdown", build.get("markdown")),
                    ("latex", build.get("latex")),
                    ("docx", compile_report.get("docx")),
                    ("pdf", compile_report.get("pdf")),
                )
                for label, relative in candidates:
                    if relative is None:
                        continue
                    deliverable_path = work_relative_artifact_path(
                        work_dir / "output",
                        relative,
                        label=f"current QA {label} path",
                    )
                    if not _artifact_exists(deliverable_path, work_dir):
                        return "stale"
                    expected_deliverables[label] = {
                        "path": f"output/{relative}",
                        "sha256": sha256_artifact(
                            deliverable_path, boundary=work_dir
                        ),
                    }
                if deliverables != expected_deliverables:
                    return "stale"
                source_manifest = _read_json(artifacts["manifest"], work_dir)
                current_source_hash = source_manifest.get("source_sha256")
                if (
                    not isinstance(current_source_hash, str)
                    or len(current_source_hash) != 64
                    or any(character not in "0123456789abcdef" for character in current_source_hash)
                ):
                    return "invalid"
                if report.get("source_pdf_sha256") != current_source_hash:
                    return "stale"
                compiled_source_hash = compile_report.get("source_pdf_sha256")
                if (
                    compiled_source_hash is not None
                    and compiled_source_hash != current_source_hash
                ):
                    return "stale"
            return value
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError):
            return "invalid"

    profile_id = None
    if exists["profile"]:
        try:
            profile_id = load_work_profile(work_dir)["id"]
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError):
            profile_id = "invalid"

    manifest: dict = {}
    if exists["manifest"]:
        try:
            manifest = _read_json(artifacts["manifest"], work_dir)
        except (ArtifactSafetyError, KeyError, TypeError, ValueError, OSError):
            manifest = {}
    adapter_value = manifest.get("adapter")
    adapter_invalid = adapter_value is not None and not isinstance(
        adapter_value, dict
    )
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
    elif status("source_audit") in {
        "needs_manual_review",
        "manual_source_review_required",
    }:
        next_action = (
            "complete the independent per-page source review; translation preparation and "
            "final QA remain blocked while source evidence needs manual review"
        )
    elif status("source_audit") != "passed":
        next_action = "repair extraction, then rerun `pipeline.py source-audit WORK_DIR`"
    elif not exists["glossary"]:
        next_action = "run `init_glossary.py WORK_DIR`, then review the glossary"
    elif not exists["translation_plan"]:
        next_action = "after reviewing the glossary, run `pipeline.py prepare WORK_DIR`"
    elif status("translation_plan") != "passed":
        next_action = "repair changed plan inputs/requests, then rerun `pipeline.py prepare WORK_DIR`"
    elif status("translation_audit") != "passed":
        next_action = "fill/resume response JSONL files, then run `audit_translation.py WORK_DIR --progress`"
    elif status("output_audit") != "passed":
        next_action = "run `pipeline.py build WORK_DIR`"
    elif status("docx_audit") != "passed":
        next_action = (
            "run `pipeline.py docx WORK_DIR --markdown AUDITED_STRUCTURED.md`; "
            "the Markdown must already contain complete semantic callout containers"
        )
    elif status("compile_audit") != "passed":
        next_action = "run `pipeline.py compile-docx WORK_DIR`"
    elif status("visual_review") != "passed":
        next_action = "inspect every final render and record the visual review"
    elif status("qa_report") != "passed":
        next_action = "run `pipeline.py finalize WORK_DIR`"
    else:
        next_action = "complete"
    return {
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
        "gate_statuses": {
            name: status(name)
            for name in (
                "source_audit",
                "translation_plan",
                "translation_audit",
                "output_audit",
                "docx_audit",
                "compile_audit",
                "visual_review",
                "qa_report",
            )
        },
        "next_action": next_action,
    }


def role_counts(work_dir: Path) -> dict[str, int]:
    ir = _read_json(work_dir / "document-ir.json", work_dir)
    inventories = ir.get("inventories", {})
    generic = inventories.get("role_inventory")
    if isinstance(generic, dict):
        return {
            role: int(item.get("occurrence_count", 0))
            for role, item in generic.items()
            if isinstance(item, dict)
        }
    return inventories.get("semantic_role_counts", {})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Profile-aware entry point for deterministic stages. Translation and visual "
            "review remain explicit human/model checkpoints."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-profile")
    validate.add_argument("profile", nargs="?", default="assignment-en-zh")

    source = subparsers.add_parser("source")
    source.add_argument("pdf", type=Path)
    source.add_argument("--work-dir", type=Path, required=True)
    source.add_argument("--profile", default="assignment-en-zh")
    source.add_argument("--render-dpi", type=int, default=120)

    import_mineru = subparsers.add_parser("import-mineru")
    import_mineru.add_argument("pdf", type=Path)
    import_mineru.add_argument("mineru_output_dir", type=Path)
    import_mineru.add_argument("--work-dir", type=Path, required=True)
    import_mineru.add_argument("--profile", required=True)
    import_mineru.add_argument("--render-dpi", type=int, default=120)
    import_mineru.add_argument("--force", action="store_true")

    source_audit = subparsers.add_parser("source-audit")
    source_audit.add_argument("work_dir", type=Path)

    ir = subparsers.add_parser("ir")
    ir.add_argument("work_dir", type=Path)
    ir.add_argument("--profile", default="assignment-en-zh")
    ir.add_argument("--force", action="store_true")

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("work_dir", type=Path)
    prepare.add_argument("--max-source-chars", type=int, default=8000)
    prepare.add_argument("--force", action="store_true")

    build = subparsers.add_parser("build")
    build.add_argument("work_dir", type=Path)
    build.add_argument("--basename")
    build.add_argument("--force", action="store_true")

    docx = subparsers.add_parser("docx")
    docx.add_argument("work_dir", type=Path)
    docx.add_argument("--markdown", type=Path, required=True)
    docx.add_argument("--resource-path", type=Path)
    docx.add_argument("--basename")
    docx.add_argument("--title")
    docx.add_argument("--minimum-images", type=int, default=0)
    docx.add_argument("--build-dir", type=Path)

    compile_docx = subparsers.add_parser("compile-docx")
    compile_docx.add_argument("work_dir", type=Path)
    compile_docx.add_argument("--basename")
    compile_docx.add_argument("--dpi", type=int, default=144)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("work_dir", type=Path)

    status = subparsers.add_parser("status")
    status.add_argument("work_dir", type=Path)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "validate-profile":
        try:
            profile = load_profile(args.profile)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            json.dumps(
                {
                    "status": "passed",
                    "profile": profile["id"],
                    "adapter": profile["input"]["adapter"],
                    "target_language": profile["translation"]["target_language"],
                    "semantic_roles": [
                        role["role"] for role in profile_contract(profile)["roles"]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "status":
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    if args.command == "source":
        profile = load_profile(args.profile)
        script = get_adapter(profile["input"]["adapter"]).script_for("source")
        run_script(
            script,
            str(args.pdf),
            "--work-dir",
            str(args.work_dir),
            "--profile",
            args.profile,
            "--render-dpi",
            str(args.render_dpi),
        )
        run_script("audit_source.py", str(args.work_dir))
        run_script("init_glossary.py", str(args.work_dir))
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    if args.command == "import-mineru":
        profile = load_profile(args.profile)
        script = get_adapter(profile["input"]["adapter"]).script_for("import")
        command = [
            str(args.pdf),
            str(args.mineru_output_dir),
            "--work-dir",
            str(args.work_dir),
            "--profile",
            args.profile,
            "--render-dpi",
            str(args.render_dpi),
        ]
        if args.force:
            command.append("--force")
        run_script(script, *command)
        run_script("audit_source.py", str(args.work_dir))
        run_script("init_glossary.py", str(args.work_dir))
        print(json.dumps(report_status(args.work_dir), ensure_ascii=False, indent=2))
        return

    work_dir = lexical_absolute_path(args.work_dir)
    validate_artifact_directory(work_dir)
    validate_artifact_tree(work_dir, work_dir, allow_missing=False)
    if args.command == "source-audit":
        run_script("document_ir.py", str(work_dir), "--check")
        run_script("audit_source.py", str(work_dir))
    elif args.command == "ir":
        command = [str(work_dir), "--profile", args.profile]
        if args.force:
            command.append("--force")
        run_script("document_ir.py", *command)
    elif args.command == "prepare":
        command = [str(work_dir), "--max-source-chars", str(args.max_source_chars)]
        if args.force:
            command.append("--force")
        run_script("prepare_translation.py", *command)
    elif args.command == "build":
        run_script("audit_translation.py", str(work_dir))
        command = [str(work_dir)]
        if args.basename:
            command.extend(["--basename", args.basename])
        if args.force:
            command.append("--force")
        run_script("build_outputs.py", *command)
        run_script("audit_outputs.py", str(work_dir))
    elif args.command == "docx":
        profile = load_work_profile(work_dir)
        markdown = _work_cli_path(args.markdown, work_dir, label="Markdown path")
        validate_artifact_file(markdown, boundary=work_dir)
        stem = _portable_basename(
            args.basename or markdown.stem,
            work_dir,
            label="DOCX basename",
        )
        output = work_dir / "output" / f"{stem}.docx"
        counts = role_counts(work_dir)
        resource_path = _work_cli_path(
            args.resource_path or markdown.parent,
            work_dir,
            label="resource path",
        )
        validate_artifact_directory(resource_path, boundary=work_dir)
        command = [
            str(markdown),
            str(output),
            "--resource-path",
            str(resource_path),
            "--profile",
            str(work_dir / "profile.json"),
        ]
        if profile.get("schema_version") == 2:
            if args.build_dir:
                raise SystemExit(
                    "--build-dir is not supported for schema V2; its deterministic "
                    "intermediates are stored under WORK_DIR/output/docx-build"
                )
            for role, count in counts.items():
                command.extend(["--expected-role", f"{role}={count}"])
        else:
            if args.build_dir:
                docx_build_dir = _work_cli_path(
                    args.build_dir, work_dir, label="DOCX build directory"
                )
                validate_artifact_tree(
                    docx_build_dir, work_dir, allow_missing=True
                )
                command.extend(["--build-dir", str(docx_build_dir)])
            command.extend(
                ["--expected-problems", str(counts.get("problem", 0))]
            )
        command.extend(["--work-dir", str(work_dir)])
        if args.title:
            command.extend(["--title", args.title])
        run_script("build_docx.py", *command)
        audit_command = [
            str(output),
            "--profile",
            str(work_dir / "profile.json"),
            "--expected-links",
            str(
                len(
                    _read_json(work_dir / "manifest.json", work_dir).get(
                        "external_uris", []
                    )
                )
            ),
            "--minimum-images",
            str(args.minimum_images),
            "--output",
            str(work_dir / "output" / "docx-audit.json"),
            "--work-dir",
            str(work_dir),
        ]
        if profile.get("schema_version") == 2:
            for role, count in counts.items():
                audit_command.extend(["--expected-role", f"{role}={count}"])
        else:
            audit_command.extend(
                [
                    "--expected-problems",
                    str(counts.get("problem", 0)),
                    "--expected-examples",
                    str(counts.get("example", 0)),
                    "--expected-tips",
                    str(counts.get("tip", 0)),
                ]
            )
        run_script("audit_docx.py", *audit_command)
    elif args.command == "compile-docx":
        profile = load_work_profile(work_dir)
        build = _read_json(
            work_dir / "output" / "build-manifest.json", work_dir
        )
        stem = _portable_basename(
            args.basename or Path(build["markdown"]).stem,
            work_dir,
            label="compile basename",
        )
        counts = role_counts(work_dir)
        command = [
            str(work_dir / "output" / f"{stem}.docx"),
            str(work_dir / "output" / f"{stem}.pdf"),
            "--render-dir",
            str(work_dir / "output" / "pdf-renders"),
            "--audit-output",
            str(work_dir / "output" / "compile-audit.json"),
            "--cjk-font",
            profile["render"]["docx"]["cjk_font"],
            "--dpi",
            str(args.dpi),
            "--work-dir",
            str(work_dir),
        ]
        if profile.get("schema_version") == 2:
            for role, count in counts.items():
                command.extend(["--expected-role", f"{role}={count}"])
        else:
            command.extend(
                ["--expected-problems", str(counts.get("problem", 0))]
            )
        run_script("compile_docx_pdf.py", *command)
    elif args.command == "finalize":
        run_script("finalize_qa.py", str(work_dir))
    print(json.dumps(report_status(work_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode or 1) from exc
